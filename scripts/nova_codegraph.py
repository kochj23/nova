#!/usr/bin/env python3
"""nova_codegraph.py — a stdlib recreation of "codebase memory" for Python repos.

The idea from the codebase-memory-MCP article, rebuilt with zero external/opaque
binaries: Python's own `ast` parses the repo into a queryable code graph (functions,
classes, imports, call edges) stored in SQLite, so an agent can ask "who calls X /
what does X call / where is X defined / who imports M" with ONE cheap query instead
of grepping + reading whole files. tree-sitter's job — but `ast` is exact for Python
and ships free, and the nova repo is Python.

This is the engine an MCP would wrap (each subcommand = one MCP tool).

Usage:
  nova_codegraph.py index <dir>        # build/refresh the graph
  nova_codegraph.py watch <dir>        # index, then re-index changed files on save
  nova_codegraph.py where   <symbol>   # where it's defined
  nova_codegraph.py callers <symbol>   # who calls it (cross-file)
  nova_codegraph.py callees <symbol>   # what it calls
  nova_codegraph.py importers <module> # who imports it
  nova_codegraph.py stats
  nova_codegraph.py demo               # token/tool-call savings vs file-by-file

The query functions return strings (the MCP wrapper reuses them); __main__ prints.
Written by Jordan Koch (via Claude).
"""
import ast
import os
import sqlite3
import sys
import time
from pathlib import Path

DB = str(Path.home() / ".openclaw/cache/codegraph.db")


def _conn():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB)


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


class Indexer(ast.NodeVisitor):
    def __init__(self, file, cur):
        self.file, self.cur = file, cur
        self.func_stack, self.class_stack = [], []

    def visit_ClassDef(self, n):
        bases = ", ".join(_unparse(b) for b in n.bases)
        self.cur.execute("INSERT INTO symbols VALUES(?,?,?,?,?)",
                         (n.name, "class", self.file, n.lineno, bases))
        self.class_stack.append(n.name); self.generic_visit(n); self.class_stack.pop()

    def _fn(self, n):
        kind = "method" if self.class_stack else "function"
        detail = ",".join(a.arg for a in n.args.args)
        self.cur.execute("INSERT INTO symbols VALUES(?,?,?,?,?)",
                         (n.name, kind, self.file, n.lineno, detail))
        self.func_stack.append(n.name); self.generic_visit(n); self.func_stack.pop()

    visit_FunctionDef = _fn
    visit_AsyncFunctionDef = _fn

    def visit_Call(self, n):
        f = n.func
        callee = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
        if callee:
            caller = self.func_stack[-1] if self.func_stack else "<module>"
            self.cur.execute("INSERT INTO edges VALUES(?,?,?,?)",
                             (caller, self.file, callee, getattr(n, "lineno", 0)))
        self.generic_visit(n)

    def visit_Import(self, n):
        for a in n.names:
            self.cur.execute("INSERT INTO imports VALUES(?,?,?)", (self.file, a.name, a.name))

    def visit_ImportFrom(self, n):
        mod = n.module or ""
        for a in n.names:
            self.cur.execute("INSERT INTO imports VALUES(?,?,?)", (self.file, mod, a.name))


def _scan(root):
    # ponytail: file identity is basename, so two same-named files in different dirs
    # collide. nova's scripts dir is flat enough; key on full path here, store basename.
    for p in Path(root).rglob("*.py"):
        if "/archive/" in str(p) or "/.git/" in str(p):
            continue
        yield p


def _index_one(cur, p):
    """(Re)index a single file: drop its old rows, reparse. Returns True on success."""
    for t in ("symbols", "edges", "imports"):
        cur.execute(f"DELETE FROM {t} WHERE {'caller_file' if t=='edges' else 'file'}=?", (p.name,))
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"), filename=str(p))
        Indexer(p.name, cur).visit(tree)
        return True
    except Exception:
        return False


def index(root):
    t0 = time.time()
    c = _conn()
    c.executescript("""
      DROP TABLE IF EXISTS symbols; DROP TABLE IF EXISTS edges; DROP TABLE IF EXISTS imports;
      CREATE TABLE symbols(name TEXT, kind TEXT, file TEXT, line INT, detail TEXT);
      CREATE TABLE edges(caller TEXT, caller_file TEXT, callee TEXT, line INT);
      CREATE TABLE imports(file TEXT, module TEXT, name TEXT);
    """)
    cur = c.cursor()
    files = nerr = 0
    for p in _scan(root):
        files += 1
        if not _index_one(cur, p):
            nerr += 1
    cur.executescript("""
      CREATE INDEX i_sym ON symbols(name); CREATE INDEX i_cee ON edges(callee);
      CREATE INDEX i_cer ON edges(caller); CREATE INDEX i_imp ON imports(module);
    """)
    c.commit()
    nsym = c.execute("SELECT count(*) FROM symbols").fetchone()[0]
    ned = c.execute("SELECT count(*) FROM edges").fetchone()[0]
    c.close()
    return (f"indexed {files} files ({nerr} parse errors) -> {nsym} symbols, {ned} call edges "
            f"in {time.time()-t0:.2f}s -> {DB}")


