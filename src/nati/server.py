"""The HTTP process: local web chat for developers, WhatsApp webhook in production.

Locally, `nati serve` still returns the static HTML at `/` and answers
`POST /api/chat`. On Fly.io, `NATI_PUBLIC_CHAT=0` hides those routes so the
public host is health + WhatsApp only.

State:
  - The world (SQLite) is on disk; a fresh connection is opened per request.
  - Each conversation's message history and its Tracer live in memory, keyed by
    conv_id, so seq numbering and history continue across turns. A per-conv lock
    serializes overlapping requests to the same conversation.
  - WhatsApp sessions are also persisted under VAR_DIR so a deploy does not
    drop the administrator's history.

FastAPI is imported lazily by `create_app()` so the rest of the CLI (doctor,
seed, fund, tools) works even if the web extras are not installed.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from nati import config
from nati.agent import loop
from nati.agent.prompt import system_prompt_sha256
from nati.agent.tracer import Tracer, new_conv_id, valid_conv_id
from nati.channels import sessions, whatsapp
from nati.tools import registry
from nati.world import db

WEB_DIR = Path(__file__).resolve().parent / "web"

# What the chat shows when the output filter suppresses a reply. Plain language:
# the person reading this is the administrator, not a developer.
_FILTER_SAID = {
    "figures": "held back an amount no tool had returned — asking again",
    "internals": "held back a mention of how I work — asking again",
    "off_domain": "held back a code block — asking again",
    "reasoning": "held back leaked reasoning — asking again",
}


def _collapse_starts(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop each `start` row that is immediately followed by its own outcome.

    The loop reports a tool twice on purpose — once when it is CALLED and once
    when it ANSWERS — because a streaming caller needs the first to show
    progress. This response is not streamed: the whole turn arrives at once, so
    both rows land together and one successful call renders as two identical
    lines.

    That is not a cosmetic problem in this application. Two rows reading
    "used record_payment" is indistinguishable from a payment recorded twice,
    which is the very first thing anyone checks in a set of books — and it was
    reported as exactly that. The audit log had one payment row; the chat had
    two lines.

    A `start` with nothing after it is KEPT. That happens only when the turn
    died mid-tool, and dropping it would hide the attempt completely.
    """
    kept: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if step.get("status") == "start":
            nxt = steps[index + 1] if index + 1 < len(steps) else None
            if nxt and nxt.get("status") != "start" and nxt.get("name") == step["name"]:
                continue
        kept.append(step)
    return kept


class _Conversation:
    """One conversation's in-memory state."""

    def __init__(
        self,
        conv_id: str,
        operator: str,
        meta: dict[str, Any],
        *,
        resume: bool = False,
    ) -> None:
        self.conv_id = conv_id
        self.history = loop.start_history()
        self.tracer = Tracer(conv_id=conv_id, operator=operator)
        if resume and (self.tracer.dir / "turns.jsonl").exists():
            self.tracer.resume_seq()
        else:
            self.tracer.conv_start(meta)
        self.lock = threading.Lock()
        self.phone: str | None = None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def apply_data_dir() -> None:
    """Honor NATI_DATA_DIR so Fly can mount a volume at /data."""
    config.load_dotenv()
    config.refresh_paths()
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    config.TRACES_DIR.mkdir(parents=True, exist_ok=True)


