#!/usr/bin/env python3
"""nova_change_class.py — change classes (standard / normal / emergency) and change outcomes.

Little Mister, 2026-10-15 (plan item #55). DORA's change-failure rate needs two facts per change:
what kind of change it was, and whether it hurt. This module owns only those two facts:

  * the columns on coagency_proposals (change_class, outcome, outcome_incident_id, outcome_at),
    added lock-safe through nova_pg_schema.safe_ddl — the co-agency LOGIC stays in nova_coagency
    (it may call classify_proposal() when it files or executes a proposal);
  * pure classifiers for proposals and git commits;
  * git_commits(), the one git-log reader shared by nova_problems and nova_comet_tail.

Classes (ITIL):
  emergency  made under fire: names an outage/incident/hotfix/revert, or filed while a critical
             incident on its target was open
  standard   pre-approved, routine, low risk: docs/tests/chore commits, threshold / timeout /
             schedule / routing / dedup tweaks, retiring or muting a noisy check
  normal     everything else
Outcomes (set by nova_comet_tail --changes): success | failed (an incident it plausibly caused
opened within 24 h) | rolled_back (reverted) | pending (< 24 h old).
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

CLASSES = ("standard", "normal", "emergency")
OUTCOMES = ("success", "failed", "rolled_back", "pending")
REPO = Path(__file__).resolve().parent.parent

COLUMNS = """
ALTER TABLE coagency_proposals ADD COLUMN IF NOT EXISTS change_class text,
  ADD COLUMN IF NOT EXISTS outcome text,
  ADD COLUMN IF NOT EXISTS outcome_incident_id bigint,
  ADD COLUMN IF NOT EXISTS outcome_at timestamptz;
"""

EMERGENCY_RE = re.compile(r"\b(hot ?fix|emergency|urgent|outage|incident #?\d+|revert|roll ?back|sev ?[01]|"
                          r"p0|is down|broken|data loss|restore from)\b", re.I)
STANDARD_RE = re.compile(r"\b(threshold|timeout|schedule|cron|dedup|window|route|routing|mute|retire|disable "
                         r"(the )?(alert|check)|tune|typo|docs?|readme|comment|rename|bump|cap )", re.I)
STANDARD_COMMIT_RE = re.compile(r"^(docs|test|tests|chore|style|ci)(\(.+\))?:", re.I)
DOC_FILE_RE = re.compile(r"(\.md$|^docs/|/tests?/|(^|/)test_[^/]*\.py$)")
REVERT_RE = re.compile(r'^Revert "(.+)"|\brevert(?:s|ed)?\s+([0-9a-f]{7,40})\b', re.I)


def ensure_columns(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, COLUMNS)


def classify_proposal(action: str | None, rationale: str | None = None, origin: str | None = None,
                      open_critical: bool = False) -> str:
    """Pure. -> standard | normal | emergency."""
    text = f"{action or ''} {rationale or ''}"
    if open_critical or EMERGENCY_RE.search(text):
        return "emergency"
    if STANDARD_RE.search(action or ""):
        return "standard"
    return "normal"


def classify_commit(subject: str | None, files: list | None = None) -> str:
    """Pure. -> standard | normal | emergency."""
    s = subject or ""
    if EMERGENCY_RE.search(s):
        return "emergency"
    fs = [f for f in (files or []) if f]
    if STANDARD_COMMIT_RE.match(s) or (fs and all(DOC_FILE_RE.search(f) for f in fs)):
        return "standard"
    return "normal"


def reverted_target(subject: str | None) -> tuple[str | None, str | None]:
    """A revert commit -> (original subject, original sha prefix); (None, None) otherwise. Pure."""
    m = REVERT_RE.search(subject or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)


def git_commits(days: int = 90, repo: Path = REPO, _run=subprocess.run) -> list:
    """[{sha, at, subject, files}] newest first; [] if git is unavailable (fails open)."""
    try:
        r = _run(["git", "-C", str(repo), "log", f"--since={days} days ago", "--name-only",
                  "--pretty=format:\x1e%H\x1f%cI\x1f%s"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for block in r.stdout.split("\x1e"):
        if not block.strip():
            continue
        head, *files = block.strip("\n").split("\n")
        try:
            sha, at, subj = head.split("\x1f", 2)
            out.append({"sha": sha, "at": datetime.fromisoformat(at), "subject": subj,
                        "files": [f for f in files if f.strip()]})
        except ValueError:
            continue
    return out
