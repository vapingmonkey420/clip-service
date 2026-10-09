"""Twitch and Kick links. The sites themselves are never contacted: yt-dlp's answers are stood in for."""

import pytest

from clipper import streams
from clipper.util import ClipperError

UUID = "5c697a87-afce-4256-b01f-3c8fe71ef5cb"
# The shape yt-dlp returns for a Twitch past broadcast (fields this code reads, plus one it must ignore).
TWITCH_VOD = {
    "id": "v2345678901",
    "title": "ranked grind  day 12",
    "uploader": "Some_Body",
    "uploader_id": "some_body",
    "duration": 21480,
    "timestamp": 1790000000,
    "is_live": False,
    "chapters": [{"start_time": 0, "end_time": 3600, "title": "Just Chatting"}, {"start_time": 3600, "end_time": 21480, "title": "VALORANT"}],
    "formats": [
        {"format_id": "sb0", "protocol": "mhtml", "url": "https://x.test/storyboard"},
        {"format_id": "Audio_Only", "protocol": "m3u8_native", "url": "https://cdn.test/a/audio_only/index-dvr.m3u8",
         "manifest_url": "https://usher.test/vod/2345678901.m3u8?sig=s&token=t", "http_headers": {"User-Agent": "UA"}},
        {"format_id": "1080p60", "protocol": "m3u8_native", "url": "https://cdn.test/a/chunked/index-dvr.m3u8",
         "manifest_url": "https://usher.test/vod/2345678901.m3u8?sig=s&token=t", "http_headers": {"User-Agent": "UA"}},
    ],
}  # fmt: skip


def test_links_are_recognised():
    cases = {
        "https://www.twitch.tv/videos/2345678901": ("twitch", "vod", "2345678901"),
        "https://www.twitch.tv/videos/2345678901?t=1h2m3s": ("twitch", "vod", "2345678901"),
        "https://twitch.tv/some_body/video/2345678901": ("twitch", "vod", "2345678901"),
        "https://www.twitch.tv/some_body/schedule?vodID=1822395420": ("twitch", "vod", "1822395420"),
        "https://www.twitch.tv/Some_Body": ("twitch", "channel", "some_body"),
        "https://m.twitch.tv/some_body/videos": ("twitch", "channel", "some_body"),
        f"https://kick.com/xqc/videos/{UUID}": ("kick", "vod", UUID),
        f"https://kick.com/video/{UUID}": ("kick", "vod", UUID),
        "https://kick.com/a-log-burner": ("kick", "channel", "a-log-burner"),
        "https://www.twitch.tv/directory": None,
        "https://www.twitch.tv/videos": None,
        "https://kick.com/categories": None,
        "https://clips.twitch.tv/SomeClip": None,
        "https://www.dropbox.com/s/abc/ep.mp4": None,
        "https://nottwitch.tv/some_body": None,
        "file:///etc/passwd": None,
        "not a link": None,
        "": None,
    }
    for link, expected in cases.items():
        assert streams.classify(link) == expected, link
    kind = streams.classify("https://cdn.test/vod/master.m3u8?token=1")
    assert kind[:2] == ("hls", "playlist") and len(kind[2]) == 12


def test_what_counts_as_long(cfg):
    assert streams.is_long("https://www.twitch.tv/videos/2345678901", cfg)  # a stream link always does
    assert not streams.is_long("https://www.dropbox.com/s/abc/ep.mp4", cfg)
    assert streams.is_long("https://www.dropbox.com/s/abc/ep.mp4", {**cfg, "kind": "stream"})


def test_channel_names_in_client_files():
    assert streams.channel_name("Some_Body", "twitch") == "some_body"
    assert streams.channel_name("@some_body", "twitch") == "some_body"
    assert streams.channel_name("https://kick.com/xqc", "kick") == "xqc"
    assert streams.channel_name("", "kick") == ""
    for bad in ("two words", f"https://kick.com/xqc/videos/{UUID}", "https://www.twitch.tv/some_body"):
        with pytest.raises(ClipperError, match="does not look like a Kick channel"):
            streams.channel_name(bad, "kick")
    assert streams.channel_url("twitch", "some_body") == "https://www.twitch.tv/some_body"


def test_a_link_to_the_moment_only_where_the_site_has_one():
    assert streams.moment_url("https://www.twitch.tv/videos/2345678901", 3723.9) == "https://www.twitch.tv/videos/2345678901?t=1h2m3s"
    assert streams.moment_url(f"https://kick.com/xqc/videos/{UUID}", 60) == ""
    assert streams.moment_url("https://www.dropbox.com/s/abc/ep.mp4", 60) == ""


