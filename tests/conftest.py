"""Pytest defaults for the world.

Production `db.create()` builds the empty bootstrap. Existing tests were written
against the 29-member challenge fixture, so tests opt into that seed via
NATI_SEED unless a test passes seed= explicitly. pytest loads this module
before test modules; `create()` reads the env at call time.
"""

from __future__ import annotations

import os

os.environ.setdefault("NATI_SEED", "challenge")
