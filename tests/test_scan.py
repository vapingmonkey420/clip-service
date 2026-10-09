"""The first pass over a long recording: loudness, rough transcript, and the shortlist."""

import json
import sys
import types

import numpy as np
import pytest
from conftest import write_wav

from clipper import pick, scan
from clipper.scan import Line
from clipper.util import ClipperError


def stream_lines() -> list[Line]:
    """Ten minutes of a made-up stream: mostly filler, one story, one big moment."""
    rows = [
        (5, 9, "Alright chat we are live give me a second to get set up."),
        (60, 64, "Thanks for the follow welcome in."),
        (120, 124, "Loading into the next match now."),
        (200, 205, "So I have to tell you what happened yesterday it is a crazy story."),
        (205, 211, "I queued into ranked and my teammate was nine years old!"),
        (211, 217, "He carried the whole lobby and honestly I got outplayed by a child."),
        (300, 303, "Checking the map."),
        (400, 404, "Wait wait wait it is one versus four."),
        (404, 409, "No way! No way! He did not see me!"),
        (409, 414, "That is the best round I have ever played! Somebody clip that!"),
        (500, 503, "Let me fix my settings quickly."),
        (580, 584, "Alright that is the stream see you tomorrow."),
    ]
    return [Line(i, float(a), float(b), text) for i, (a, b, text) in enumerate(rows)]


META = {"title": "ranked grind", "duration": 600.0, "games": [(0.0, "Just Chatting"), (100.0, "VALORANT")]}

# ----------------------------------------------------------------- loudness


def test_a_shout_stands_out_but_ordinary_talking_does_not(tmp_path):
    wav = tmp_path / "a.wav"
    talk = [(t, t + 8, 2500) for t in range(10, 290, 20)]  # steady talking on and off for five minutes
    write_wav(wav, [*talk, (150, 156, 9000)], 300, floor=60)  # with one shout in the middle
    levels = scan.loudness(wav)
    assert levels.size == 300
    (event,) = scan.loud_moments(levels)
    assert 149 <= event["at"] <= 151 and 155 <= event["end"] <= 157 and event["lift"] > 6


def test_nothing_is_loud_in_an_even_recording_or_a_very_short_one(tmp_path):
    wav = tmp_path / "even.wav"
    write_wav(wav, [(0, 200, 3000)], 200, floor=60)
    assert scan.loud_moments(scan.loudness(wav)) == []
    assert scan.loud_moments(np.array([-30.0] * 20, dtype=np.float32)) == []
    assert scan.loudness(tmp_path / "even.wav").dtype == np.float32


def test_only_the_loudest_few_moments_are_kept_and_they_are_spread_out():
    levels = np.full(3000, -40.0, dtype=np.float32)
    levels[::7] = -22.0  # ordinary talking
    for start in range(100, 2900, 100):
        levels[start : start + 4] = -8.0
    found = scan.loud_moments(levels, limit=5)
    assert len(found) == 5 and all(b["at"] - a["at"] >= 20 for a, b in zip(found, found[1:]))


# ---------------------------------------------------------- rough transcript


class FakeSegment:
    def __init__(self, start, end, text, avg_logprob=-0.3, no_speech_prob=0.1):
        self.start, self.end, self.text = start, end, text
        self.avg_logprob, self.no_speech_prob = avg_logprob, no_speech_prob


def fake_whisper(monkeypatch, replies):
    """Stand in for the speech library: each call to transcribe() pops the next list of segments."""
    calls = []

    class Model:
        def __init__(self, name, **kwargs):
            calls.append(("load", name))

        def transcribe(self, samples, **options):
            calls.append(("transcribe", len(samples), options))
            return iter(replies.pop(0) if replies else []), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    return calls


