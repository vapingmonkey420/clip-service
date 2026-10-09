"""Past broadcasts on Twitch and Kick: finding them and getting at their playlists.

Neither platform offers outsiders an official way to download a broadcast, so
this goes through yt-dlp, the same open-source downloader the rest of the
pipeline falls back on for web pages. It is asked for one thing only: the
broadcast's details and the address of its playlist. Reading the playlist and
fetching media is done in hls.py.

Everything here depends on how the two sites work today. When one of them
changes, the fix is usually a newer yt-dlp (`pip install -U yt-dlp`).
"""

from __future__ import annotations

import hashlib
import itertools
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

from . import hls
from .util import ClipperError, log

TWITCH_HOSTS = ("twitch.tv", "www.twitch.tv", "m.twitch.tv", "go.twitch.tv")
KICK_HOSTS = ("kick.com", "www.kick.com")
# First path words on twitch.tv and kick.com that are pages of the site, not channels.
NOT_CHANNELS = {
    "videos", "video", "directory", "settings", "subscriptions", "inventory", "wallet", "drops", "p", "jobs",
    "downloads", "turbo", "search", "store", "prime", "login", "signup", "user", "friends", "clips", "collections",
    "categories", "browse", "auth", "following", "dashboard", "api", "terms-of-service", "privacy-policy",
}  # fmt: skip
CHANNEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,39}$")
UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")


@dataclass
class Broadcast:
    platform: str  # twitch | kick | hls
    id: str
    url: str  # the page a person would open
    title: str = ""
    channel: str = ""
    duration: float = 0.0  # as the site reports it; the playlist is the final word
    started: float | None = None  # when the stream began, as Unix time
    live: bool = False  # still being recorded
    games: list = field(default_factory=list)  # [(seconds in, name), ...]
    master: str = ""  # playlist address; carries an access token, so never log it
    headers: dict = field(default_factory=dict)
    levels: list = field(default_factory=list)  # the quality levels as yt-dlp listed them, in case the playlist cannot be re-read

    @property
    def key(self) -> str:
        """A short stable name for this broadcast, used to avoid clipping it twice."""
        return f"{self.platform}-{re.sub(r'[^A-Za-z0-9]', '', self.id)[:40]}"

    def public(self) -> dict:
        """Everything except the playlist address, for saving to disk."""
        data = asdict(self)
        data.pop("master"), data.pop("headers"), data.pop("levels")
        return data


# ------------------------------------------------------------- reading links


