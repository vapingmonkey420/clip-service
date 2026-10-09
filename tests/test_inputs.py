"""Config files, share links and feeds: the places where outside input comes in."""

from datetime import datetime, timezone

import pytest

from clipper import config, feeds, ingest
from clipper.cli import parse_overrides
from clipper.util import ClipperError, clock, slugify

# ------------------------------------------------------------------- clients


def write_client(tmp_path, text: str, name: str = "acme-show"):
    (tmp_path / f"{name}.yml").write_text(text)
    return tmp_path


def test_client_file_is_merged_over_defaults(tmp_path):
    root = write_client(tmp_path, "name: Acme Show\nclips:\n  count: 5\ncaptions:\n  highlight: '#00E5FF'\n")
    cfg = config.load_client("acme-show", root)
    assert cfg["slug"] == "acme-show" and cfg["name"] == "Acme Show"
    assert cfg["clips"]["count"] == 5 and cfg["clips"]["max_seconds"] == 60  # untouched default
    assert cfg["captions"]["highlight"] == "#00E5FF" and cfg["captions"]["font"] == "Poppins"


def test_shipped_client_files_are_valid():
    for slug in config.list_clients():
        config.load_client(slug)
    assert "demo" in config.list_clients() and "_template" not in config.list_clients()


def test_the_template_is_a_valid_client_and_documents_every_setting():
    import yaml

    template = yaml.safe_load((config.CLIENTS_DIR / "_template.yml").read_text())
    config.validate(config.merge(config.DEFAULTS, template), "the template")

    def keys(node, prefix=""):
        for key, value in node.items():
            if isinstance(value, dict) and value and key != "replace":
                yield from keys(value, f"{prefix}{key}.")
            else:
                yield f"{prefix}{key}"

    documented, known = set(keys(template)), set(keys(config.DEFAULTS))
    assert documented <= known, f"template mentions unknown settings: {documented - known}"
    hidden = {"render.preset", "render.crf", "transcribe.beam_size"}  # tuning knobs, deliberately left out
    assert known - documented <= hidden, f"settings missing from the template: {known - documented - hidden}"


def test_problems_are_reported_together_and_in_plain_words(tmp_path):
    root = write_client(tmp_path, "clips:\n  count: 0\n  layout: sideways\ncaptions:\n  highlight: yellow\n")
    with pytest.raises(ClipperError) as error:
        config.load_client("acme-show", root)
    message = str(error.value)
    assert "clips.count" in message and "clips.layout" in message and "captions.highlight" in message


def test_unknown_or_unsafe_client_names(tmp_path):
    with pytest.raises(ClipperError, match="No client file"):
        config.load_client("nobody", tmp_path)
    for bad in ("../secrets", "Acme Show", "", "a/b"):
        with pytest.raises(ClipperError, match="not a valid name"):
            config.load_client(bad, tmp_path)


def test_broken_yaml_is_explained(tmp_path):
    root = write_client(tmp_path, "name: [unclosed\n")
    with pytest.raises(ClipperError, match="not valid YAML"):
        config.load_client("acme-show", root)


def test_overrides_from_the_command_line():
    assert parse_overrides(["clips.count=3", "transcribe.model=tiny.en", "clips.tighten=false"]) == {
        "clips": {"count": 3, "tighten": False},
        "transcribe": {"model": "tiny.en"},
    }
    with pytest.raises(ClipperError):
        parse_overrides(["nonsense"])


# --------------------------------------------------------------------- links


def test_dropbox_links_become_direct_downloads():
    assert ingest.normalize_url("https://www.dropbox.com/scl/fi/abc/ep.mp4?rlkey=k&dl=0") == "https://www.dropbox.com/scl/fi/abc/ep.mp4?rlkey=k&dl=1"
    assert ingest.normalize_url("https://www.dropbox.com/s/abc/ep.mp4").endswith("?dl=1")
    assert ingest.normalize_url("https://cdn.test/ep.mp4?token=1") == "https://cdn.test/ep.mp4?token=1"


def test_google_drive_ids_are_recognised():
    assert ingest.drive_file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing") == "1AbCdEfGhIjKlMnOp"
    assert ingest.drive_file_id("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp") == "1AbCdEfGhIjKlMnOp"
    assert ingest.drive_file_id("https://www.dropbox.com/s/abc/ep.mp4") is None


def test_file_type_is_worked_out_from_name_or_headers():
    assert ingest._extension("https://cdn.test/shows/ep42.MP4?x=1", "application/octet-stream", "") == ".mp4"
    assert ingest._extension("https://cdn.test/download", "video/quicktime", "") == ".mov"
    assert ingest._extension("https://cdn.test/download", "", 'attachment; filename="Episode 42.mp3"') == ".mp3"
    assert ingest._extension("https://cdn.test/download", "", "") == ".media"