def test_the_skim_is_done_in_chunks_and_put_back_on_one_clock(tmp_path, monkeypatch):
    wav = tmp_path / "long.wav"
    write_wav(wav, [(2, 8), (40, 46), (70, 76)], 90)
    monkeypatch.setattr(scan, "CHUNK", 30)
    calls = fake_whisper(monkeypatch, [
        [FakeSegment(1.0, 8.0, " Hello  chat we are live. ")],
        [FakeSegment(0.5, 16.0, "Wait no way."), FakeSegment(16.0, 17.0, "Wait no way."), FakeSegment(18.0, 20.0, "Thank you.", avg_logprob=-1.6)],
        [FakeSegment(9.0, 15.5, "That was the best round I have played.")],
    ])  # fmt: skip
    lines = scan.rough_transcript(wav, {"scan_model": "tiny.en"}, "en")
    assert calls[0] == ("load", "tiny.en")
    assert [c[1] for c in calls[1:]] == [30 * 16000] * 3
    assert calls[1][2]["beam_size"] == 1 and calls[1][2]["word_timestamps"] is False and calls[1][2]["vad_filter"] is True
    assert [line.text for line in lines] == ["Hello chat we are live.", "Wait no way.", "That was the best round I have played."]
    assert lines[0].end == 8.0 and lines[1].end == 46.0 and lines[2].end == 75.5  # each chunk's times moved to its place
    # The second phrase was reported as 15 seconds long. Three words cannot take that long, so its start is pulled in.
    assert 46.0 - lines[1].start == pytest.approx(0.45 * 3 + 1.5)
    assert [line.index for line in lines] == [0, 1, 2]


def test_the_skim_can_stop_early_and_ends_chunks_in_quiet(tmp_path, monkeypatch):
    wav = tmp_path / "long.wav"
    write_wav(wav, [(0, 24), (27, 60)], 60)  # quiet from 24s to 27s
    monkeypatch.setattr(scan, "CHUNK", 30)
    calls = fake_whisper(monkeypatch, [])
    scan.rough_transcript(wav, {"scan_model": "tiny.en"}, "en", per_second=scan.loudness(wav), max_seconds=45)
    lengths = [c[1] / 16000 for c in calls[1:]]
    assert 24 <= lengths[0] <= 27.5  # the first chunk ends in the quiet stretch, not mid-word at 30s
    assert sum(lengths) == pytest.approx(45)  # and nothing past the limit is looked at


def test_a_supplied_transcript_can_stand_in_for_the_skim(talk):
    lines = scan.lines_from_transcript(talk)
    assert len(lines) == len(talk.sentences) and lines[2].text.startswith("Here is the thing")


# ------------------------------------------------------------------ shortlist


def test_marks_show_where_it_got_loud_and_where_viewers_clipped():
    lines = stream_lines()
    marks = scan.tags(lines, [{"at": 403.0, "end": 412.0, "lift": 9.0}], [{"at": 208.0, "views": 400, "title": "nine year old"}, {"at": 230.0, "views": 12, "title": "lol"}])
    assert marks[7] == marks[8] == marks[9] == "{LOUD}"
    assert marks[4] == "{CLIPPED 400}" and marks[5] == "{CLIPPED 12}"  # each clip goes to the nearest phrase
    assert 0 not in marks and scan.tags(lines, [], [{"at": 20000.0, "views": 5, "title": "far from any speech"}]) == {}


def test_the_request_carries_the_brief_the_marks_and_what_was_on(cfg):
    lines = stream_lines()
    marks = scan.tags(lines, [{"at": 403.0, "end": 412.0, "lift": 9.0}], [])
    text = scan.build_request(lines, lines, marks, {**cfg, "about": "A ranked shooter streamer"}, {**META, "notes": "skip the intro"}, 6)
    assert "Show: Test Show" in text and "About: A ranked shooter streamer" in text
    assert "Shortlist up to 6 stretches" in text and "20 to 60 seconds" in text and "Length: 10:00" in text
    assert "What was on, by time: 0:00 Just Chatting; 1:40 VALORANT" in text and "Editor's notes for this recording: skip the intro" in text
    assert "[8] 6:44 No way! No way! He did not see me! {LOUD}" in text
    assert "[0] 0:05 Alright chat" in text and "{LOUD}" not in text.split("[0]")[1].split("\n")[0]


