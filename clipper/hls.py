"""Read HLS playlists and fetch only the stretches that are needed.

A past broadcast on Twitch or Kick is not one file. It is a playlist naming
thousands of media files a few seconds long. Reading the playlist here means a
stretch from hour five of a stream costs a few dozen small downloads, never the
whole stream, and the place of every file on the stream's clock is known.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .util import ClipperError, log

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
MAX_PLAYLIST_BYTES = 64 * 1024 * 1024
WORKERS = 6


@dataclass
class Variant:
    """One quality level of a stream."""

    url: str
    name: str = ""
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codecs: str = ""

    @property
    def video_codec(self) -> str:
        for code, family in (("avc1", "h264"), ("avc3", "h264"), ("hvc1", "hevc"), ("hev1", "hevc"), ("av01", "av1")):
            if code in self.codecs.lower():
                return family
        return ""

    @property
    def audio_only(self) -> bool:
        if self.codecs:
            return not self.video_codec and "mp4a" in self.codecs.lower()
        return not self.height and "audio" in self.name.lower()


@dataclass
class Piece:
    """One media file of a playlist."""

    url: str
    start: float  # seconds from the start of the stream
    duration: float
    run: int  # goes up by one wherever the recording was interrupted
    init: str = ""  # header file that must precede this piece (fragmented MP4 playlists only)

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Playlist:
    pieces: list[Piece] = field(default_factory=list)
    ended: bool = False  # False while the stream is still being recorded

    @property
    def duration(self) -> float:
        return self.pieces[-1].end if self.pieces else 0.0

    def runs(self) -> list[tuple[float, float]]:
        """(start, end) of each uninterrupted stretch of the recording."""
        spans: dict[int, list[float]] = {}
        for piece in self.pieces:
            span = spans.setdefault(piece.run, [piece.start, piece.end])
            span[1] = piece.end
        return [(a, b) for a, b in spans.values()]

    def between(self, start: float, end: float) -> list[list[Piece]]:
        """The pieces covering [start, end], as one list per uninterrupted stretch.

        Usually that is a single list. Where the recording was interrupted inside
        the span there are two, because pieces from either side of an interruption
        cannot be joined into one playable file.
        """
        groups: dict[int, list[Piece]] = {}
        for piece in self.pieces:
            if piece.start < end and piece.end > start:
                groups.setdefault(piece.run, []).append(piece)
        return [groups[run] for run in sorted(groups)]


# ------------------------------------------------------------------- parsing


def _attributes(text: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in ATTRIBUTE.findall(text)}


def parse_master(text: str, base_url: str) -> list[Variant]:
    """Quality levels listed in a master playlist. A plain media playlist counts as one level."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ClipperError("That link did not return a stream playlist.")
    if any(line.startswith("#EXTINF") for line in lines):
        return [Variant(url=base_url, name="only")]

    names: dict[str, str] = {}
    variants: list[Variant] = []
    pending: dict[str, str] | None = None
    for line in lines:
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _attributes(line.split(":", 1)[1])
            if attrs.get("URI"):
                # Sound kept in a separate playlist from the picture. Neither Twitch nor Kick does this.
                raise ClipperError("This stream keeps its sound and picture in separate playlists, which is not supported.")
            if attrs.get("GROUP-ID"):
                names[attrs["GROUP-ID"]] = attrs.get("NAME", "")
        elif line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attributes(line.split(":", 1)[1])
        elif not line.startswith("#") and pending is not None:
            width, _, height = pending.get("RESOLUTION", "").lower().partition("x")
            group = pending.get("VIDEO", "")
            variants.append(
                Variant(
                    url=urljoin(base_url, line),
                    name=names.get(group) or group,
                    bandwidth=_int(pending.get("BANDWIDTH")),
                    width=_int(width),
                    height=_int(height),
                    fps=_float(pending.get("FRAME-RATE")),
                    codecs=pending.get("CODECS", ""),
                )
            )
            pending = None
    if not variants:
        raise ClipperError("The stream's playlist lists no quality levels.")
    return variants


def parse_media(text: str, base_url: str) -> Playlist:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ClipperError("The stream's media playlist could not be read.")
    playlist = Playlist()
    clock, run, init, length = 0.0, 0, "", None
    for line in lines:
        if line.startswith("#EXTINF:"):
            length = _float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            if playlist.pieces:
                run += 1
        elif line.startswith("#EXT-X-MAP:"):
            header = urljoin(base_url, _attributes(line.split(":", 1)[1]).get("URI", ""))
            if playlist.pieces and header != init and playlist.pieces[-1].run == run:
                run += 1  # a new header means a new, separately playable stretch
            init = header
        elif line.startswith("#EXT-X-KEY:"):
            if _attributes(line.split(":", 1)[1]).get("METHOD", "NONE") != "NONE":
                raise ClipperError("This stream is encrypted, so it cannot be fetched.")
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise ClipperError("This stream packs its pieces into byte ranges, which is not supported.")
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.ended = True
        elif not line.startswith("#") and length is not None:
            playlist.pieces.append(Piece(urljoin(base_url, line), clock, length, run, init))
            clock += length
            length = None
    if not playlist.pieces:
        raise ClipperError("The stream's playlist is empty.")
    return playlist


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ choosing