def watch(root, interval=1.5):
    print(index(root))
    print(f"watching {root} for *.py changes (every {interval}s, Ctrl-C to stop)...")
    mtimes = {p: p.stat().st_mtime for p in _scan(root)}
    while True:
        time.sleep(interval)
        cur_files = {p: p.stat().st_mtime for p in _scan(root)}
        changed = [p for p, m in cur_files.items() if mtimes.get(p) != m]
        gone = [p for p in mtimes if p not in cur_files]
        if not changed and not gone:
            continue
        c = _conn(); cur = c.cursor()
        for p in gone:
            for t in ("symbols", "edges", "imports"):
                cur.execute(f"DELETE FROM {t} WHERE {'caller_file' if t=='edges' else 'file'}=?", (p.name,))
        ok = sum(_index_one(cur, p) for p in changed)
        c.commit(); c.close()
        mtimes = cur_files
        ts = time.strftime("%H:%M:%S")
        bits = []
        if ok: bits.append(f"{ok} reindexed")
        if len(changed) - ok: bits.append(f"{len(changed)-ok} parse-failed")
        if gone: bits.append(f"{len(gone)} removed")
        print(f"[{ts}] " + ", ".join(bits) + ": " + ", ".join(p.name for p in changed + gone))


def where(sym):
    c = _conn()
    rows = c.execute("SELECT kind, file, line, detail FROM symbols WHERE name=? ORDER BY file", (sym,)).fetchall()
    if not rows:
        return f"no definition for '{sym}'"
    return "\n".join(f"{kind:8} {file}:{line}  ({detail})" for kind, file, line, detail in rows)


def callers(sym):
    c = _conn()
    rows = c.execute("SELECT DISTINCT caller, caller_file, line FROM edges WHERE callee=? ORDER BY caller_file, line", (sym,)).fetchall()
    out = [f"{len(rows)} call sites of '{sym}':"]
    out += [f"  {file}:{line}  in {caller}()" for caller, file, line in rows]
    return "\n".join(out)


def callees(sym):
    c = _conn()
    rows = c.execute("SELECT DISTINCT callee, line FROM edges WHERE caller=? ORDER BY line", (sym,)).fetchall()
    out = [f"{sym}() calls {len(rows)} distinct:"]
    out += [f"  L{line}: {callee}" for callee, line in rows]
    return "\n".join(out)


def importers(mod):
    c = _conn()
    rows = c.execute("SELECT DISTINCT file, name FROM imports WHERE module LIKE ? OR name=? ORDER BY file",
                     (f"%{mod}%", mod)).fetchall()
    out = [f"{len(rows)} files import '{mod}':"]
    out += [f"  {file}  (imports {name})" for file, name in rows]
    return "\n".join(out)


def stats():
    c = _conn()
    return "\n".join([
        f"files: {c.execute('SELECT count(DISTINCT file) FROM symbols').fetchone()[0]}",
        f"symbols: {c.execute('SELECT count(*) FROM symbols').fetchone()[0]} "
        f"{dict(c.execute('SELECT kind,count(*) FROM symbols GROUP BY kind').fetchall())}",
        f"call edges: {c.execute('SELECT count(*) FROM edges').fetchone()[0]}",
        f"most-called: {c.execute('SELECT callee, count(*) c FROM edges GROUP BY callee ORDER BY c DESC LIMIT 8').fetchall()}",
    ])


def demo():
    """Show the token/tool-call win on a real, heavily-used symbol."""
    c = _conn()
    # pick the most-called PROJECT symbol (defined here), not a builtin/stdlib method
    BUILTINS = ('get','log','len','append','print','strip','str','int','format','join','split',
                'dumps','loads','execute','run','open','range','list','dict','set','sorted','enumerate')
    top = c.execute(
        "SELECT callee, count(*) n FROM edges WHERE callee IN (SELECT name FROM symbols) "
        "AND callee NOT IN ({}) AND callee NOT LIKE '\\_\\_%' ESCAPE '\\' "
        "GROUP BY callee ORDER BY n DESC LIMIT 1".format(",".join("?"*len(BUILTINS))), BUILTINS).fetchone()
    sym = top[0] if top else "main"
    sites = c.execute("SELECT DISTINCT caller_file FROM edges WHERE callee=?", (sym,)).fetchall()
    nfiles = len(sites)
    nsites = c.execute("SELECT count(*) FROM edges WHERE callee=?", (sym,)).fetchone()[0]
    graph_bytes = nsites * 45  # ~one short line per call site
    # file-by-file alternative: grep (1 call) + read every file that matches
    avg_file_bytes = 14000
    fbf_bytes = nfiles * avg_file_bytes
    out = [f"Query: 'who calls {sym}()?'",
           f"  code-graph: 1 tool call, {nsites} sites across {nfiles} files, ~{graph_bytes:,} bytes returned",
           f"  file-by-file: ~{1+nfiles} tool calls (grep + read {nfiles} files), ~{fbf_bytes:,} bytes read"]
    if graph_bytes:
        out.append(f"  => ~{fbf_bytes//max(graph_bytes,1)}x fewer bytes, ~{(1+nfiles)//1}x->1 tool calls")
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(0)
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "watch":
        watch(arg or "."); sys.exit(0)
    fn = {"index": lambda: index(arg or "."), "where": lambda: where(arg),
          "callers": lambda: callers(arg), "callees": lambda: callees(arg),
          "importers": lambda: importers(arg), "stats": stats, "demo": demo}.get(cmd)
    print(fn() if fn else "unknown cmd")