def create_app(profile_name: str | None = None, *, public_chat: bool | None = None):
    """Build the FastAPI app. Imports fastapi lazily so the CLI stays usable."""
    try:
        from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Query, Request
        from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
    except ModuleNotFoundError as exc:  # pragma: no cover - install guidance
        raise SystemExit(
            "The web chat needs FastAPI. Run:  uv sync\n"
            f"  (missing: {exc.name})"
        ) from exc

    if public_chat is None:
        public_chat = _truthy(os.environ.get("NATI_PUBLIC_CHAT", "0"))

    apply_data_dir()
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

    def _run_chat(conv: _Conversation, text: str) -> tuple[str, list[dict[str, Any]], bool]:
        steps: list[dict[str, Any]] = []
        reply = ""
        ok = True
        con = db.connect()
        try:
            with conv.lock:
                for event in loop.run_turn(
                    con, conv.history, text, profile, conv.tracer
                ):
                    if isinstance(event, loop.ToolStep):
                        steps.append({
                            "name": event.name, "status": event.status,
                            "message": event.message,
                            "summary": event.summary or None,
                            "debug": {"reason": event.reason}
                            if event.reason else None,
                        })
                    elif isinstance(event, loop.Reply):
                        reply = event.content
                    elif isinstance(event, loop.Retrying):
                        steps.append({
                            "name": "output filter", "status": "blocked",
                            "message": _FILTER_SAID.get(
                                event.cause, "the reply was not sent"),
                            "debug": {
                                "reason": event.cause,
                                "unverified": event.unverified,
                                "disclosed": event.disclosed,
                                "allowed": event.allowed,
                                "withheld": event.withheld,
                            },
                        })
                    elif isinstance(event, loop.Failure):
                        reply = event.message
                        ok = False
        except Exception:  # noqa: BLE001 - the last line of defence
            conv.tracer.note("unhandled exception in the turn", author="server")
            reply = (
                "Something went wrong on my side and I could not finish that. "
                "Nothing was recorded. Please try again."
            )
            ok = False
        finally:
            con.close()
        if conv.phone:
            sessions.save(conv.phone, conv.conv_id, conv.history)
        return reply, _collapse_starts(steps), ok

    def _conversation_for_phone(phone: str) -> _Conversation:
        with conversations_lock:
            for conv in conversations.values():
                if conv.phone == phone:
                    return conv
            stored_id, stored_history = sessions.load(phone)
            resume = bool(stored_id and valid_conv_id(stored_id))
            conv_id = stored_id if resume else new_conv_id()
            conv = _Conversation(conv_id, operator, _meta(), resume=resume)
            if stored_history:
                conv.history = stored_history
            conv.phone = phone
            conversations[conv_id] = conv
            return conv

    def _handle_whatsapp(payload: dict[str, Any]) -> None:
        extracted = whatsapp.extract_text(payload)
        if extracted is None:
            return
        sender, text, msg_id = extracted
        if not whatsapp.same_phone(sender, whatsapp.admin_phone()):
            return
        if not sessions.claim_message(msg_id):
            return
        conv = _conversation_for_phone(sender)
        reply, steps, _ok = _run_chat(conv, text)
        outbound = whatsapp.compose_outbound(reply, steps)
        try:
            whatsapp.send_reply(sender, outbound)
        except (OSError, TimeoutError) as exc:
            conv.tracer.note(f"whatsapp send failed: {exc}", author="server")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "model": profile.model, "tools": len(registry.TOOLS)}

    @app.get("/webhook/whatsapp")
    def whatsapp_verify(
        hub_mode: str | None = Query(None, alias="hub.mode"),
        hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
        hub_challenge: str | None = Query(None, alias="hub.challenge"),
    ) -> Any:
        if hub_mode == "subscribe" and hub_verify_token == whatsapp.verify_token():
            return PlainTextResponse(hub_challenge or "")
        raise HTTPException(status_code=403, detail="Forbidden")

    @app.post("/webhook/whatsapp")
    async def whatsapp_event(
        request: Request,
        background: BackgroundTasks,
        x_hub_signature_256: str | None = Header(None, alias="X-Hub-Signature-256"),
    ) -> Any:
        if not whatsapp.configured():
            raise HTTPException(status_code=503, detail="WhatsApp is not configured.")
        body = await request.body()
        if not whatsapp.signature_ok(
            body, x_hub_signature_256 or "", whatsapp.app_secret()
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON")
        background.add_task(_handle_whatsapp, payload)
        return JSONResponse({"status": "ok"})

    if public_chat:
        @app.get("/")
        def index() -> Any:
            return FileResponse(WEB_DIR / "index.html")

    if public_chat:
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

            reply, steps, ok = _run_chat(conv, text)
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

    apply_data_dir()
    env = os.environ.get("NATI_PUBLIC_CHAT")
    if env is None or not str(env).strip():
        public = host in ("127.0.0.1", "localhost", "::1")
    else:
        public = _truthy(env)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if public and not loopback:
        raise SystemExit(
            "Refusing to expose the web chat on a public address.\n"
            "  Use --host 127.0.0.1 for the developer UI, or set "
            "NATI_PUBLIC_CHAT=0 for webhooks only."
        )
    uvicorn.run(
        create_app(profile_name, public_chat=public),
        host=host, port=port, log_level="warning",
    )