def test_only_web_links_count_as_links_and_secrets_stay_out_of_logs():
    assert ingest.is_url("https://x.test/a.mp4") and not ingest.is_url("/home/me/a.mp4") and not ingest.is_url("file:///etc/passwd")
    assert ingest._redact("https://x.test/a.mp4?token=secret") == "https://x.test/a.mp4?…"


def test_missing_local_file_is_a_clear_error(tmp_path):
    with pytest.raises(ClipperError, match="not found"):
        ingest.fetch(str(tmp_path / "nope.mp4"), tmp_path)


# --------------------------------------------------------------------- feeds

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Acme Show</title>
<item><title>Ep 49:  Hiring</title><guid>acme-49</guid><pubDate>Mon, 28 Sep 2026 09:00:00 +0000</pubDate>
  <enclosure url="https://cdn.test/49.mp3" type="audio/mpeg" length="1"/></item>
<item><title>Ep 50: Pricing</title><guid>acme-50</guid><pubDate>Mon, 05 Oct 2026 09:00:00 +0000</pubDate>
  <enclosure url="https://cdn.test/50.mp3" type="audio/mpeg" length="1"/></item>
<item><title>A blog post, not an episode</title><guid>post-1</guid><pubDate>Mon, 05 Oct 2026 10:00:00 +0000</pubDate></item>
<item><title>Ep 1</title><guid>acme-1</guid><pubDate>Mon, 06 Jan 2025 09:00:00 +0000</pubDate>
  <enclosure url="https://cdn.test/1.mp3" type="audio/mpeg" length="1"/></item>
</channel></rss>"""


def test_feed_episodes_newest_first_with_stable_keys():
    episodes = feeds.parse_feed(RSS)
    assert [e["title"] for e in episodes] == ["Ep 50: Pricing", "Ep 49: Hiring", "Ep 1"]
    assert episodes[0]["source"] == "https://cdn.test/50.mp3"
    assert len({e["key"] for e in episodes}) == 3 and all(len(e["key"]) == 16 for e in episodes)
    assert feeds.parse_feed(RSS)[0]["key"] == episodes[0]["key"]


def test_only_fresh_episodes_are_taken():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    fresh = feeds.recent(feeds.parse_feed(RSS), days=7, limit=2, now=now)
    assert [e["title"] for e in fresh] == ["Ep 50: Pricing"]  # Ep 49 is 8 days old; the archive is never back-filled


def test_bad_feed_is_a_clear_error():
    with pytest.raises(ClipperError, match="not valid RSS"):
        feeds.parse_feed(b"<html>Not found</html")


# ---------------------------------------------------------------------- util


def test_small_formatters():
    assert clock(0) == "0:00" and clock(75.4) == "1:15" and clock(3723) == "1:02:03"
    assert slugify("Why I wasted 3 months on gear!") == "why-i-wasted-3-months-on-gear"
    assert slugify("Ünïcödé — and symbols ✨") == "unicode-and-symbols"
    assert slugify("Shows don't have a content problem") == "shows-dont-have-a-content-problem"
    assert slugify("It’s the host’s call") == "its-the-hosts-call"
    assert slugify("") == "clip" and len(slugify("word " * 40)) <= 40


# ------------------------------------------------------------------- streams


def test_a_stream_client_gets_stream_defaults_but_its_file_has_the_last_word(tmp_path):
    root = write_client(tmp_path, "name: Some Body\nkind: stream\ntwitch: '@Some_Body'\nkick: https://kick.com/somebody\npermission: Client agreement\n")
    cfg = config.load_client("acme-show", root)
    assert cfg["kind"] == "stream" and cfg["twitch"] == "some_body" and cfg["kick"] == "somebody"
    assert cfg["clips"]["tighten"] is False and cfg["clips"]["min_seconds"] == 15  # pauses are not cut out of gameplay
    assert cfg["clips"]["count"] == 8 and cfg["stream"]["scan_model"] == "tiny.en"
    root = write_client(tmp_path, "kind: stream\nclips:\n  tighten: true\n  min_seconds: 30\n")
    assert config.load_client("acme-show", root)["clips"] == {**config.DEFAULTS["clips"], "tighten": True, "min_seconds": 30}
    assert config.load_client("demo")["clips"]["tighten"] is True  # an ordinary show is unchanged


def test_stream_settings_are_checked(tmp_path):
    root = write_client(tmp_path, "kind: livestream\ntwitch: two words\nkick: https://www.twitch.tv/wrong_site\nstream:\n  max_hours: 100\n  max_height: 99\nclips:\n  layout: split\n")
    with pytest.raises(ClipperError) as error:
        config.load_client("acme-show", root)
    message = str(error.value)
    for expected in ("kind should be one of show, stream", "twitch: 'two words' does not look like a Twitch channel", "kick:", "stream.max_hours", "stream.max_height"):
        assert expected in message
    assert "clips.layout" not in message  # split is a real layout


def test_the_shipped_stream_client_is_a_stream():
    cfg = config.load_client("demo-stream")
    assert cfg["kind"] == "stream" and cfg["twitch"] == "" and cfg["kick"] == ""  # it watches nothing
