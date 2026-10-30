#!/usr/bin/env python3
"""Apply the local tiddl fix: let ApiError/AuthError accept extra keys.

tiddl's api.py does `raise ApiError(**data)` with Tidal's raw error response.
The constructors only accept the documented keys, so any extra one (timestamp,
path, ...) raises TypeError instead — and tiddl's download loop swallows that
per track, leaving albums half-downloaded with exit code 0. Upstream closed
this as wontfix (oskvr37/tiddl#351), so we add `**kwargs` to both signatures.

`pipx upgrade` / `pipx upgrade-all` reinstall tiddl and drop the patch;
monitor.py checks it every run and alerts on ntfy. Re-run this then.

    python3 scripts/patch_tiddl.py           # patch (idempotent)
    python3 scripts/patch_tiddl.py --check   # exit 1 if not patched
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

TIDDL_PYTHON = os.getenv("TIDDL_PYTHON", str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
SIGNATURE = re.compile(r"(def __init__\(\s*self,[^)]*?)(\s*\):)")


def exceptions_file() -> Path:
    out = subprocess.run([TIDDL_PYTHON, "-c", "import tiddl.exceptions as e; print(e.__file__)"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def patch(source: str) -> str:
    def add_kwargs(m: re.Match) -> str:
        args = m.group(1)
        if "**" in args:
            return m.group(0)
        return args.rstrip().rstrip(",") + ", **kwargs" + m.group(2)
    return SIGNATURE.sub(add_kwargs, source)


def main() -> int:
    try:
        path = exceptions_file()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"tiddl not found via {TIDDL_PYTHON} ({exc}). Install it: pipx install tiddl", file=sys.stderr)
        return 2
    source = path.read_text()
    patched = patch(source)
    if "--check" in sys.argv:
        ok = patched == source
        print(f"{path}: {'patched' if ok else 'NOT patched'}")
        return 0 if ok else 1
    if patched == source:
        print(f"{path}: already patched")
        return 0
    path.write_text(patched)
    print(f"{path}: patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
