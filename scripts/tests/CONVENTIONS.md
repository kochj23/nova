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
