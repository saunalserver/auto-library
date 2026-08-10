import base64
import struct
from pathlib import Path

import pytest

from dedup_lib import fingerprint_file, FingerprintResult

FIXTURES = Path(__file__).parent / "fixtures"

def test_fingerprint_silence_returns_result():
    result = fingerprint_file(FIXTURES / "signal_a.flac")
    assert isinstance(result, FingerprintResult)
    assert 9.0 < result.duration_ms < 11000  # ~10s
    assert result.fingerprint_version >= 1
    assert len(result.raw_ints) > 0

def test_fingerprint_decodes_to_int32_array():
    result = fingerprint_file(FIXTURES / "signal_a.flac")
    # raw_ints is the decoded int32 array
    assert all(isinstance(x, int) for x in result.raw_ints[:10])
    assert all(-2**31 <= x < 2**31 for x in result.raw_ints[:10])

def test_fingerprint_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        fingerprint_file(FIXTURES / "does_not_exist.flac")

def test_fingerprint_silence_and_sine_differ():
    silence = fingerprint_file(FIXTURES / "signal_a.flac")
    sine = fingerprint_file(FIXTURES / "signal_b.flac")
    assert silence.raw_ints != sine.raw_ints

from dedup_lib import compare_fingerprints

def test_compare_identical_returns_1():
    fp = fingerprint_file(FIXTURES / "signal_a.flac")
    assert compare_fingerprints(fp, fp) == 1.0

def test_compare_silence_vs_sine_is_low():
    silence = fingerprint_file(FIXTURES / "signal_a.flac")
    sine = fingerprint_file(FIXTURES / "signal_b.flac")
    sim = compare_fingerprints(silence, sine)
    assert sim < 0.95, f"Expected low similarity, got {sim}"

def test_compare_silence_vs_short_silence_rejects_on_duration():
    # 10s vs 5s = 50% duration delta, exceeds 20% gate -> reject (returns 0.0)
    silence = fingerprint_file(FIXTURES / "signal_a.flac")
    short = fingerprint_file(FIXTURES / "signal_a_short.flac")
    sim = compare_fingerprints(silence, short)
    assert sim == 0.0

def test_compare_is_symmetric():
    silence = fingerprint_file(FIXTURES / "signal_a.flac")
    sine = fingerprint_file(FIXTURES / "signal_b.flac")
    assert abs(compare_fingerprints(silence, sine) - compare_fingerprints(sine, silence)) < 1e-9
