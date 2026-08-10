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