def scan_variant(variants: list[Variant]) -> Variant:
    """The cheapest level that still carries the sound: sound-only if offered, else the smallest picture."""
    audio = [v for v in variants if v.audio_only]
    if audio:
        return min(audio, key=lambda v: v.bandwidth or 0)
    return min(variants, key=lambda v: (v.bandwidth or 10**12, v.height or 10**6))


def best_variant(variants: list[Variant], max_height: int = 1080) -> Variant:
    """The level to cut clips from: the sharpest picture up to `max_height`, preferring H.264."""
    video = [v for v in variants if not v.audio_only]
    if not video:
        raise ClipperError("This stream has sound but no picture.")
    codec_rank = {"h264": 0, "": 1, "hevc": 2, "av1": 3}
    # Height 0 means the playlist did not say; assume it is the original.
    fits = [v for v in video if (v.height or max_height) <= max_height] or [min(video, key=lambda v: v.height or 10**6)]
    top = max(v.height or max_height for v in fits)
    sharpest = [v for v in fits if (v.height or max_height) == top]
    return min(sharpest, key=lambda v: (codec_rank.get(v.video_codec, 4), -(v.bandwidth or 0)))


# ------------------------------------------------------------------ fetching


def make_session(headers: dict | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    for key, value in (headers or {}).items():
        if key.lower() not in ("accept-encoding", "content-length", "host"):
            session.headers[key] = value
    adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_text(session: requests.Session, url: str) -> tuple[str, str]:
    """Fetch a playlist. Returns (text, final URL after redirects)."""
    response = _get(session, url, what="the stream's playlist")
    if len(response.content) > MAX_PLAYLIST_BYTES:
        raise ClipperError("The stream's playlist is implausibly large.")
    return response.content.decode("utf-8", "replace"), response.url


def load_master(session: requests.Session, url: str) -> list[Variant]:
    text, final = get_text(session, url)
    return parse_master(text, final)


def load_media(session: requests.Session, variant: Variant) -> Playlist:
    text, final = get_text(session, variant.url)
    return parse_media(text, final)


def _get(session: requests.Session, url: str, what: str, attempts: int = 4) -> requests.Response:
    problem = "no answer"
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(8.0, 0.7 * 2**attempt))
        try:
            response = session.get(url, timeout=(15, 60))
        except requests.RequestException as exc:
            problem = type(exc).__name__
            continue
        if response.status_code == 200:
            return response
        problem = f"HTTP {response.status_code}"
        if response.status_code in (401, 403, 404, 410):
            break  # asking again will not change the answer
    raise ClipperError(f"Could not fetch {what} ({problem}) from {redact(url)}.")


def _piece_bytes(session: requests.Session, piece: Piece) -> bytes:
    try:
        return _get(session, piece.url, what="a piece of the stream").content
    except ClipperError:
        # Twitch lists a stretch whose music it muted under a name only the streamer may fetch.
        # The muted copy that everyone else is served sits beside it.
        if "-unmuted" not in piece.url:
            raise
        return _get(session, piece.url.replace("-unmuted", "-muted"), what="a piece of the stream").content


def fetch(session: requests.Session, pieces: list[Piece], target: Path, max_bytes: int = 6 * 1024**3) -> Path:
    """Download `pieces` in order into one file. Returns the path, with .ts or .mp4 chosen to match."""
    if not pieces:
        raise ClipperError("There is nothing in that stretch of the stream to download.")
    if len({p.run for p in pieces}) > 1:
        raise ValueError("pieces from different runs cannot be joined")
    fragmented = bool(pieces[0].init)
    target = target.with_suffix(".mp4" if fragmented else ".ts")
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    batch = WORKERS * 4  # a batch at a time, so only a few pieces are ever held in memory
    with open(target, "wb") as out, ThreadPoolExecutor(max_workers=WORKERS) as pool:
        if fragmented:
            out.write(_get(session, pieces[0].init, what="the stream's header").content)
        for i in range(0, len(pieces), batch):
            # map() hands results back in order while keeping several downloads in flight.
            for data in pool.map(lambda piece: _piece_bytes(session, piece), pieces[i : i + batch]):
                written += len(data)
                if written > max_bytes:
                    raise ClipperError(f"That stretch of the stream is larger than {max_bytes / 1024**3:.0f} GB.")
                out.write(data)
    log.debug("Fetched %d pieces, %.1f MB, into %s", len(pieces), written / 1024**2, target.name)
    return target


def redact(url: str) -> str:
    """A link safe to print: no query string, where access tokens live."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query="…" if parts.query else "", fragment=""))