def test_a_twitch_broadcast_is_read_from_what_yt_dlp_returns(monkeypatch):
    asked = []
    monkeypatch.setattr(streams, "_extract", lambda url, platform, what, take=None: asked.append(url) or TWITCH_VOD)
    found = streams.resolve("https://www.twitch.tv/videos/2345678901?t=10s")
    assert asked == ["https://www.twitch.tv/videos/2345678901?t=10s"]
    assert (found.platform, found.id, found.channel, found.duration, found.live) == ("twitch", "v2345678901", "some_body", 21480.0, False)
    assert found.title == "ranked grind day 12" and found.started == 1790000000.0
    assert found.master == "https://usher.test/vod/2345678901.m3u8?sig=s&token=t" and found.headers == {"User-Agent": "UA"}
    assert found.games == [(0.0, "Just Chatting"), (3600.0, "VALORANT")]
    assert found.key == "twitch-v2345678901"
    saved = found.public()
    assert "master" not in saved and "headers" not in saved  # the playlist address carries a token


def test_quality_levels_can_come_from_yt_dlp_if_the_playlist_will_not_open_again(monkeypatch):
    from clipper import hls

    monkeypatch.setattr(streams, "_extract", lambda *a, **k: {**TWITCH_VOD, "formats": [
        {"format_id": "Audio_Only", "protocol": "m3u8_native", "url": "https://cdn.test/a/audio_only/index-dvr.m3u8", "vcodec": "none",
         "acodec": "mp4a.40.2", "tbr": 160.0, "manifest_url": "https://usher.test/vod/1.m3u8?sig=s"},
        {"format_id": "1080p60", "protocol": "m3u8_native", "url": "https://cdn.test/a/chunked/index-dvr.m3u8", "vcodec": "avc1.64002A",
         "acodec": "mp4a.40.2", "tbr": 8534.0, "height": 1080, "width": 1920, "fps": 60, "manifest_url": "https://usher.test/vod/1.m3u8?sig=s"},
    ]})  # fmt: skip
    found = streams.resolve("https://www.twitch.tv/videos/2345678901")

    def refuse(session, url):
        raise ClipperError("Could not fetch the stream's playlist (HTTP 403)")

    monkeypatch.setattr(hls, "load_master", refuse)
    _, levels = streams.open_playlists(found)
    assert hls.scan_variant(levels).url.endswith("audio_only/index-dvr.m3u8") and hls.scan_variant(levels).audio_only
    best = hls.best_variant(levels)
    assert (best.name, best.height, best.video_codec, best.bandwidth) == ("1080p60", 1080, "h264", 8534000)
    assert "levels" not in found.public()
    with pytest.raises(ClipperError):  # a bare playlist link has nothing to fall back on
        streams.open_playlists(streams.resolve("https://cdn.test/vod/master.m3u8"))


def test_a_broadcast_still_being_recorded_is_flagged(monkeypatch):
    monkeypatch.setattr(streams, "_extract", lambda *a, **k: {**TWITCH_VOD, "is_live": True})
    assert streams.resolve("https://www.twitch.tv/videos/2345678901").live


def test_a_broadcast_with_nothing_playable_is_an_error(monkeypatch):
    monkeypatch.setattr(streams, "_extract", lambda *a, **k: {**TWITCH_VOD, "formats": TWITCH_VOD["formats"][:1]})
    with pytest.raises(ClipperError, match="no playable version"):
        streams.resolve("https://www.twitch.tv/videos/2345678901")


def test_a_playlist_link_needs_no_lookup(monkeypatch):
    monkeypatch.setattr(streams, "_extract", lambda *a, **k: pytest.fail("should not be called"))
    found = streams.resolve("https://cdn.test/shows/episode-9/master.m3u8")
    assert (found.platform, found.title, found.master) == ("hls", "episode-9", "https://cdn.test/shows/episode-9/master.m3u8")
    with pytest.raises(ClipperError, match="not a Twitch or Kick link"):
        streams.resolve("https://www.dropbox.com/s/abc/ep.mp4")


def test_twitch_channel_listing(monkeypatch):
    def fake(url, platform, what, take=None):
        assert url == "https://www.twitch.tv/some_body/videos?filter=archives&sort=time" and take == 2
        return [{"id": "v9", "url": "https://www.twitch.tv/videos/9", "title": "late  stream", "duration": 7200.0},
                {"id": "v8", "url": "https://www.twitch.tv/videos/8", "title": None, "duration": None}]  # fmt: skip

    monkeypatch.setattr(streams, "_extract", fake)
    assert streams.recent("twitch", "some_body", limit=2) == [
        {"id": "v9", "url": "https://www.twitch.tv/videos/9", "title": "late stream", "duration": 7200.0, "started": None, "live": False},
        {"id": "v8", "url": "https://www.twitch.tv/videos/8", "title": "", "duration": 0.0, "started": None, "live": False},
    ]


def test_kick_channel_listing(monkeypatch):
    listing = [
        {"session_title": "older", "duration": 3_600_000, "start_time": "2026-10-04 20:00:00", "is_live": False, "video": {"uuid": UUID}},
        {"session_title": "newest, still going", "duration": 0, "start_time": "2026-10-06 01:00:00", "is_live": True,
         "video": {"uuid": UUID.replace("5c", "6d")}},
        {"session_title": "no video id", "video": {}},
        "junk",
    ]  # fmt: skip
    monkeypatch.setattr(streams, "_kick_api", lambda path, name: listing if path == "v2/channels/xqc/videos" else pytest.fail(path))
    found = streams.recent("kick", "xqc", limit=5)
    assert [b["title"] for b in found] == ["newest, still going", "older"]  # newest first, unusable rows dropped
    assert found[0]["live"] and found[1]["duration"] == 3600.0
    assert found[1]["url"] == f"https://kick.com/xqc/videos/{UUID}" and found[1]["started"] == pytest.approx(1791144000.0)
    monkeypatch.setattr(streams, "_kick_api", lambda path, name: {"message": "nope"})
    with pytest.raises(ClipperError, match="unexpected shape"):
        streams.recent("kick", "xqc")


