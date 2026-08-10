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


def _popcount(x: int) -> int:
    """Number of 1-bits in a 32-bit int."""
    x = x - ((x >> 1) & 0x55555555)
    x = (x & 0x33333333) + ((x >> 2) & 0x33333333)
    x = (x + (x >> 4)) & 0x0F0F0F0F
    return (x * 0x01010101) >> 24

def _hamming_norm(a_ints: tuple[int, ...], b_ints: tuple[int, ...]) -> float:
    """Average bit-error-rate across two equal-length int32 arrays."""
    assert len(a_ints) == len(b_ints)
    total_bits = 32 * len(a_ints)
    diff_bits = sum(_popcount(a ^ b) for a, b in zip(a_ints, b_ints))
    return diff_bits / total_bits

def compare_fingerprints(a: FingerprintResult, b: FingerprintResult) -> float:
    """Sliding-window similarity score in [0.0, 1.0].

    Returns 0.0 if durations differ by >20% (different audio length).
    Otherwise, slides the shorter fingerprint across the longer one and
    returns 1 - min_offset_bit_error_rate.
    """
    a_len, b_len = len(a.raw_ints), len(b.raw_ints)
    if a_len == 0 or b_len == 0:
        return 0.0
    # Duration reject gate (>20% length delta)
    if abs(a_len - b_len) > 0.20 * max(a_len, b_len):
        return 0.0
    shorter = a.raw_ints if a_len <= b_len else b.raw_ints
    longer = b.raw_ints if a_len <= b_len else a.raw_ints
    m = len(shorter)
    n = len(longer)
    best = 1.0
    for offset in range(0, n - m + 1):
        window = longer[offset:offset + m]
        ber = _hamming_norm(shorter, window)
        if ber < best:
            best = ber
    return 1.0 - best
