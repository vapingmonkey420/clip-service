"""Get the episode file onto disk and pull out a mono track for transcription."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests

from .util import ClipperError, log, need, probe, run, write_json

MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts", ".flv",
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus",
}  # fmt: skip
MAX_BYTES = int(float(os.environ.get("CLIPPER_MAX_GB", "8")) * 1024**3)
MIN_SECONDS = 30
MAX_SECONDS = 8 * 3600
CHUNK = 1 << 20

NEEDS_FILE_LINK = (
    "Couldn't download the episode from that link. Share the video file itself instead: "
    "a Dropbox or Google Drive link set to 'anyone with the link', or any direct link to "
    "the .mp4/.mov/.mp3. (Links to a YouTube or Spotify page often fail from cloud servers.)"
)


def is_url(source: str) -> bool:
    return bool(re.match(r"^https?://", source or "", re.I))


def normalize_url(url: str) -> str:
    """Turn common share links into direct-download links."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.endswith("dropbox.com"):
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["dl"] = "1"
        return urlunsplit(parts._replace(query=urlencode(query)))
    return url.strip()


def drive_file_id(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.netloc.lower().endswith(("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
        return None
    match = re.search(r"/d/([\w-]{10,})", parts.path)
    if match:
        return match.group(1)
    return dict(parse_qsl(parts.query)).get("id")


def fetch(source: str, workdir: Path) -> Path:
    """Return a local path for `source`, downloading it if it is a link."""
    workdir.mkdir(parents=True, exist_ok=True)
    if not is_url(source):
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ClipperError(f"Episode file not found: {source}")
        return path

    file_id = drive_file_id(source)
    if file_id:
        return _fetch_drive(file_id, workdir)

    url = normalize_url(source)
    path = _fetch_direct(url, workdir)
    if path is None:
        path = _fetch_page(url, workdir)
    return path


def _extension(url: str, content_type: str, disposition: str) -> str:
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.I)
    candidates = [unquote(match.group(1))] if match else []
    candidates.append(unquote(urlsplit(url).path))
    for name in candidates:
        ext = Path(name).suffix.lower()
        if ext in MEDIA_EXTENSIONS:
            return ext
    by_type = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
               "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv"}  # fmt: skip
    return by_type.get((content_type or "").split(";")[0].strip().lower(), ".media")


def _fetch_direct(url: str, workdir: Path) -> Path | None:
    """Stream a file to disk. Returns None when the link is a web page, not a file."""
    log.info("Downloading %s", _redact(url))
    try:
        with requests.get(url, stream=True, timeout=(20, 120), allow_redirects=True,
                          headers={"User-Agent": "clip-service/0.1"}) as response:  # fmt: skip
            if response.status_code in (401, 403, 404):
                raise ClipperError(
                    f"The episode link answered {response.status_code}. Check that it is shared "
                    "with 'anyone with the link' and has not expired."
                )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if content_type.split(";")[0].strip().lower() in ("text/html", "application/xhtml+xml"):
                return None
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_BYTES:
                raise ClipperError(f"The episode file is {declared / 1024**3:.1f} GB; the limit is {MAX_BYTES / 1024**3:.0f} GB.")
            ext = _extension(response.url, content_type, response.headers.get("Content-Disposition", ""))
            path = workdir / f"source{ext}"
            written = 0
            with open(path, "wb") as fh:
                for chunk in response.iter_content(CHUNK):
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ClipperError(f"The episode file is larger than {MAX_BYTES / 1024**3:.0f} GB.")
                    fh.write(chunk)
    except requests.RequestException as exc:
        raise ClipperError(f"Download failed: {type(exc).__name__}. {NEEDS_FILE_LINK}") from None
    log.info("Downloaded %.0f MB", written / 1024**2)
    return path


def _fetch_drive(file_id: str, workdir: Path) -> Path:
    try:
        import gdown
    except ImportError:
        raise ClipperError("Google Drive links need the `gdown` package (pip install gdown).") from None
    log.info("Downloading from Google Drive")
    target = workdir / "source.media"
    try:
        result = gdown.download(id=file_id, output=str(target), quiet=True)
    except Exception as exc:  # gdown raises several unrelated types
        raise ClipperError(
            f"Google Drive refused the download ({type(exc).__name__}). Set the file's sharing to "
            "'anyone with the link' and try again."
        ) from None
    if not result or not target.is_file():
        raise ClipperError("Google Drive did not return a file. Set its sharing to 'anyone with the link'.")
    return target


def _fetch_page(url: str, workdir: Path) -> Path:
    """Last resort for links to a watch page rather than a file."""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise ClipperError(NEEDS_FILE_LINK) from None
    log.info("Link is a web page; trying a page download")
    proc = run(
        [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
         "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
         "--max-filesize", str(MAX_BYTES), "-o", str(workdir / "source.%(ext)s"), url],
        check=False,
        timeout=3 * 3600,
    )  # fmt: skip
    found = sorted(p for p in workdir.glob("source.*") if p.suffix.lower() in MEDIA_EXTENSIONS)
    if proc.returncode != 0 or not found:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        raise ClipperError(f"{NEEDS_FILE_LINK}\nDetail: {detail[0][:300]}")
    return found[0]


def _redact(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(query="…" if parts.query else "", fragment=""))


def prepare(source: str, workdir: Path, max_seconds: float = MAX_SECONDS) -> dict:
    """Fetch the episode, check it, and write audio.wav + probe.json into workdir."""
    need("ffmpeg")
    path = fetch(source, workdir)
    info = describe(path, workdir, max_seconds=max_seconds)
    write_json(workdir / "probe.json", info)
    kind = f"{info['width']}x{info['height']} video" if info["has_video"] else "audio only"
    log.info("Episode ready: %.1f min, %s", info["duration"] / 60, kind)
    return info


def describe(path: Path, workdir: Path, stem: str = "", min_seconds: float = MIN_SECONDS, max_seconds: float = MAX_SECONDS,
             rewrap: bool = False) -> dict:  # fmt: skip
    """Check one media file and put a 16 kHz mono copy of its sound beside it for transcription.

    Returns the file's details plus `path` (the copy to cut clips from) and `audio`.
    `stem` names the working files, so several media files can share a folder.
    `rewrap` insists on a fresh container even for a format that normally seeks well,
    which pieces of a stream joined end to end do not.
    """
    info = probe(path)
    if not info["has_audio"]:
        raise ClipperError("That file has no audio track, so there is nothing to transcribe.")
    if info["duration"] < min_seconds:
        raise ClipperError(f"That file is only {info['duration']:.0f} seconds long; it needs to be at least {min_seconds:.0f}.")
    if info["duration"] > max_seconds:
        raise ClipperError(f"That file is {info['duration'] / 3600:.1f} hours long; the limit is {max_seconds / 3600:.0f} hours.")

    info["original"] = str(path)
    info["path"] = str(make_seekable(path, info, workdir, stem, force=rewrap))

    # first_pts=0 pads a late-starting audio track so transcript times match the video timeline.
    audio = workdir / (f"{stem}.wav" if stem else "audio.wav")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", info["path"], "-map", "0:a:0", "-ac", "1",
         "-af", "aresample=16000:first_pts=0", "-c:a", "pcm_s16le", audio])  # fmt: skip
    info["audio"] = str(audio)
    return info


def make_seekable(path: Path, info: dict, workdir: Path, stem: str = "", force: bool = False) -> Path:
    """Clips are cut by jumping to a timestamp, which mp3, raw aac, .ts and similar cannot do
    precisely. Rewrap those (no re-encoding) into a container with a proper index."""
    indexed = ("mp4", "mov", "matroska", "webm", "wav", "flac", "ogg")
    if not force and any(name in info.get("format", "") for name in indexed):
        return path
    target = workdir / ((stem or "working") + (".mkv" if info["has_video"] else ".mka"))
    maps = ["-map", "0:a:0"] + (["-map", "0:V:0"] if info["has_video"] else [])
    proc = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, *maps, "-c", "copy", target], check=False)
    if proc.returncode != 0 or not target.is_file():
        log.warning("Could not rewrap %s; cuts may be slightly off.", path.name)
        return path
    return target
