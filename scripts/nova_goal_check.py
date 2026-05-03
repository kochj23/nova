#!/usr/bin/env python3
"""
nova_goal_check.py — Daily goal accountability check.

Runs at 7:05am (after morning brief). Detects git activity across focus
projects, identifies stale/overdue goals, and posts a focused nudge to Slack
if anything needs attention. Silent if everything is on track.

Also promotes any pending corrections to rules.

Cron: 5 7 * * *
Written by Jordan Koch.
"""

import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR
from nova_goals import (
    ensure_schema as ensure_goals_schema,
    get_active_goals, get_stale_goals, get_overdue_goals,
    detect_activity_from_git, format_goals_brief, goal_summary,
)
from nova_rules import (
    ensure_schema as ensure_rules_schema,
    promote_corrections, get_active_rules,
)

SOURCE = "nova_goal_check"
TODAY = date.today().isoformat()


def main():
    log("Starting daily goal check", level=LOG_INFO, source=SOURCE)

    # Ensure tables exist
    ensure_goals_schema()
    ensure_rules_schema()

    # Detect git activity on focus projects
    detect_activity_from_git()

    # Promote any new corrections to rules
    promoted = promote_corrections()
    if promoted:
        log(f"Promoted {promoted} corrections to rules", level=LOG_INFO, source=SOURCE)

    # Check for issues
    stale = get_stale_goals()
    overdue = get_overdue_goals()
    active = get_active_goals()
    summary = goal_summary()

    # Only post if there's something to say
    if not stale and not overdue and len(active) <= 4:
        log("All goals on track, nothing to report", level=LOG_INFO, source=SOURCE)
        return 0

    lines = [f"*Goal Check — {TODAY}*"]

    if overdue:
        lines.append("")
        lines.append("🔴 *Overdue:*")
        for g in overdue:
            lines.append(f"  • {g['title']} — was due {g['deadline']}")

    if stale:
        lines.append("")
        lines.append("⏸️ *Stale (no progress):*")
        for g in stale:
            lines.append(f"  • {g['title']} — {g['days_idle']}d since last activity")

    if len(active) > 4:
        lines.append("")
        lines.append(f"⚠️ You have {len(active)} active goals. Your rule is 3-4 max. "
                     "Consider pausing or dropping something.")

    # Rules summary
    rules = get_active_rules()
    if promoted:
        lines.append("")
        lines.append(f"📝 {promoted} new rule(s) from corrections. {len(rules)} total active rules.")

    nova_config.post_both("\n".join(lines), slack_channel=nova_config.SLACK_NOTIFY)
    log("Goal check posted to Slack", level=LOG_INFO, source=SOURCE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
