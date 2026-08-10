#!/usr/bin/env python3
"""Generate deterministic test audio fixtures. Idempotent — safe to re-run.

Note: silence returns an empty fingerprint from chromaprint (no content to
hash), so we use short sine waves instead. Two files generated from the
same ffmpeg args are audio-identical and used to test the match path; the
third is a different frequency and used to test the no-match path.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent


def gen(name, args):
    out = ROOT / name
    if out.exists():
        return
    subprocess.run(
        ["ffmpeg", "-y", *args, str(out)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# 10s of 440Hz sine — "signal A"
gen("signal_a.flac", ["-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-t", "10"])
# 5s of 440Hz sine — same source signal, shorter. Used to test the
# duration-reject gate (>20% length delta). 5s vs 10s = 50% delta → reject.
gen("signal_a_short.flac", ["-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-t", "5"])
# 10s of 880Hz sine — different audio, must NOT match signal_a
gen("signal_b.flac", ["-f", "lavfi", "-i", "sine=frequency=880:duration=10", "-t", "10"])

print("Fixtures generated in", ROOT)
for f in ROOT.glob("*.flac"):
    print(f"  {f.name} ({f.stat().st_size} bytes)")
