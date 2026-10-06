#!/usr/bin/env python3
"""Essay memory fetch on nova-core (2026-08-12..09-30 incident: psql with no -h hit a nonexistent
local socket, returned [], and journal_essay aborted 'Only 0 memories for X' every Wednesday).
Written by Jordan Koch (via Claude). psql is faked through subprocess.run; nothing touches PG."""
import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401  (real module locked in before any stubbing)

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
TMP = Path(tempfile.mkdtemp(prefix="nj_essay_fetch_"))
SOCKET_ERR = ('psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: '
              'No such file or directory\n')


def _stubs():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    iu = types.ModuleType("nova_image_utils"); iu.generate_image = MagicMock(return_value=None)
    oc = types.ModuleType("nova_ops_context")
    oc.get_full_context = lambda hours=24: {}; oc.format_security_brief = lambda c: ""; oc.format_infra_brief = lambda c: ""
    rs = types.ModuleType("nova_resolve"); rs.resolve_url = lambda svc, path="": f"http://127.0.0.1:0{path}"
    return {"nova_notify": nn, "nova_image_utils": iu, "nova_ops_context": oc, "nova_resolve": rs}


def _load():
    spec = importlib.util.spec_from_file_location("nj_essay_fetch", SCRIPTS / "nova_journal.py")
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()), patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")), patch("subprocess.Popen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nj = _load()
nj.LOG_FILE = TMP / "nova_journal.log"


class EssayFetchTests(unittest.TestCase):
    def test_psql_targets_pg_primary_host_not_local_socket(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as r:
            nj.fetch_memories_by_source("geology")
        argv = r.call_args[0][0]
        self.assertEqual(argv[0], "psql")
        self.assertIn("-h", argv)
        self.assertEqual(argv[argv.index("-h") + 1], "pg-primary.digitalnoise.net")

    def test_psql_failure_is_logged_not_silent(self):
        logged = []
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 2, stdout="", stderr=SOCKET_ERR)), \
             patch.object(nj, "log", side_effect=logged.append):
            self.assertEqual(nj.fetch_memories_by_source("geology"), [])
        self.assertTrue(any("psql exit 2" in m and "No such file or directory" in m for m in logged), logged)

    def test_topic_essay_aborts_with_zero_when_fetch_fails(self):
        with patch.object(nj, "get_available_sources", return_value=["geology"]), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 2, stdout="", stderr=SOCKET_ERR)), \
             patch.object(nj, "log"):
            with self.assertRaisesRegex(RuntimeError, "Only 0 memories for geology"):
                nj.topic_essay({})


if __name__ == "__main__":
    unittest.main()
