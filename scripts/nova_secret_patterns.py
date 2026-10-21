#!/usr/bin/env python3
"""Sensitive secret-scan patterns, fetched at RUNTIME from the fleet secret store.

Why this module exists
----------------------
The scanners that hunt for Jordan's known-bad credential strings used to carry those
strings as literals inside TRACKED test files -- in a PUBLIC repo (kochj23/nova). The
detector therefore published the very secret it was built to detect: the string entered
history in 0adc04e on 2026-05-13 and was world-readable for ~5 months. A secret scanner
must never be the thing that leaks the secret.

So the sensitive patterns now live in the fleet secret store (`nova.secrets`, pgcrypto,
via `nova_secrets`) under SECRET_NAME, and both the git hooks and the test suite fetch
them at runtime. Nothing sensitive is tracked in git any more.

Storage
-------
  name  : SECRET_NAME ("nova-secret-scan-patterns")
  value : JSON list of fixed strings, e.g. ["pattern-a", "pattern-b"]
  write : printf '%s' '["..."]' | python3 nova_secrets.py set nova-secret-scan-patterns 'note'
          (plaintext via STDIN, never argv -- same rule as every other fleet secret)

The entries are plain substrings, not regexes: callers match them with `grep -F` /
`str.__contains__`, so a pattern containing regex metacharacters stays literal.

Fail loud, never silently pass
------------------------------
If the store is unreachable (no NOVA_SECRET_KEY, DB down, psycopg2 missing) this module
raises PatternStoreUnavailable rather than returning an empty list. An empty list would
make every "this file contains no secrets" assertion vacuously true -- a scanner that
passes because it has nothing to look for is worse than no scanner, because it reports
green. Callers must degrade visibly instead:

  * tests      -> patterns_or_skip(self) marks the test SKIPPED with the reason
  * git hooks  -> `list` exits 3; the hook prints a loud SKIPPED warning for this one
                  check and keeps its remaining checks running

CLI
---
  nova_secret_patterns.py list     one pattern per line (consumed by githooks via `grep -f -`)
  nova_secret_patterns.py check    print status to stderr; exit 0 reachable / 3 unavailable
  exit 3 == store unavailable (distinct from 1, so hooks can tell "broken" from "found nothing")
"""
import json
import os
import sys

SECRET_NAME = "nova-secret-scan-patterns"

#: exit status meaning "the pattern store could not be reached" (see module docstring)
EXIT_UNAVAILABLE = 3


class PatternStoreUnavailable(RuntimeError):
    """The pattern store could not be reached, so the scan CANNOT be trusted."""


def load_patterns():
    """Return the sensitive patterns as a list of fixed strings.

    Raises PatternStoreUnavailable on any failure -- missing master key, DB down,
    malformed value, empty list. Never returns [] (see module docstring).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import nova_secrets
    except Exception as e:  # psycopg2 absent, nova_dsn missing, ...
        raise PatternStoreUnavailable(f"cannot import nova_secrets: {e}") from e

    try:
        raw = nova_secrets.get_secret(SECRET_NAME)
    except SystemExit as e:
        # nova_secrets._env() calls sys.exit() when NOVA_SECRET_KEY is absent. Catch it:
        # a missing key must not take the whole test run / hook down with a bare exit.
        raise PatternStoreUnavailable(f"master key or secret unavailable: {e}") from e
    except Exception as e:
        raise PatternStoreUnavailable(f"{SECRET_NAME} not readable: {e}") from e

    try:
        pats = json.loads(raw)
    except Exception as e:
        raise PatternStoreUnavailable(f"{SECRET_NAME} is not valid JSON: {e}") from e

    if not isinstance(pats, list) or not all(isinstance(p, str) and p for p in pats):
        raise PatternStoreUnavailable(f"{SECRET_NAME} must be a JSON list of non-empty strings")
    if not pats:
        raise PatternStoreUnavailable(f"{SECRET_NAME} is an empty list -- nothing would be scanned")
    return pats


def patterns_or_skip(testcase):
    """Return the patterns, or mark `testcase` SKIPPED with a clear reason.

    Use this in a test dedicated to the store-backed patterns, so that only that test
    skips -- the surrounding static assertions (``sk-``, ``ghp_``, ...) keep running.
    """
    try:
        return load_patterns()
    except PatternStoreUnavailable as e:
        testcase.skipTest(
            f"known-credential scan SKIPPED: pattern store unreachable ({e}). "
            f"This test did NOT verify the absence of known credentials."
        )


def assert_no_known_credentials(testcase, src, what="source"):
    """Assert that `src` contains none of the store-backed patterns.

    Skips (loudly) rather than passing when the store is unreachable. The failure message
    deliberately names only the pattern's index and length, never the pattern itself, so a
    CI log or a screenshot of a red test does not re-leak the secret.
    """
    for i, pat in enumerate(patterns_or_skip(testcase)):
        testcase.assertNotIn(
            pat, src,
            f"{what} contains known-credential pattern #{i} (len {len(pat)}) from "
            f"{SECRET_NAME} -- value withheld; read it from the secret store",
        )


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd not in ("list", "check"):
        sys.stderr.write(f"usage: {os.path.basename(argv[0])} list|check\n")
        return 2
    try:
        pats = load_patterns()
    except PatternStoreUnavailable as e:
        sys.stderr.write(f"[nova_secret_patterns] UNAVAILABLE: {e}\n")
        return EXIT_UNAVAILABLE
    if cmd == "check":
        # count only -- never the patterns themselves on a status check
        sys.stderr.write(f"[nova_secret_patterns] OK: {len(pats)} pattern(s) from {SECRET_NAME}\n")
        return 0
    for p in pats:
        sys.stdout.write(p + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
