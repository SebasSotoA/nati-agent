"""The web chat server: one FastAPI process serves both the page and the API.

This mirrors the team's existing pattern (sims-alonso's legislator-arena): a
single process returns the static HTML at `/` and answers a plain-JSON chat POST
at `/api/chat`. No separate frontend server, no build step, no streaming
protocol — a turn is a few seconds, and the reply carries the ordered tool-call
steps so the UI can show what happened.

State:
  - The world (SQLite) is on disk; a fresh connection is opened per request.
  - Each conversation's message history and its Tracer live in memory, keyed by
    conv_id, so seq numbering and history continue across turns. A per-conv lock
    serializes overlapping requests to the same conversation.

FastAPI is imported lazily by `create_app()` so the rest of the CLI (doctor,
seed, fund, tools) works even if the web extras are not installed.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nati import config
from nati.agent import loop
from nati.agent.prompt import system_prompt_sha256
from nati.agent.tracer import Tracer, new_conv_id, valid_conv_id
from nati.tools import registry
from nati.world import db

WEB_DIR = Path(__file__).resolve().parent / "web"


class _Conversation:
    """One conversation's in-memory state."""

    def __init__(self, conv_id: str, operator: str, meta: dict[str, Any]) -> None:
        self.conv_id = conv_id
        self.history = loop.start_history()
        self.tracer = Tracer(conv_id=conv_id, operator=operator)
        self.tracer.conv_start(meta)
        self.lock = threading.Lock()


def create_app(profile_name: str | None = None):
    """Build the FastAPI app. Imports fastapi lazily so the CLI stays usable."""
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
    except ModuleNotFoundError as exc:  # pragma: no cover - install guidance
        raise SystemExit(
            "The web chat needs FastAPI. Run:  uv sync\n"
            f"  (missing: {exc.name})"
        ) from exc

    profile = config.load_profile(profile_name)
    operator = config.operator()
    conversations: dict[str, _Conversation] = {}
    conversations_lock = threading.Lock()

    app = FastAPI(title="Nati", docs_url=None, redoc_url=None)

    def _meta() -> dict[str, Any]:
        # Recorded once per conversation; pins the trace to an exact setup.
        con = db.connect()
        try:
            world_hash = db.world_hash(con)
        finally:
            con.close()
        return {
            "model": profile.model,
            "profile": profile.name,
            "region": profile.region,
            "system_prompt_sha256": system_prompt_sha256(),
            "tool_schemas_sha256": registry.schemas_hash(),
            "world_db_sha256": world_hash,
        }

    @app.get("/")
    def index() -> Any:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "model": profile.model, "tools": len(registry.TOOLS)}

    @app.post("/api/chat")
    def chat(payload: dict = Body(...)) -> Any:
        text = str(payload.get("message") or "").strip()
        req_conv_id = payload.get("conv_id")
        if not text:
            raise HTTPException(status_code=400, detail="Empty message.")

        # A conv_id from the client is UNTRUSTED — it names a directory. Anything
        # that is not exactly the shape we generate is rejected outright rather
        # than corrected, so a caller cannot pick where the trace is written, nor
        # write into a conversation that is not theirs. `Tracer` refuses these
        # too; this check exists so the answer is a 400 and not a 500.
        if req_conv_id is not None and not valid_conv_id(req_conv_id):
            raise HTTPException(status_code=400, detail="Invalid conversation id.")

        # Find or start the conversation.
        with conversations_lock:
            if req_conv_id and req_conv_id in conversations:
                conv = conversations[req_conv_id]
            elif req_conv_id:
                # A well-formed id this process has never seen. Start a fresh
                # conversation under a NEW id rather than adopting the given one:
                # adopting it would let a client append to, and overwrite the
                # meta.json of, a trace directory from another session.
                conv_id = new_conv_id()
                conv = _Conversation(conv_id, operator, _meta())
                conversations[conv_id] = conv
            else:
                conv_id = new_conv_id()
                conv = _Conversation(conv_id, operator, _meta())
                conversations[conv_id] = conv

        # One world connection per request; serialize turns within a conversation.
        con = db.connect()
        steps: list[dict[str, Any]] = []
        reply = ""
        ok = True
        try:
            with conv.lock:
                for event in loop.run_turn(
                    con, conv.history, text, profile, conv.tracer
                ):
                    if isinstance(event, loop.ToolStep):
                        steps.append({"name": event.name, "status": event.status,
                                      "message": event.message})
                    elif isinstance(event, loop.Reply):
                        reply = event.content
                    elif isinstance(event, loop.Failure):
                        reply = event.message
                        ok = False
                    # Thinking is a no-op for a non-streaming response.
        except Exception:  # noqa: BLE001 - the last line of defence
            # A bug in the agent loop must not become a 500 that kills the
            # conversation: that is a denial of service a single message could
            # trigger. An audit reached exactly this with an out-of-range id,
            # which raised OverflowError from the SQLite driver and escaped every
            # `except` in the dispatcher. That specific hole is closed in
            # `gates`, but the class of it is closed here — the turn degrades to
            # an apology and the conversation stays alive.
            conv.tracer.note("unhandled exception in the turn", author="server")
            reply = (
                "Something went wrong on my side and I could not finish that. "
                "Nothing was recorded. Please try again."
            )
            ok = False
        finally:
            con.close()

        return JSONResponse({
            "conv_id": conv.conv_id,
            "reply": reply,
            "steps": steps,
            "ok": ok,
        })

    return app


def serve(host: str, port: int, profile_name: str | None = None) -> None:
    """Run the server. Single worker: the in-memory state is per-process."""
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise SystemExit(
            "The web chat needs uvicorn. Run:  uv sync\n"
            f"  (missing: {exc.name})"
        ) from exc

    uvicorn.run(create_app(profile_name), host=host, port=port, log_level="warning")
