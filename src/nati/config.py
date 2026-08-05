"""Configuration: model profiles, paths, and the Bedrock key.

The key is read from the environment or from a local `.env` file, which is in
.gitignore. NEVER from the repo: it is a spend-capable credential and this repo
is handed to the people who will attack the agent.

Flow for a teammate cloning it:  cp .env.example .env  and paste the key in.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Repo root: .../agent/src/nati/config.py -> .../agent
ROOT = Path(__file__).resolve().parents[2]

PROFILES_FILE = ROOT / "profiles.toml"
ENV_FILE = ROOT / ".env"
VAR_DIR = ROOT / "var"
TRACES_DIR = ROOT / "traces"
DB_PATH = VAR_DIR / "world.db"
SCHEMA_SQL = ROOT / "src" / "nati" / "world" / "schema.sql"
SEED_SQL = ROOT / "src" / "nati" / "world" / "seed.sql"

# The synthetic world's "today". Fixed so runs are reproducible: the 2026 cycle
# is seeded through August 4th.
WORLD_TODAY = "2026-08-04"

_KEY_ENV_VARS = ("NATI_API_KEY", "AWS_BEARER_TOKEN_BEDROCK")


class ConfigError(Exception):
    """Invalid or incomplete configuration, with an actionable message."""


def load_dotenv(path: Path | None = None) -> list[str]:
    """Loads ROOT/.env into os.environ. Returns the keys it set.

    Variables ALREADY in the environment win: an explicit `export` must not be
    overridden by the file. A deliberately minimal parser (no new dependency):
    it handles comments, blank lines, an optional `export `, and quotes.
    """
    path = path or ENV_FILE
    if not path.exists():
        return []

    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


@dataclass(frozen=True)
class Profile:
    """A model profile resolved from profiles.toml."""

    name: str
    model: str
    base_url: str
    region: str
    max_tokens: int
    reasoning_effort: str
    temperature: float
    description: str


def _load_profiles_doc() -> dict:
    if not PROFILES_FILE.exists():
        raise ConfigError(
            f"I cannot find {PROFILES_FILE.name}.\n"
            f"  Expected at: {PROFILES_FILE}"
        )
    try:
        with PROFILES_FILE.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{PROFILES_FILE.name} is not valid TOML: {exc}") from exc


def list_profiles() -> list[str]:
    return sorted(_load_profiles_doc().get("profiles", {}))


def load_profile(name: str | None = None) -> Profile:
    """Resolves a profile by name, or the default from profiles.toml.

    Nothing in the codebase depends on which model is configured. Resolution
    order, most specific first:

      1. the `name` argument (from `--profile`)
      2. NATI_PROFILE
      3. `default_profile` in profiles.toml
      4. the first profile defined

    NATI_MODEL then overrides just the model id, so a different model can be
    tried without editing any file.
    """
    load_dotenv()
    doc = _load_profiles_doc()
    profiles = doc.get("profiles", {})
    if not profiles:
        raise ConfigError(f"{PROFILES_FILE.name} defines no [profiles.*]")

    name = (
        name
        or os.environ.get("NATI_PROFILE", "").strip()
        or doc.get("default_profile")
        or next(iter(profiles))
    )
    if name not in profiles:
        available = ", ".join(sorted(profiles))
        raise ConfigError(
            f"Profile '{name}' does not exist.\n"
            f"  Available: {available}\n"
            f"  Profiles are defined in {PROFILES_FILE.name}."
        )

    p = profiles[name]
    missing = [k for k in ("model", "base_url", "region") if k not in p]
    if missing:
        raise ConfigError(
            f"Profile '{name}' is incomplete; missing: {', '.join(missing)}"
        )

    # An ad-hoc model swap with no file edit: NATI_MODEL=... uv run nati doctor
    model = os.environ.get("NATI_MODEL", "").strip() or p["model"]

    return Profile(
        name=name,
        model=model,
        base_url=p["base_url"],
        region=p["region"],
        max_tokens=int(p.get("max_tokens", 4096)),
        # Some reasoning models only return their Chain of Thought when it is
        # explicitly requested; others ignore this field. Empty means "do not
        # ask", which is right for families that have no separate reasoning
        # channel.
        reasoning_effort=str(p.get("reasoning_effort", "")),
        temperature=float(p.get("temperature", 0.3)),
        description=p.get("description", ""),
    )


def model_override() -> str | None:
    """The NATI_MODEL override, if set. Used by `doctor` to report it."""
    load_dotenv()
    return os.environ.get("NATI_MODEL", "").strip() or None


def api_key() -> str:
    """The Bedrock key, from the environment or .env. Fails with instructions."""
    load_dotenv()
    for var in _KEY_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    raise ConfigError(
        "The Bedrock key is missing.\n"
        "  Option 1:  cp .env.example .env   and paste the key inside\n"
        "  Option 2:  export NATI_API_KEY=<the team's key>\n"
        f"  (I also accept {_KEY_ENV_VARS[1]})"
    )


def api_key_present() -> bool:
    load_dotenv()
    return any(os.environ.get(v, "").strip() for v in _KEY_ENV_VARS)


def key_source() -> str:
    """Where the key came from, so `doctor` can say without revealing it."""
    if not api_key_present():
        return "none"
    return ".env" if ENV_FILE.exists() else "environment"


def operator() -> str:
    """Who is running the agent. Recorded in every trace.

    Two purposes: attributing a finding to whoever found it, and tracing Bedrock
    spend (the key is shared by the whole team).
    """
    load_dotenv()
    return os.environ.get("NATI_USER", "").strip() or "anonymous"
