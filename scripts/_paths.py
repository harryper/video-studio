"""Centralised path constants for video-studio.

All daemons, tests, and tooling import from here instead of hardcoding
absolute paths. SKILL_DIR is derived from this file's location so the
project tree can be moved/copied to any location on any machine.

After the openclaw decoupling (commit 9544fe5), this module deliberately
does NOT expose any node / openclaw binary constants — the LLM backend
is the anthropic SDK (see scripts/llm_client.py) and the script daemon
no longer shells out to a node-based agent.

Usage:
    import sys
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from _paths import SKILL_DIR, JOBS_DIR, RUNS_DIR, LOGS_DIR
"""
from pathlib import Path

# ── Derived roots (relative — never hardcode) ────────────────────────
SKILL_DIR = Path(__file__).resolve().parents[1]

# ── Project subdirs (relative to SKILL_DIR) ──────────────────────────
JOBS_DIR = SKILL_DIR / "jobs" / "video"
RUNS_DIR = SKILL_DIR / "runs"
LOGS_DIR = SKILL_DIR / "logs"

# ── Reference (read-only snapshot) ───────────────────────────────────
REFERENCE_CAT_DOCTOR = SKILL_DIR / "reference" / "cat-doctor"