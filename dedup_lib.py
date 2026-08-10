"""Shared helpers for dedup subsystem: fingerprinting, comparison, DB ops.

Imported by dedup_scan.py, dedup_state.py, dedup_tool.py, and (via patch)
monitor.py. Keep this module dependency-light so the monitor patch doesn't
pull in heavyweight stuff at runtime.
"""
from __future__ import annotations

import base64
import json
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FingerprintResult:
    duration_ms: int
    fingerprint_b64: str
    fingerprint_version: int
    raw_ints: tuple[int, ...]


def fingerprint_file(path: Path) -> FingerprintResult:
    """Run fpcalc on a file, return parsed fingerprint. Raises FileNotFoundError
    if path missing, RuntimeError on fpcalc failure."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Not found: {path}")
    proc = subprocess.run(
        ["fpcalc", "-json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fpcalc failed on {path}: {proc.stderr[:200]}")
    data = json.loads(proc.stdout)
    raw_bytes = base64.b64decode(data["fingerprint"])
    # Chromaprint base64 output is prefixed with a single format/version byte
    # (0x01). Strip it so the remaining buffer is 4-byte-aligned for unpacking
    # into int32s. Truncate any trailing partial int defensively.
    if raw_bytes and raw_bytes[0] == 0x01:
        raw_bytes = raw_bytes[1:]
    n_ints = len(raw_bytes) // 4
    raw_bytes = raw_bytes[: n_ints * 4]
    raw_ints = struct.unpack(f">{n_ints}i", raw_bytes)
    return FingerprintResult(
        duration_ms=int(data["duration"] * 1000),
        fingerprint_b64=data["fingerprint"],
        fingerprint_version=int(data.get("version", 2)),
        raw_ints=raw_ints,
    )