def classify(url: str) -> tuple[str, str, str] | None:
    """('twitch'|'kick'|'hls', 'vod'|'channel'|'playlist', id or name), or None for any other link."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    words = [word for word in parts.path.split("/") if word]

    if host in TWITCH_HOSTS:
        query = parse_qs(parts.query)
        if query.get("vodID", [""])[0].isdigit():
            return "twitch", "vod", query["vodID"][0]
        if len(words) >= 2 and words[0] == "videos" and words[1].lstrip("v").isdigit():
            return "twitch", "vod", words[1].lstrip("v")
        if len(words) >= 3 and words[1] in ("v", "video", "videos") and words[2].isdigit():
            return "twitch", "vod", words[2]
        if words and words[0].lower() not in NOT_CHANNELS and CHANNEL.match(words[0]) and (len(words) == 1 or words[1] in ("videos", "about", "schedule", "clips")):
            return "twitch", "channel", words[0].lower()
        return None
    if host in KICK_HOSTS:
        if len(words) >= 3 and words[1] == "videos" and UUID.match(words[2].lower()):
            return "kick", "vod", words[2].lower()
        if len(words) >= 2 and words[0] == "video" and UUID.match(words[1].lower()):
            return "kick", "vod", words[1].lower()
        if words and words[0].lower() not in NOT_CHANNELS and CHANNEL.match(words[0]) and (len(words) == 1 or words[1] in ("videos", "about", "clips")):
            return "kick", "channel", words[0].lower()
        return None
    if parts.path.lower().endswith(".m3u8"):
        return "hls", "playlist", hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return None


def is_stream_link(url: str) -> bool:
    return classify(url) is not None


def is_long(source: str, cfg: dict) -> bool:
    """Streams, and anything a client file marks as one, are skimmed first instead of transcribed whole."""
    return is_stream_link(source) or cfg.get("kind") == "stream"


def channel_name(value: str, platform: str) -> str:
    """Accept 'somebody', '@somebody' or a full channel link in a client file."""
    value = (value or "").strip()
    if not value:
        return ""
    found = classify(value if "://" in value else f"https://{platform}.{'tv' if platform == 'twitch' else 'com'}/{value.lstrip('@')}")
    if not found or found[0] != platform or found[1] != "channel":
        raise ClipperError(f"'{value}' does not look like a {platform.title()} channel name.")
    return found[2]


def channel_url(platform: str, channel: str) -> str:
    return f"https://www.twitch.tv/{channel}" if platform == "twitch" else f"https://kick.com/{channel}"


def moment_url(broadcast_url: str, seconds: float) -> str:
    """A link that opens the broadcast at a given moment, where the site supports one."""
    found = classify(broadcast_url)
    if not found or found[:2] != ("twitch", "vod"):
        return ""
    total = max(0, int(seconds))
    return f"https://www.twitch.tv/videos/{found[2]}?t={total // 3600}h{total % 3600 // 60}m{total % 60}s"


# ------------------------------------------------------------------- yt-dlp


class _Quiet:
    """Send the downloader's chatter to our debug log."""

    def debug(self, message):
        log.debug("yt-dlp: %s", message)

    def warning(self, message):
        log.debug("yt-dlp warning: %s", message)

    def error(self, message):
        log.debug("yt-dlp error: %s", message)


def _downloader():
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        raise ClipperError("Twitch and Kick links need the `yt-dlp` package (pip install -r requirements.txt).") from None
    return YoutubeDL({
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "logger": _Quiet(),
        "socket_timeout": 30, "retries": 3, "extractor_retries": 2, "ignore_no_formats_error": True,
    })  # fmt: skip


def _explain(exc: Exception, platform: str, what: str) -> ClipperError:
    """Turn a downloader error into something a person can act on."""
    text = " ".join(str(exc).split())
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r";? please report this issue on .*$", "", text)
    low = text.lower()
    site = platform.title()
    detail = f" (Detail: {text[:200]})" if text else ""
    if any(sign in low for sign in ("proxy", "timed out", "name or service not known", "temporary failure in name", "connection reset", "connection refused")):
        return ClipperError(f"Could not reach {site} from this server. Try again later.{detail}")
    if "subscriber-only" in low or "logged into an account" in low:
        return ClipperError(
            f"{site} only shows this past broadcast to subscribers, so it cannot be fetched. "
            "Ask the streamer to send the file, or to make past broadcasts public."
        )
    if "403" in low or "cloudflare" in low or "impersonat" in low or "captcha" in low:
        return ClipperError(
            f"{site} refused the request from this server, most likely its bot protection. "
            "This comes and goes. Try again later, or have the streamer send the file: both sites let a "
            f"streamer download their own past broadcasts.{detail}"
        )
    if "does not exist" in low or "404" in low or "not found" in low:
        if what == "channel":
            return ClipperError(f"{site} has no channel by that name.{detail}")
        return ClipperError(
            f"{site} has no {what} at that link. Past broadcasts are deleted after a while "
            "(7 to 60 days on Twitch, 7 or 30 on Kick), and only exist at all if the streamer keeps them."
        )
    return ClipperError(f"Could not read the {what} from {site}: {text[:300]}")


def _extract(url: str, platform: str, what: str, take: int | None = None):
    """Ask yt-dlp about a link. With `take`, the link is a listing and its first `take` entries are returned."""
    from yt_dlp.utils import YoutubeDLError

    with _downloader() as ydl:
        try:
            if take is None:
                return ydl.extract_info(url, download=False)
            listing = ydl.extract_info(url, download=False, process=False)
            # The entries are fetched as they are read, so read them while the downloader is still open.
            return [entry for entry in itertools.islice(listing.get("entries") or [], take) if entry]
        except YoutubeDLError as exc:
            raise _explain(exc, platform, what) from None
        except Exception as exc:  # the extractors can fail in ways the library does not wrap
            raise _explain(exc, platform, what) from None


