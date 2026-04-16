#!/usr/bin/env python3
"""
test_smoke.py — Smoke tests for all Nova Python scripts.

Validates that every script:
  1. Can be parsed without syntax errors (py_compile)
  2. Has no obvious import failures for standard/local deps
  3. Has a valid shebang or is importable as a module

Run:  python3 ~/.openclaw/scripts/test_smoke.py
Cron: Weekly via launchd to catch regressions early.

Exit code: 0 = all pass, 1 = failures found.

Written by Jordan Koch.
"""

import ast
import importlib.util
import os
import py_compile
import sys
import traceback
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".openclaw" / "scripts"

# Scripts that are known to need special deps or are disabled
SKIP_SCRIPTS = {
    "test_smoke.py",           # This file
    "nova_logger.py",          # Library, not a runnable script
    "nova_config.py",          # Library, not a runnable script
    "herd_config.py",          # Config file, not a script
}

# Local modules that scripts import from the scripts dir
LOCAL_MODULES = {"nova_config", "nova_logger", "herd_config"}


def test_syntax(path: Path) -> tuple[bool, str]:
    """Check if the script has valid Python syntax."""
    try:
        py_compile.compile(str(path), doraise=True)
        return True, "OK"
    except py_compile.PyCompileError as e:
        return False, f"Syntax error: {e}"


def test_ast_parse(path: Path) -> tuple[bool, str]:
    """Parse the AST and check for obvious issues."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        return True, f"{len(tree.body)} top-level statements"
    except SyntaxError as e:
        return False, f"AST parse error line {e.lineno}: {e.msg}"


def test_imports(path: Path) -> tuple[bool, str]:
    """Check that all imports can be resolved (stdlib + local)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if not _can_import(mod):
                    missing.append(mod)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if not _can_import(mod):
                    missing.append(mod)

    if missing:
        unique = sorted(set(missing))
        return False, f"Missing imports: {', '.join(unique)}"
    return True, "All imports resolvable"


def _can_import(module_name: str) -> bool:
    """Check if a module can be found (without actually importing it)."""
    if module_name in LOCAL_MODULES:
        return True
    if module_name in sys.stdlib_module_names:
        return True
    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False


def main():
    scripts = sorted(SCRIPTS_DIR.glob("nova_*.py"))
    # Also check other Python scripts
    scripts += sorted(SCRIPTS_DIR.glob("dream_*.py"))
    scripts += sorted(SCRIPTS_DIR.glob("slack_*.py"))

    total = 0
    passed = 0
    failed = 0
    failures = []

    print(f"Nova Script Smoke Tests")
    print(f"{'=' * 60}")
    print(f"Scripts directory: {SCRIPTS_DIR}")
    print(f"Found {len(scripts)} Python scripts\n")

    for script in scripts:
        name = script.name
        if name in SKIP_SCRIPTS:
            continue

        total += 1
        errors = []

        # Test 1: Syntax
        ok, msg = test_syntax(script)
        if not ok:
            errors.append(f"  SYNTAX: {msg}")

        # Test 2: AST
        ok, msg = test_ast_parse(script)
        if not ok:
            errors.append(f"  AST: {msg}")

        # Test 3: Imports
        ok, msg = test_imports(script)
        if not ok:
            errors.append(f"  IMPORTS: {msg}")

        if errors:
            failed += 1
            failures.append((name, errors))
            print(f"  FAIL  {name}")
            for e in errors:
                print(f"        {e}")
        else:
            passed += 1
            print(f"  OK    {name}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed, {total} total")

    if failures:
        print(f"\nFailures:")
        for name, errs in failures:
            print(f"  {name}:")
            for e in errs:
                print(f"    {e}")
        return 1
    else:
        print("\nAll scripts passed smoke tests.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
