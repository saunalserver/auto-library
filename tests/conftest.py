"""Keep the test suite out of the real logs.

musiclib reads LOG_DIR at import time, and the suite exercises the download
path — fake "DUPLICATE DETECTED: Artist - NewAlbum" lines were landing in the
live logs/monitor.log on every run.
"""
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="auto-library-test-logs-"))
