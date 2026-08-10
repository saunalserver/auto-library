"""Shared helpers for dedup subsystem: fingerprinting, comparison, DB ops.

Imported by dedup_scan.py, dedup_state.py, dedup_tool.py, and (via patch)
monitor.py. Keep this module dependency-light so the monitor patch doesn't
pull in heavyweight stuff at runtime.
"""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import struct
import subprocess
import time
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


_PAREN_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

def normalize(text: str | None) -> str:
    """Normalize artist/title for matching.

    - Lowercase
    - Strip parentheticals: (feat. X), (Radio Edit), [Remix]
    - Strip punctuation: JAY-Z == JAY Z
    - Collapse whitespace
    """
    if not text:
        return ""
    text = text.lower()
    text = _PAREN_RE.sub("", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def init_dedup_schema(conn: sqlite3.Connection) -> None:
    """Create dedup tables if missing. Idempotent."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS audio_fingerprints (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath            TEXT UNIQUE NOT NULL,
            artist              TEXT,
            album               TEXT,
            title               TEXT,
            normalized_artist   TEXT NOT NULL,
            normalized_title    TEXT NOT NULL,
            duration_ms         INTEGER,
            fingerprint         TEXT NOT NULL,
            fingerprint_version INTEGER,
            file_size           INTEGER,
            file_mtime          REAL,
            scanned_at          TIMESTAMP NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fp_normalized_artist ON audio_fingerprints(normalized_artist)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fp_normalized_title ON audio_fingerprints(normalized_title)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedup_findings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        TEXT NOT NULL,
            filepath        TEXT NOT NULL,
            artist          TEXT,
            album           TEXT,
            title           TEXT,
            similarity      REAL,
            matched_path    TEXT,
            status          TEXT DEFAULT 'pending',
            size_bytes      INTEGER,
            reviewed_at     TIMESTAMP,
            reviewed_by     TEXT,
            review_note     TEXT,
            added_at        TIMESTAMP NOT NULL,
            UNIQUE(group_id, filepath)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_findings_status ON dedup_findings(status)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedup_protections (
            filepath        TEXT PRIMARY KEY,
            reason          TEXT,
            added_at        TIMESTAMP NOT NULL,
            added_by        TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dedup_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath        TEXT NOT NULL,
            action          TEXT NOT NULL,
            when_at         TIMESTAMP NOT NULL,
            actor           TEXT,
            details         TEXT
        )
    """)
    conn.commit()


def index_file(conn: sqlite3.Connection, path: Path, artist: str, album: str, title: str) -> None:
    """Fingerprint a file and upsert into audio_fingerprints. No-op if mtime unchanged."""
    path = Path(path)
    stat = path.stat()
    cur = conn.cursor()
    cur.execute("SELECT file_mtime FROM audio_fingerprints WHERE filepath = ?", (str(path),))
    row = cur.fetchone()
    if row and abs(row["file_mtime"] - stat.st_mtime) < 1.0:
        return
    fp = fingerprint_file(path)
    cur.execute("""
        INSERT INTO audio_fingerprints
            (filepath, artist, album, title, normalized_artist, normalized_title,
             duration_ms, fingerprint, fingerprint_version, file_size, file_mtime, scanned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(filepath) DO UPDATE SET
            artist=excluded.artist, album=excluded.album, title=excluded.title,
            normalized_artist=excluded.normalized_artist, normalized_title=excluded.normalized_title,
            duration_ms=excluded.duration_ms, fingerprint=excluded.fingerprint,
            fingerprint_version=excluded.fingerprint_version, file_size=excluded.file_size,
            file_mtime=excluded.file_mtime, scanned_at=excluded.scanned_at
    """, (
        str(path), artist, album, title,
        normalize(artist), normalize(title),
        fp.duration_ms, fp.fingerprint_b64, fp.fingerprint_version,
        stat.st_size, stat.st_mtime, time.time(),
    ))
    conn.commit()


def find_existing_fingerprints(
    conn: sqlite3.Connection, normalized_artist: str, normalized_title: str,
) -> list[sqlite3.Row]:
    """Return all fingerprint rows matching the (artist, title) bucket."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM audio_fingerprints "
        "WHERE normalized_artist = ? AND normalized_title = ?",
        (normalized_artist, normalized_title),
    )
    return cur.fetchall()
