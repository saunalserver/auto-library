"""Comparison engine — populates dedup_findings by comparing fingerprints
within (normalized_artist, normalized_title) buckets."""
from __future__ import annotations

import sqlite3
import time
import uuid
from base64 import b64decode
from collections import defaultdict
from struct import unpack
from typing import Iterable

from dedup_lib import FingerprintResult, compare_fingerprints

SIMILARITY_THRESHOLD = 0.95


def _row_to_fp(row: sqlite3.Row) -> FingerprintResult:
    fp_b64 = row["fingerprint"]
    # Mirror fingerprint_file's decoding: URL-safe → standard alphabet + padding.
    fp_b64 = fp_b64.translate(str.maketrans("-_", "+/"))
    fp_b64 += "=" * (-len(fp_b64) % 4)
    raw_bytes = b64decode(fp_b64)
    # Strip the leading 0x01 format/version byte and truncate any trailing
    # partial int32 so the buffer is exactly 4-byte-aligned for struct.unpack.
    if raw_bytes and raw_bytes[0] == 0x01:
        raw_bytes = raw_bytes[1:]
    n_ints = len(raw_bytes) // 4
    raw_bytes = raw_bytes[: n_ints * 4]
    raw_ints = unpack(f">{n_ints}i", raw_bytes)
    return FingerprintResult(
        duration_ms=row["duration_ms"],
        fingerprint_b64=row["fingerprint"],
        fingerprint_version=row["fingerprint_version"],
        raw_ints=raw_ints,
    )


def compare_library(conn: sqlite3.Connection) -> int:
    """Find audio-identical files and populate dedup_findings.

    For each (normalized_artist, normalized_title) bucket with >1 fingerprint,
    compare pairwise and insert findings for matches above threshold.
    Returns count of new findings inserted.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Group rows by bucket
    cur.execute("""
        SELECT id, filepath, artist, album, title, normalized_artist, normalized_title,
               duration_ms, fingerprint, fingerprint_version, file_size
        FROM audio_fingerprints
        ORDER BY normalized_artist, normalized_title
    """)
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in cur.fetchall():
        buckets[(row["normalized_artist"], row["normalized_title"])].append(row)

    # Load protected paths into a set for fast lookup
    prot_cur = conn.execute("SELECT filepath FROM dedup_protections")
    protected = {r[0] for r in prot_cur.fetchall()}

    # Pairs already recorded (any status except deleted) — the weekly scan must
    # not re-add the same finding under a fresh group_id every Monday.
    known_pairs = {
        (r[0], r[1]) for r in conn.execute(
            "SELECT filepath, matched_path FROM dedup_findings WHERE status != 'deleted'"
        ).fetchall()
    }

    inserted = 0
    now = time.time()
    for (n_artist, n_title), rows in buckets.items():
        if len(rows) < 2:
            continue
        # Compare all pairs
        for i, a in enumerate(rows):
            fp_a = _row_to_fp(a)
            for b in rows[i+1:]:
                fp_b = _row_to_fp(b)
                sim = compare_fingerprints(fp_a, fp_b)
                if sim < SIMILARITY_THRESHOLD:
                    continue
                if (a["filepath"], b["filepath"]) in known_pairs or (b["filepath"], a["filepath"]) in known_pairs:
                    continue
                group_id = str(uuid.uuid4())
                for path_row, matched in [(a, b), (b, a)]:
                    status = "protected" if path_row["filepath"] in protected else "pending"
                    try:
                        conn.execute("""
                            INSERT INTO dedup_findings
                                (group_id, filepath, artist, album, title, similarity,
                                 matched_path, status, size_bytes, added_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            group_id, path_row["filepath"], path_row["artist"],
                            path_row["album"], path_row["title"], sim,
                            matched["filepath"], status, path_row["file_size"], now,
                        ))
                        inserted += 1
                    except sqlite3.IntegrityError:
                        pass  # duplicate (group_id, filepath), skip
    conn.commit()
    return inserted
