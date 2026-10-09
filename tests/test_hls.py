"""Reading stream playlists and fetching pieces of them. No network: the 'server' here is a dictionary."""

import pytest

from clipper import hls
from clipper.util import ClipperError

BASE = "https://cdn.test/abc_streamer_123/"
TWITCH_MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:ORIGIN="s3",B="false",REGION="NA",CLUSTER="cloudfront_vod"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=8534030,CODECS="avc1.64002A,mp4a.40.2",RESOLUTION=1920x1080,VIDEO="chunked",FRAME-RATE=59.999
chunked/index-dvr.m3u8?token=secret
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="720p60",NAME="720p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=3422999,CODECS="avc1.4D401F,mp4a.40.2",RESOLUTION=1280x720,VIDEO="720p60",FRAME-RATE=59.999
https://other.test/720p60/index-dvr.m3u8
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="audio_only",NAME="Audio Only",AUTOSELECT=NO,DEFAULT=NO
#EXT-X-STREAM-INF:BANDWIDTH=220328,CODECS="mp4a.40.2",VIDEO="audio_only"
audio_only/index-dvr.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-TWITCH-TOTAL-SECS:44.500
#EXTINF:10.000,
0.ts
#EXTINF:10.000,
1-unmuted.ts
#EXTINF:10.000,
2.ts
#EXT-X-DISCONTINUITY
#EXTINF:10.000,
3.ts
#EXTINF:4.500,
4.ts
#EXT-X-ENDLIST
"""


class Reply:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content, self.url = status, content, ""


class FakeSession:
    """Answers GETs from a dictionary of url -> bytes, a status code, or a list of those to hand out in turn."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, timeout=None):
        self.asked.append(url)
        answer = self.pages.get(url, 404)
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        reply = Reply(answer) if isinstance(answer, int) else Reply(200, answer)
        reply.url = url
        return reply


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(hls.time, "sleep", lambda seconds: None)


def test_master_playlist_lists_quality_levels():
    levels = hls.parse_master(TWITCH_MASTER, BASE + "master.m3u8")
    assert [(v.name, v.height, v.audio_only, v.video_codec) for v in levels] == [
        ("1080p60", 1080, False, "h264"), ("720p60", 720, False, "h264"), ("Audio Only", 0, True, ""),
    ]  # fmt: skip
    assert levels[0].url == BASE + "chunked/index-dvr.m3u8?token=secret"  # relative to the master, token kept
    assert levels[1].url == "https://other.test/720p60/index-dvr.m3u8"
    assert levels[0].fps == pytest.approx(59.999) and levels[0].bandwidth == 8534030


def test_a_plain_media_playlist_counts_as_one_level_and_junk_is_refused():
    (only,) = hls.parse_master(MEDIA, BASE + "index.m3u8")
    assert only.url == BASE + "index.m3u8"
    with pytest.raises(ClipperError, match="did not return a stream playlist"):
        hls.parse_master("<html>Sign in</html>", BASE)
    with pytest.raises(ClipperError, match="separate playlists"):
        hls.parse_master('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="English",URI="audio.m3u8"\n', BASE)


def test_the_cheapest_level_is_skimmed_and_the_best_reasonable_one_is_cut_from():
    levels = hls.parse_master(TWITCH_MASTER, BASE)
    assert hls.scan_variant(levels).name == "Audio Only"
    assert hls.best_variant(levels).name == "1080p60"
    assert hls.best_variant(levels, max_height=720).name == "720p60"
    assert hls.best_variant(levels, max_height=360).name == "720p60"  # nothing that small: take the smallest there is

    kick = [hls.Variant("a", height=1080, bandwidth=6_000_000, codecs="avc1.64002A,mp4a.40.2"),
            hls.Variant("b", height=160, bandwidth=300_000, codecs="avc1.4D400C,mp4a.40.2")]  # fmt: skip
    assert hls.scan_variant(kick).url == "b"  # no sound-only level on Kick: the smallest picture stands in

    mixed = [hls.Variant("hevc", height=1080, bandwidth=5_000_000, codecs="hvc1.1.6.L123,mp4a.40.2"),
             hls.Variant("avc", height=1080, bandwidth=6_000_000, codecs="avc1.64002A,mp4a.40.2"),
             hls.Variant("big", height=1440, bandwidth=9_000_000, codecs="hvc1.1.6.L150,mp4a.40.2")]  # fmt: skip
    assert hls.best_variant(mixed).url == "avc"  # same size: the codec everything can read
    with pytest.raises(ClipperError, match="no picture"):
        hls.best_variant([hls.Variant("x", codecs="mp4a.40.2")])


def test_media_playlist_gives_every_piece_a_place_on_the_clock():
    playlist = hls.parse_media(MEDIA, BASE + "chunked/index-dvr.m3u8?token=secret")
    assert playlist.ended and playlist.duration == pytest.approx(44.5)
    assert [(p.start, p.duration, p.run) for p in playlist.pieces] == [(0, 10, 0), (10, 10, 0), (20, 10, 0), (30, 10, 1), (40, 4.5, 1)]
    assert playlist.pieces[0].url == BASE + "chunked/0.ts"
    assert playlist.runs() == [(0.0, 30.0), (30.0, 44.5)]