def test_a_channel_link_means_its_latest_finished_broadcast(monkeypatch):
    # Kick's listing says which one is live. Twitch's does not, so the newest has to be opened to find out.
    monkeypatch.setattr(streams, "recent", lambda platform, channel, limit=3: [
        {"id": "v10", "url": "https://www.twitch.tv/videos/10", "live": True},
        {"id": "v9", "url": "https://www.twitch.tv/videos/9", "live": False},
        {"id": "v8", "url": "https://www.twitch.tv/videos/8", "live": False},
    ])  # fmt: skip
    asked = []

    def fake(url, platform, what, take=None):
        asked.append(url)
        return {**TWITCH_VOD, "id": "v" + url.rsplit("/", 1)[1], "is_live": url.endswith("/9")}

    monkeypatch.setattr(streams, "_extract", fake)
    found = streams.resolve("https://www.twitch.tv/some_body")
    assert asked == ["https://www.twitch.tv/videos/9", "https://www.twitch.tv/videos/8"]  # 10 flagged live, 9 found to be
    assert found.id == "v8" and found.url == "https://www.twitch.tv/videos/8"
    monkeypatch.setattr(streams, "recent", lambda platform, channel, limit=3: [])
    with pytest.raises(ClipperError, match="no finished past broadcasts"):
        streams.resolve("https://www.twitch.tv/some_body")


def test_viewer_clips_are_hints_and_never_stop_a_run(monkeypatch):
    def broken(*args, **kwargs):
        raise ClipperError("Twitch refused")

    monkeypatch.setattr(streams, "_extract", broken)
    assert streams.viewer_clips("twitch", "some_body") == []
    monkeypatch.setattr(streams, "_extract", lambda *a, **k: [
        {"title": "he  did not see me", "view_count": 412, "duration": 28.0, "timestamp": 1790003000},
        {"title": "no timestamp", "view_count": 5, "duration": 10.0, "timestamp": None},
    ])  # fmt: skip
    assert streams.viewer_clips("twitch", "some_body") == [{"title": "he did not see me", "views": 412, "duration": 28.0, "created": 1790003000.0}]
    monkeypatch.setattr(streams, "_kick_api", lambda path, name: {"clips": [{"title": "W", "views": 9, "duration": 20, "created_at": "2026-10-06T01:00:00Z"}]})
    assert streams.viewer_clips("kick", "xqc")[0]["views"] == 9


def test_viewer_clips_are_placed_on_the_broadcast_clock():
    clips = [{"title": "big", "views": 400, "duration": 30, "created": 1000 + 600 + 30},
             {"title": "small", "views": 3, "duration": 30, "created": 1000 + 120 + 30},
             {"title": "made the next day from the recording", "views": 50, "duration": 30, "created": 1000 + 90000},
             {"title": "from an earlier stream", "views": 70, "duration": 30, "created": 400}]  # fmt: skip
    assert streams.clip_hints(clips, started=1000, duration=7200) == [
        {"at": 600.0, "views": 400, "title": "big"}, {"at": 120.0, "views": 3, "title": "small"},
    ]  # fmt: skip
    assert streams.clip_hints(clips, started=None, duration=7200) == []  # without a start time nothing can be placed


def test_failures_are_explained_in_plain_words():
    explain = streams._explain
    assert "subscribers" in str(explain(Exception("You must be logged into an account that has access to this subscriber-only content"), "twitch", "past broadcast"))
    refused = str(explain(Exception("ERROR: [kick:vod] x: Unable to download JSON metadata: HTTP Error 403: Forbidden; please report this issue on https://github.com/yt-dlp"), "kick", "past broadcast"))
    assert "bot protection" in refused and "please report" not in refused and "download their own" in refused
    assert "deleted after a while" in str(explain(Exception("Video 123 does not exist"), "twitch", "past broadcast"))
    assert "no channel by that name" in str(explain(Exception('Channel "nobody" not found'), "twitch", "channel"))
    assert "Could not reach Twitch" in str(explain(Exception("Unable to connect to proxy: Tunnel connection failed: 403 Forbidden"), "twitch", "channel"))
    assert "Could not read the past broadcast from Kick: something odd" in str(explain(Exception("something odd"), "kick", "past broadcast"))


def test_times_from_either_site():
    assert streams._timestamp(1790000000) == 1790000000.0
    assert streams._timestamp("2026-10-06T01:00:00Z") == streams._timestamp("2026-10-06 01:00:00") == streams._timestamp("2026-10-06T01:00:00+00:00")
    assert streams._timestamp("") is None and streams._timestamp("yesterday") is None and streams._timestamp(None) is None