def _kick_api(path: str, name: str):
    """Call Kick's own site API the way yt-dlp does, browser disguise included."""
    from yt_dlp.utils import YoutubeDLError

    with _downloader() as ydl:
        try:
            extractor = ydl.get_info_extractor("KickVOD")
            return extractor._download_json(f"https://kick.com/api/{path}", name, note=False, impersonate=True)
        except YoutubeDLError as exc:
            raise _explain(exc, "kick", "channel") from None
        except Exception as exc:
            raise _explain(exc, "kick", "channel") from None


# ---------------------------------------------------------------- broadcasts


def resolve(url: str) -> Broadcast:
    """Details and playlist address for one past broadcast. A channel link means its latest finished one."""
    found = classify(url)
    if not found:
        raise ClipperError("That is not a Twitch or Kick link.")
    platform, kind, key = found
    if kind == "playlist":
        name = PurePosixPath(urlsplit(url).path).parent.name or "stream"
        return Broadcast(platform="hls", id=key, url=url, title=name, master=url)
    if kind == "vod":
        return _read_broadcast(url, platform, key)
    # A channel: its newest broadcast may be the one going out right now, which Twitch's listing does not flag.
    for item in recent(platform, key, limit=3):
        if item["live"]:
            continue
        broadcast = _read_broadcast(item["url"], platform, item["id"])
        if not broadcast.live:
            return broadcast
    raise ClipperError(
        f"{platform.title()} lists no finished past broadcasts for '{key}'. On Twitch the streamer has to "
        "switch on 'Store past broadcasts'; on both sites they are deleted after a while."
    )


def _read_broadcast(url: str, platform: str, key: str) -> Broadcast:
    info = _extract(url, platform, "past broadcast")
    formats = [f for f in info.get("formats") or [] if str(f.get("protocol", "")).startswith("m3u8")]
    if not formats:
        raise ClipperError(f"{platform.title()} returned no playable version of that broadcast.")
    master = next((f["manifest_url"] for f in formats if f.get("manifest_url")), "") or formats[-1]["url"]
    games = [
        (float(chapter.get("start_time") or 0), str(chapter.get("title") or ""))
        for chapter in info.get("chapters") or []
        if chapter.get("title")
    ] or [(0.0, str(name)) for name in (info.get("categories") or [])[:1] if name]
    return Broadcast(
        platform=platform,
        id=str(info.get("id") or key),
        url=url,
        title=" ".join(str(info.get("title") or "").split()),
        channel=str(info.get("channel") or info.get("uploader_id") or info.get("uploader") or ""),
        duration=float(info.get("duration") or 0),
        started=float(info["timestamp"]) if info.get("timestamp") else None,
        live=bool(info.get("is_live")),
        games=games,
        master=master,
        headers=dict(formats[-1].get("http_headers") or {}),
        levels=[
            {
                "url": f["url"],
                "name": str(f.get("format_id") or ""),
                "bandwidth": int(float(f.get("tbr") or 0) * 1000),
                "width": int(f.get("width") or 0),
                "height": int(f.get("height") or 0),
                "fps": float(f.get("fps") or 0),
                "codecs": ",".join(c for c in (f.get("vcodec"), f.get("acodec")) if c and c != "none"),
            }
            for f in formats
            if f.get("url")
        ],
    )


def open_playlists(broadcast: Broadcast):
    """(session, quality levels) for a broadcast."""
    session = hls.make_session(broadcast.headers)
    try:
        return session, hls.load_master(session, broadcast.master)
    except ClipperError:
        if not broadcast.levels:
            raise
        # The playlist would not open a second time. yt-dlp already read it once, so go by what it saw.
        log.info("The broadcast's playlist could not be re-read; using the quality levels yt-dlp listed.")
        return session, [hls.Variant(**level) for level in broadcast.levels]


