"""tiddl, but CD-quality tracks come down as FLAC instead of AAC.

Run with tiddl's own venv python, same arguments as the `tiddl` command:
    ~/.local/share/pipx/venvs/tiddl/bin/python tiddl_v2.py url album/123 download

Since early 2026 Tidal's v1 `playbackinfo` endpoint answers a LOSSLESS or
HI_RES_LOSSLESS request for a track without a hi-res master with HIGH (AAC
320) for this client id. tiddl 2.8 saves that as .m4a, and the library ends
up with lossy files. The openapi v2 `trackManifests` endpoint still serves
the same tracks as plain FLAC (DASH, no DRM) to the same token.

So: ask v1 as before; when it says HIGH, ask v2 for FLAC and hand tiddl a
v1-shaped DASH stream. It is labelled HI_RES_LOSSLESS because that is the
tiddl code path that downloads DASH into .m4a, remuxes to .flac with
`ffmpeg -c:a copy` and then writes the FLAC tags. If v2 has no FLAC either
(the track really is AAC-only), tiddl gets the original AAC stream, and the
.m4a is tagged like a FLAC plus SOURCE=Tidal AAC 320 so it stays recognisable
after flac-tagger converts it.
"""
import base64
import logging
import sys

from mutagen.easymp4 import EasyMP4, EasyMP4Tags
from requests_cache import DO_NOT_CACHE

import tiddl.metadata

from tiddl.api import TidalApi
from tiddl.exceptions import ApiError

log = logging.getLogger("tiddl_v2")
V2_URL = "https://openapi.tidal.com/v2/trackManifests/{}"
V2_PARAMS = [("formats", "FLAC"), ("formats", "FLAC_HIRES"), ("manifestType", "MPEG_DASH"),
             ("uriScheme", "DATA"), ("adaptive", "false"), ("usage", "PLAYBACK")]

_v1_stream = TidalApi.getTrackStream


def v2_flac_stream(api, track_id, v1):
    """v1-shaped TrackStream for the v2 FLAC manifest, or None."""
    req = api.session.get(V2_URL.format(track_id), params=V2_PARAMS, expire_after=DO_NOT_CACHE,
                          headers={"Accept": "application/vnd.api+json"})
    if req.status_code != 200:
        log.warning(f"v2 manifest {track_id}: HTTP {req.status_code}")
        return None
    attrs = req.json()["data"]["attributes"]
    uri = attrs.get("uri", "")
    if not any(f.startswith("FLAC") for f in attrs.get("formats", [])) or attrs.get("drmData") \
            or attrs.get("trackPresentation") != "FULL" or not uri.startswith("data:application/dash+xml;base64,"):
        return None
    mpd = base64.b64decode(uri.split(",", 1)[1]).decode()
    if "ContentProtection" in mpd or 'codecs="flac"' not in mpd:
        return None
    bits, rate = 16, 44100
    for part in mpd.split('id="FLAC', 1)[1:]:
        fields = part.split('"', 1)[0].split(",")       # e.g. ',44100,16'
        if len(fields) == 3 and fields[1].isdigit() and fields[2].isdigit():
            rate, bits = int(fields[1]), int(fields[2])
    return v1.model_copy(update={
        "audioQuality": "HI_RES_LOSSLESS",
        "manifestMimeType": "application/dash+xml",
        "manifest": base64.b64encode(mpd.encode()).decode(),
        "bitDepth": bits,
        "sampleRate": rate,
    })


def getTrackStream(self, track_id, quality):
    stream = _v1_stream(self, track_id, quality)
    if stream.audioQuality == "HIGH" and quality in ("LOSSLESS", "HI_RES_LOSSLESS"):
        try:
            flac = v2_flac_stream(self, track_id, stream)
        except Exception as e:
            log.warning(f"v2 manifest {track_id} failed: {e}")
            flac = None
        if flac is not None:
            log.info(f"track {track_id}: v1 offered AAC, using v2 FLAC {flac.bitDepth}/{flac.sampleRate}")
            return flac
        log.info(f"track {track_id}: no FLAC on Tidal, keeping AAC")
    return stream


_add_metadata = tiddl.metadata.addMetadata
EasyMP4Tags.RegisterFreeformKey("isrc", "ISRC")
EasyMP4Tags.RegisterFreeformKey("source", "SOURCE")
AAC_SOURCE = "Tidal AAC 320 (no lossless on Tidal)"


def addMetadata(track_path, track, cover_data=b"", credits=[], album_artist="", lyrics=""):
    """Tag an AAC-only track (.m4a) like a FLAC: flac-tagger's m4a_to_flac.sh copies these
    tags into the .flac it makes. tiddl's own MP4 tags date it `2026-03-25 00:00:00+00:00`
    and use the *track* artist as album artist, which splits the album in Navidrome."""
    _add_metadata(track_path, track, cover_data, credits, album_artist=album_artist, lyrics=lyrics)
    if track_path.suffix != ".m4a":
        return
    tags = EasyMP4(track_path)
    tags["albumartist"] = album_artist or (track.artist.name if track.artist else "")
    tags["title"] = track.title + (f" ({track.version})" if track.version else "")
    if track.streamStartDate:
        tags["date"] = track.streamStartDate.strftime("%Y-%m-%d")
    if track.isrc:
        tags["isrc"] = track.isrc
    tags["source"] = AAC_SOURCE
    tags.save()


def _api_error_init(self, status=0, subStatus="", userMessage="", **_):
    # A 404 for "no lyrics" has neither subStatus nor userMessage; upstream's
    # constructor then raises TypeError instead of the ApiError.
    self.status, self.sub_status, self.user_message = status, subStatus, userMessage


TidalApi.getTrackStream = getTrackStream
tiddl.metadata.addMetadata = addMetadata          # before tiddl.cli imports it
ApiError.__init__ = _api_error_init

if __name__ == "__main__":
    from tiddl.cli import cli
    sys.argv[0] = "tiddl"
    cli()
