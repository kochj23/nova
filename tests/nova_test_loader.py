"""
nova_test_loader.py — Shared loader for Nova scripts that use Python 3.10+ type syntax.

Many Nova scripts use X | None union type hints which require Python 3.10+.
This loader injects `from __future__ import annotations` to make them load
cleanly on Python 3.9.

Written by Jordan Koch.
"""

import importlib.util
import types
from pathlib import Path


def load_script_compat(script_path, module_name):
    """Load a script that may use Python 3.10+ type hint syntax on Python 3.9+.

    Prepends `from __future__ import annotations` to defer all type hint evaluation,
    which allows X | Y union types to work on Python 3.9.
    """
    src = Path(script_path).read_text()
    # Inject __future__ annotations right after any shebang or encoding comment
    lines = src.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#!") or stripped.startswith("# -*-") or stripped.startswith("# coding"):
            insert_at = i + 1
            continue
        break
    if not any("from __future__ import annotations" in l for l in lines):
        lines.insert(insert_at, "from __future__ import annotations\n")
    src = "".join(lines)

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    mod = types.ModuleType(module_name)
    mod.__spec__ = spec
    mod.__file__ = str(script_path)
    mod.__package__ = ""
    code = compile(src, str(script_path), "exec")
    exec(code, mod.__dict__)
    return mod