def recent(platform: str, channel: str, limit: int = 3) -> list[dict]:
    """A channel's newest past broadcasts, newest first: [{id, url, title, duration, started, live}]."""
    if platform == "twitch":
        entries = _extract(f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time", "twitch", "channel", take=limit)
        found = []
        for entry in entries:
            if not entry.get("url"):
                continue
            found.append({
                "id": str(entry.get("id") or ""), "url": entry["url"], "title": " ".join(str(entry.get("title") or "").split()),
                "duration": float(entry.get("duration") or 0), "started": None, "live": False,
            })  # fmt: skip
        return found
    if platform == "kick":
        listing = _kick_api(f"v2/channels/{channel}/videos", channel)
        if not isinstance(listing, list):
            raise ClipperError("Kick's list of past broadcasts came back in an unexpected shape.")
        found = []
        for item in listing:
            if not isinstance(item, dict):
                continue
            video = item.get("video") if isinstance(item.get("video"), dict) else {}
            uuid = str(video.get("uuid") or item.get("uuid") or "")
            if not UUID.match(uuid):
                continue
            found.append({
                "id": uuid, "url": f"https://kick.com/{channel}/videos/{uuid}",
                "title": " ".join(str(item.get("session_title") or "").split()),
                "duration": float(item.get("duration") or 0) / 1000.0,
                "started": _timestamp(item.get("start_time") or item.get("created_at")),
                "live": bool(item.get("is_live")),
            })  # fmt: skip
        found.sort(key=lambda b: b["started"] or 0, reverse=True)
        return found[:limit]
    raise ClipperError(f"Channels can only be watched on Twitch and Kick, not '{platform}'.")


def viewer_clips(platform: str, channel: str, limit: int = 60) -> list[dict]:
    """Clips viewers made on the channel lately: [{title, views, duration, created}]. Empty on any failure.

    Used only as a hint about where the good moments were, so nothing here is allowed to stop a run.
    """
    try:
        if platform == "twitch":
            entries = _extract(f"https://www.twitch.tv/{channel}/clips?range=7d", "twitch", "channel", take=limit)
            rows = [
                {"title": e.get("title"), "views": e.get("view_count"), "duration": e.get("duration"), "created": e.get("timestamp")}
                for e in entries
            ]  # fmt: skip
        elif platform == "kick":
            listing = _kick_api(f"v2/channels/{channel}/clips", channel)
            rows = [
                {"title": c.get("title"), "views": c.get("views", c.get("view_count")), "duration": c.get("duration"),
                 "created": _timestamp(c.get("created_at"))}
                for c in (listing.get("clips") if isinstance(listing, dict) else None) or [] if isinstance(c, dict)
            ][:limit]  # fmt: skip
        else:
            return []
    except Exception as exc:  # a hint is never worth failing for
        log.info("Viewer clips for %s were not available (%s).", channel, str(exc)[:120])
        return []
    clean = []
    for row in rows:
        try:
            clean.append({
                "title": " ".join(str(row["title"] or "").split())[:120], "views": int(row["views"] or 0),
                "duration": float(row["duration"] or 0), "created": float(row["created"]),
            })  # fmt: skip
        except (TypeError, ValueError):
            continue
    return clean


def clip_hints(clips: list[dict], started: float | None, duration: float) -> list[dict]:
    """Place viewer clips on the broadcast's clock: [{at, views, title}], best first.

    A clip is made a little after the moment it captures, so its place is put
    half a minute before the time it was created. That is a rough guess, which
    is why these are only ever used as hints.
    """
    if not started or not clips:
        return []
    hints = []
    for clip in clips:
        at = clip["created"] - started - 30.0
        if 0 <= at <= duration:
            hints.append({"at": round(at, 1), "views": clip["views"], "title": clip["title"]})
    return sorted(hints, key=lambda h: -h["views"])[:25]


def _timestamp(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text.replace("Z", "+00:00"), text.replace(" ", "T")):
        try:
            stamp = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    return None
