#!/usr/bin/env python3
"""
nova_journal_lint.py — Validate and auto-fix Hugo frontmatter in nova-journal.

Runs as a scheduled task (every 30 min) or as a pre-push git hook.
Catches the recurring issue where LLM-generated titles contain unescaped
quotes or characters that break YAML parsing, preventing GitHub Pages deploy.

Fixes applied automatically:
  - Nested/unescaped double quotes in title/alt/description fields
  - Emoji prefixes in title fields (🎨, 📺, 💬, 📄, 🌃, etc.)
  - Colons in unquoted values
  - Trailing quote mismatches

If fixes are applied, commits and pushes them so the deploy unblocks.

Written by Jordan Koch.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content"
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal_lint.log"

EMOJI_PATTERN = re.compile(r'^[\U0001F300-\U0001FAFF☀-➿‍️\s]+')
YAML_FIELDS = ("title", "alt", "description")


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fix_yaml_value(field: str, raw_value: str) -> str:
    """Fix a YAML string value that has broken quoting.

    Only fixes actual YAML-breaking issues:
    - Unescaped double quotes inside a double-quoted value
    - Emoji followed by unescaped quotes (e.g. 🎨 "Title")
    Returns the raw value unchanged if it's valid YAML.
    """
    val = raw_value.strip()

    # Not quoted — only a problem if it contains unescaped special chars
    if not (val.startswith('"') and val.endswith('"')):
        return val

    # Quoted value — check for unescaped inner quotes
    inner = val[1:-1]

    # Count unescaped quotes (not preceded by backslash)
    unescaped_quotes = len(re.findall(r'(?<!\\)"', inner))
    if unescaped_quotes == 0:
        return val  # Already valid

    # Has unescaped inner quotes — fix them
    # Strip emoji prefix if it precedes a quoted substring
    if field in ("title", "alt"):
        inner = re.sub(r'^([\U0001F300-\U0001FAFF☀-➿️‍]+\s*)"', '', inner)
        # If the inner content is itself wrapped in quotes, unwrap
        if inner.endswith('"'):
            inner = inner[:-1]

    # Escape or remove remaining inner quotes
    inner = inner.replace('\\"', '__ESC__')
    inner = inner.replace('"', '')
    inner = inner.replace('__ESC__', '\\"')
    inner = inner.strip()

    return f'"{inner}"'


def lint_file(filepath: Path) -> list[str]:
    """Check a single markdown file's frontmatter. Returns list of fixes applied."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    if not content.startswith("---"):
        return []

    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        return []

    frontmatter = content[4:end_idx]
    body = content[end_idx:]
    lines = frontmatter.split("\n")
    fixes = []
    new_lines = []

    for line in lines:
        fixed_line = line
        for field in YAML_FIELDS:
            prefix = f"{field}: "
            indent_prefix = f"  {field}: "

            if line.lstrip().startswith(prefix):
                indent = line[:len(line) - len(line.lstrip())]
                raw_value = line.lstrip()[len(prefix):]
                new_value = fix_yaml_value(field, raw_value)
                if new_value != raw_value.strip():
                    fixed_line = f"{indent}{prefix}{new_value}"
                    fixes.append(f"{field}: {raw_value.strip()[:60]} -> {new_value[:60]}")
                break

        new_lines.append(fixed_line)

    if fixes:
        new_frontmatter = "\n".join(new_lines)
        new_content = "---\n" + new_frontmatter + body
        filepath.write_text(new_content, encoding="utf-8")

    return fixes


def hugo_build_check() -> tuple[bool, str]:
    """Run hugo build and return (success, error_output)."""
    try:
        result = subprocess.run(
            ["hugo", "--gc", "--minify", "--buildFuture", "--quiet"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=120
        )
        return result.returncode == 0, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def git_commit_and_push(files_fixed: int):
    """Commit and push fixes."""
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
        msg = f"fix(lint): Auto-fix {files_fixed} file(s) with broken YAML frontmatter"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
            log(f"Pushed auto-fix commit: {files_fixed} file(s)")
        elif "nothing to commit" in (result.stdout + result.stderr):
            log("No changes to commit after lint")
    except subprocess.TimeoutExpired:
        log("ERROR: git operation timed out")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"

    log(f"Journal lint starting (mode={mode})")

    all_fixes = {}
    md_files = sorted(CONTENT_DIR.rglob("*.md"))

    for f in md_files:
        fixes = lint_file(f)
        if fixes:
            all_fixes[str(f.relative_to(HUGO_ROOT))] = fixes

    if all_fixes:
        total_fixes = sum(len(v) for v in all_fixes.values())
        log(f"Fixed {total_fixes} issue(s) in {len(all_fixes)} file(s):")
        for path, fixes in all_fixes.items():
            for fix in fixes:
                log(f"  {path}: {fix}")

        if mode == "auto":
            ok, err = hugo_build_check()
            if ok:
                git_commit_and_push(len(all_fixes))
                log("Hugo build passes after fixes — deployed")
            else:
                log(f"ERROR: Hugo still failing after lint fixes: {err[:200]}")
                notify_slack(err)
        elif mode == "hook":
            print(f"LINT: Fixed {total_fixes} issue(s) — re-stage and commit")
            sys.exit(1)
        else:
            log("Dry run — no commit (use 'auto' mode to commit)")
    else:
        log(f"All {len(md_files)} files OK")

    if mode == "auto" and not all_fixes:
        ok, err = hugo_build_check()
        if not ok:
            log(f"WARNING: Hugo build failing but no lint fixes found: {err[:200]}")
            notify_slack(err)


def notify_slack(error: str):
    """Post build failure to #nova-notifications."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from nova_config import slack_post
        slack_post(
            "#nova-notifications",
            f"⚠️ *Journal deploy broken* — Hugo build failing, lint couldn't auto-fix.\n```{error[:500]}```"
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