def test_a_playlist_still_being_written_is_marked_unfinished():
    assert not hls.parse_media(MEDIA.replace("#EXT-X-ENDLIST\n", ""), BASE).ended


def test_a_span_across_an_interruption_comes_back_as_two():
    playlist = hls.parse_media(MEDIA, BASE)
    (one,) = playlist.between(5, 25)
    assert [p.start for p in one] == [0, 10, 20]
    before, after = playlist.between(25, 42)
    assert [p.start for p in before] == [20] and [p.start for p in after] == [30, 40]
    assert playlist.between(100, 110) == []


def test_fragmented_playlists_carry_their_header_and_a_new_header_starts_a_new_run():
    text = """#EXTM3U
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:6.0,
0.mp4
#EXTINF:6.0,
1.mp4
#EXT-X-MAP:URI="init-1.mp4"
#EXTINF:6.0,
2.mp4
#EXT-X-ENDLIST
"""
    playlist = hls.parse_media(text, BASE)
    assert [(p.run, p.init.rsplit("/", 1)[1]) for p in playlist.pieces] == [(0, "init-0.mp4"), (0, "init-0.mp4"), (1, "init-1.mp4")]


def test_streams_that_cannot_be_fetched_piecewise_say_so():
    with pytest.raises(ClipperError, match="encrypted"):
        hls.parse_media('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4,\n0.ts\n', BASE)
    with pytest.raises(ClipperError, match="byte ranges"):
        hls.parse_media("#EXTM3U\n#EXTINF:4,\n#EXT-X-BYTERANGE:100@0\nall.ts\n", BASE)
    with pytest.raises(ClipperError, match="empty"):
        hls.parse_media("#EXTM3U\n#EXT-X-ENDLIST\n", BASE)


def test_pieces_are_joined_in_order(tmp_path):
    playlist = hls.parse_media(MEDIA, BASE)
    session = FakeSession({BASE + "0.ts": b"AAA", BASE + "1-unmuted.ts": 403, BASE + "1-muted.ts": b"BBB", BASE + "2.ts": b"CCC"})
    path = hls.fetch(session, playlist.pieces[:3], tmp_path / "part")
    assert path.name == "part.ts" and path.read_bytes() == b"AAABBBCCC"
    assert BASE + "1-muted.ts" in session.asked  # the public copy of a muted stretch


def test_a_flaky_server_is_asked_again_but_a_missing_piece_is_not(tmp_path):
    playlist = hls.parse_media(MEDIA, BASE)
    session = FakeSession({BASE + "0.ts": [500, 503, b"AAA"]})
    assert hls.fetch(session, playlist.pieces[:1], tmp_path / "a").read_bytes() == b"AAA"
    assert session.asked.count(BASE + "0.ts") == 3
    gone = FakeSession({})
    with pytest.raises(ClipperError, match="HTTP 404"):
        hls.fetch(gone, playlist.pieces[:1], tmp_path / "b")
    assert gone.asked.count(BASE + "0.ts") == 1


def test_fragmented_pieces_get_their_header_first_and_an_mp4_name(tmp_path):
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\n0.mp4\n#EXTINF:6.0,\n1.mp4\n#EXT-X-ENDLIST\n'
    playlist = hls.parse_media(text, BASE)
    session = FakeSession({BASE + "init.mp4": b"HEAD", BASE + "0.mp4": b"00", BASE + "1.mp4": b"11"})
    path = hls.fetch(session, playlist.pieces, tmp_path / "part")
    assert path.suffix == ".mp4" and path.read_bytes() == b"HEAD0011"


def test_fetch_refuses_to_join_across_an_interruption_or_to_fill_the_disk(tmp_path):
    playlist = hls.parse_media(MEDIA, BASE)
    with pytest.raises(ValueError):
        hls.fetch(FakeSession({}), playlist.pieces, tmp_path / "x")
    session = FakeSession({BASE + "0.ts": b"A" * 100})
    with pytest.raises(ClipperError, match="larger than"):
        hls.fetch(session, playlist.pieces[:1], tmp_path / "y", max_bytes=50)
    with pytest.raises(ClipperError, match="nothing in that stretch"):
        hls.fetch(session, [], tmp_path / "z")


def test_many_pieces_keep_their_order(tmp_path):
    text = "#EXTM3U\n" + "".join(f"#EXTINF:2.0,\n{i}.ts\n" for i in range(120)) + "#EXT-X-ENDLIST\n"
    playlist = hls.parse_media(text, BASE)
    session = FakeSession({BASE + f"{i}.ts": f"[{i}]".encode() for i in range(120)})
    assert hls.fetch(session, playlist.pieces, tmp_path / "long").read_bytes() == "".join(f"[{i}]" for i in range(120)).encode()


def test_tokens_stay_out_of_messages():
    assert hls.redact("https://usher.test/vod/1.m3u8?sig=abc&token=xyz") == "https://usher.test/vod/1.m3u8?…"
    with pytest.raises(ClipperError) as error:
        hls.get_text(FakeSession({}), "https://usher.test/vod/1.m3u8?sig=abc&token=xyz")
    assert "token" not in str(error.value) and "sig=" not in str(error.value)