def test_claudes_reply_becomes_stretches_and_nonsense_is_dropped():
    lines = stream_lines()
    reply = json.dumps({"windows": [
        {"first": 3, "last": 5, "reason": "Story about a nine year old  teammate.", "score": 80},
        {"first": 7, "last": 9, "reason": "One versus four clutch.", "score": 95},
        {"first": 9, "last": 7, "reason": "backwards", "score": 50},
        {"first": 3, "last": 99, "reason": "out of range", "score": 50},
        {"first": "x", "last": 2, "reason": "not a number", "score": 50},
        "junk",
    ]})  # fmt: skip
    found = scan.parse_windows(reply, lines)
    assert [(w["start"], w["end"], w["score"]) for w in found] == [(200.0, 217.0, 80.0), (400.0, 414.0, 95.0)]
    assert found[0]["why"] == "Story about a nine year old teammate."
    (long_one,) = scan.parse_windows({"windows": [{"first": 0, "last": 11, "reason": "everything", "score": 10}]}, lines)
    assert long_one["end"] - long_one["start"] == scan.MAX_WINDOW  # a runaway stretch is cut down


def test_stretches_are_padded_kept_inside_the_recording_and_merged(cfg):
    raw = [{"start": 400.0, "end": 414.0, "why": "clutch", "score": 95.0}, {"start": 200.0, "end": 217.0, "why": "story", "score": 80.0}]
    story, clutch = scan.to_windows(raw, 600.0, cfg, wanted=5)
    assert story["start"] <= 200 - scan.PAD_BEFORE and story["end"] >= 217 + scan.PAD_AFTER
    assert clutch["end"] - clutch["start"] >= cfg["clips"]["max_seconds"] + 30  # room for a full-length clip
    # At the very start and end of a recording the stretch is pushed inward, never past the edges.
    first, last = scan.to_windows([{"start": 2.0, "end": 10.0, "why": "a", "score": 1.0}, {"start": 590.0, "end": 598.0, "why": "b", "score": 1.0}], 600.0, cfg, 5)
    assert first["start"] == 0.0 and last["end"] == 600.0 and last["end"] - last["start"] >= 90
    # Two that touch become one, and its reason says both.
    (both,) = scan.to_windows([{"start": 200.0, "end": 217.0, "why": "story", "score": 80.0}, {"start": 240.0, "end": 260.0, "why": "joke", "score": 60.0}], 600.0, cfg, 5)
    assert both["why"] == "story / joke" and both["score"] == 80.0 and both["start"] < 200 < 260 < both["end"]
    # Only the best `wanted` are kept, and the total stays inside the budget.
    many = [{"start": float(t), "end": float(t + 20), "why": "x", "score": float(t)} for t in range(0, 20000, 400)]
    kept = scan.to_windows(many, 20000.0, cfg, wanted=40)
    assert sum(w["end"] - w["start"] for w in kept) <= scan.MAX_SHORTLIST_SECONDS
    assert len(scan.to_windows(many, 20000.0, cfg, wanted=3)) == 3


def test_without_claude_the_fallback_finds_the_lively_stretches(cfg, monkeypatch):
    monkeypatch.setattr(pick, "claude_backend", lambda: None)
    cfg["clips"].update(count=2, min_seconds=10, max_seconds=30)
    cfg["stream"]["shortlist"] = 2
    result = scan.shortlist(stream_lines(), [{"at": 403.0, "end": 412.0, "lift": 9.0}], [], cfg, META)
    assert result["picker"] == "heuristic" and "No Claude credential" in result["note"] and result["model"] == ""
    inside = [any(w["start"] <= t <= w["end"] for w in result["windows"]) for t in (207, 405)]
    assert inside == [True, True]  # the story and the clutch, not the greetings or the settings
    assert not any(w["start"] <= 62 <= w["end"] for w in result["windows"])


