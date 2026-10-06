# Nova test conventions (the 7 house categories)

Jordan's rule: every script carries tests in ALL SEVEN categories. One dedicated file per
script: `tests/test_<script stem>.py` (e.g. `nova_affect.py` -> `tests/test_nova_affect.py`).
If a shorter-named file already exists for that script (e.g. `tests/test_hold.py`), extend it.

Seven `unittest.TestCase` classes with EXACTLY these names, each holding at least one real
assertion (never `pass`, never `assertTrue(True)`):

| Class             | What it proves |
|-------------------|----------------|
| `TestSecurity`    | no hardcoded credentials (regex over source); SQL is parameterized (no f-string SQL with values); inputs sanitized; redline/allowlist honored where the module has one; secrets come from Keychain/fleet store, not source |
| `TestPerformance` | a pure function on ~10k items (or the hot path) stays under a stated bound; no unbounded loop |
| `TestRetry`       | the module's external calls (HTTP, subprocess, LLM, memory server) retry with backoff: mock the call to fail twice then succeed and assert the attempt count. If the module has NO retry on an external call, the test proves it FAILS OPEN (no exception escapes, safe default returned) and carries a `# RETRY GAP: <function>` comment |
| `TestUnit`        | pure functions in isolation: edge cases, empty input, error conditions; call `demo()`/`--selftest` when present |
| `TestIntegration` | composition: shared helpers are imported not re-implemented, the right table / memory source / service_config key is used, two functions chained produce the expected shape |
| `TestFunctional`  | the golden path of `main()`/run with every external mocked, asserting what gets written/posted; plus one error path |
| `TestFrame`       | smoke: `subprocess.run([sys.executable, script, "--selftest" or "--help"], timeout=30)` exits 0 (env `NOVA_TEST_QUIET=1`); importing the module never runs `main()` |

Hard rules
- Offline only: no PostgreSQL, no network, no LLM, no Slack. Mock `psycopg2.connect`, `urllib.request.urlopen`,
  `subprocess.run`, `requests` as needed. Each file runs in under ~15 s.
- Load the module with `importlib.util.spec_from_file_location` from the scripts dir (see
  `tests/test_attention_focus.py`), or plain `import` when the module is import-clean.
- Must be green BOTH ways: `NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_X.py` and
  `python3 tests/test_X.py`.
- Do not change the script under test unless you find a genuine bug; keep that change minimal and say so.
- Docstring header: `"""Tests for <script> — the 7 house categories (Security, Performance, Retry, Unit,
  Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""`

## Pitfalls learned on 2026-10-05 (every one of these bit us)
- Stub every outbound side effect at module load, not just in one test: `nova_config.post_both`, `nova_notify.notify`,
  Slack/Discord senders, `urllib.request.urlopen`, `requests`, `subprocess.run`, SMTP. One unmocked path posted a
  SQL-injection test string to #nova-chat eight times.
- Never assign into `sys.modules` directly; use `monkeypatch.setitem(sys.modules, ...)` or `patch.dict(sys.modules, ...)`
  with the SAME object restored (a leaked MagicMock broke 12 tests in other files). Never `patch.dict("sys.modules")`
  around an import that pulls in Cython extensions (asyncpg) — restore only the keys you set.
- No module-level clocks (`NOW = datetime.now()`) in timing assertions; compute at call time.
- Redirect any `LOG_FILE` / state file / outreach log to a tempdir; tests must leave `~/.openclaw/logs` untouched.
- A script's `--selftest` may reach PG or the LLM; check before using it in TestFrame. Prefer `--help`, else import smoke.
- A test file must pass when run right after any other file: finish by running your whole batch in ONE pytest session.
- Never put a personal address (Jordan's gmail / digitalnoise mail) or a literal home-directory path in a test:
  the pre-push scanner blocks the push. Build probes at runtime (`"kochj23" + "@" + "gmail.com"`, `str(Path.home())`).