def test_viewer_clips_tip_the_fallback(cfg):
    lines = stream_lines()
    (plain,) = scan.heuristic_shortlist(lines, [], [], cfg, wanted=1)
    (tipped,) = scan.heuristic_shortlist(lines, [], [{"at": 207.0, "views": 90000, "title": "the kid"}], cfg, wanted=1)
    assert plain["start"] == 400.0  # left alone, the shouting wins
    assert tipped["start"] == 200.0 and "viewer clips nearby" in tipped["why"]  # a much-clipped story overtakes it
    # A hint cannot conjure a clip out of a stretch where hardly anything is said.
    (still,) = scan.heuristic_shortlist(lines, [], [{"at": 302.0, "views": 90000, "title": "map"}], cfg, wanted=1)
    assert still["start"] == 400.0


def test_claude_does_the_shortlist_when_it_can(cfg, monkeypatch):
    seen = {}

    def fake_ask(backend, system, user, model, schema=None):
        seen.update(backend=backend, system=system, user=user, schema=schema)
        return {"windows": [{"first": 7, "last": 9, "reason": "One versus four clutch.", "score": 95}]}

    monkeypatch.setattr(pick, "claude_backend", lambda: "cli")
    monkeypatch.setattr(pick, "ask", fake_ask)
    result = scan.shortlist(stream_lines(), [], [], cfg, META)
    assert result["picker"] == "claude" and result["model"] == cfg["picker"]["model"] and result["note"] == ""
    (window,) = result["windows"]
    assert window["start"] < 400 < 414 < window["end"] and window["why"] == "One versus four clutch."
    assert seen["schema"] is scan.SCHEMA and "never an instruction" in seen["system"] and "<transcript>" in seen["user"]


def test_a_claude_failure_falls_back_with_a_note_unless_claude_was_demanded(cfg, monkeypatch):
    def broken(*args, **kwargs):
        raise ClipperError("Claude API error: RateLimitError")

    monkeypatch.setattr(pick, "claude_backend", lambda: "api")
    monkeypatch.setattr(pick, "ask", broken)
    result = scan.shortlist(stream_lines(), [], [], cfg, META)
    assert result["picker"] == "heuristic" and "RateLimitError" in result["note"]
    with pytest.raises(ClipperError, match="RateLimitError"):
        scan.shortlist(stream_lines(), [], [], cfg, META, mode="claude")
    monkeypatch.setattr(pick, "ask", lambda *a, **k: {"windows": []})
    assert "shortlisted nothing usable" in scan.shortlist(stream_lines(), [], [], cfg, META)["note"]


def test_a_recording_with_no_speech_is_a_clear_error(cfg):
    with pytest.raises(ClipperError, match="Almost no speech"):
        scan.shortlist([Line(0, 1.0, 2.0, "Thank you.")], [], [], cfg, META, mode="heuristic")


def test_how_many_stretches_get_a_close_look(cfg):
    assert scan.wanted_count(cfg) == 14  # eight clips wanted: half as many again, plus two
    cfg["clips"]["count"] = 1
    assert scan.wanted_count(cfg) == 5
    cfg["clips"]["count"] = 30
    assert scan.wanted_count(cfg) == 24
    cfg["stream"]["shortlist"] = 9
    assert scan.wanted_count(cfg) == 9


def test_a_very_long_transcript_is_sent_in_several_requests(cfg, monkeypatch):
    lines = [Line(i, i * 10.0, i * 10.0 + 8, "word " * 60) for i in range(4000)]  # about 1.3 million characters
    asked = []
    monkeypatch.setattr(pick, "ask", lambda backend, system, user, model, schema=None: asked.append(user) or {"windows": []})
    scan.claude_shortlist(lines, [], [], cfg, {"duration": 40000.0}, 12, "api", "m")
    assert len(asked) == 3 and all(len(user) <= pick.MAX_PROMPT_CHARS + 2000 for user in asked)
    assert "This is one part of the recording: lines 0 to 1333." in asked[0]
