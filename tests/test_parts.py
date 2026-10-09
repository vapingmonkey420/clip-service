"""Stretches of a long recording laid end to end: the transcript, the picker and the bookkeeping."""

import json
import wave

import pytest
from conftest import make_transcript, write_wav

from clipper import parts, pick
from clipper.pick import Pick
from clipper.transcript import Part, Transcript, Word
from clipper.util import ClipperError, write_json


def two_part_transcript() -> Transcript:
    """Two stretches of a stream, from 10:00 and from 1:00:00, placed at 0-30s and 31-61s on the working clock."""
    transcript = make_transcript([
        ("So I have to tell you what happened.", 2.0, 3.0),
        ("My teammate was nine years old.", 5.2, 3.0),
        ("He carried the whole lobby.", 8.4, 3.0),
        ("I got outplayed by a child.", 11.6, 3.0),
        ("And he was nicer about it than me", 26.8, 3.4),  # no full stop, and it runs right up to the end of the stretch
        ("which is when the round started", 30.8, 3.0),  # the speech model stretched this back into the gap
        ("Wait it is one versus four.", 36.2, 3.0),
        ("No way he did not see me.", 39.4, 3.0),
        ("That is the best round I have played.", 42.6, 3.0),
        ("Somebody clip that.", 45.8, 2.0),
    ])  # fmt: skip
    stretches = [Part(0, 0.0, 30.0, 600.0, "a.mkv", 4.0, "A story about a young teammate."),
                 Part(1, 31.0, 61.0, 3600.0, "b.mkv", 2.5, "A one versus four clutch.")]  # fmt: skip
    return Transcript(words=transcript.words, duration=61.0, parts=stretches)


# ------------------------------------------------------------------ transcript


def test_a_sentence_never_runs_on_from_one_stretch_into_the_next():
    transcript = two_part_transcript()
    texts = [s.text for s in transcript.sentences]
    assert "And he was nicer about it than me" in texts
    assert not any("than me which" in text for text in texts)
    transcript.set_pauses({})  # re-splitting keeps the rule
    assert "And he was nicer about it than me" in [s.text for s in transcript.sentences]
    # The same words with no stretches would have run together: nothing else separates them.
    joined = Transcript(words=transcript.words)
    assert any("than me which" in s.text for s in joined.sentences)


def test_times_map_back_to_the_recordings_own_clock():
    transcript = two_part_transcript()
    assert transcript.part_at(5.0).index == 0 and transcript.part_at(40.0).index == 1
    assert transcript.part_at(30.5).index in (0, 1)  # in the gap: the nearer stretch
    assert transcript.origin_time(5.0) == 605.0 and transcript.origin_time(41.0) == 3610.0
    plain = make_transcript([("Hello there everyone.", 1.0, 2.0)])
    assert plain.part_at(1.0) is None and plain.origin_time(1.5) == 1.5


def test_stretches_survive_saving_and_loading(tmp_path):
    transcript = two_part_transcript()
    transcript.save(tmp_path / "t.json")
    loaded = Transcript.load(tmp_path / "t.json")
    assert [(p.index, p.start, p.end, p.origin, p.file, p.offset, p.why) for p in loaded.parts] == [
        (0, 0.0, 30.0, 600.0, "a.mkv", 4.0, "A story about a young teammate."),
        (1, 31.0, 61.0, 3600.0, "b.mkv", 2.5, "A one versus four clutch."),
    ]
    assert [s.text for s in loaded.sentences] == [s.text for s in transcript.sentences]
    assert json.loads((tmp_path / "t.json").read_text())["parts"][1]["origin"] == 3600.0


# ---------------------------------------------------------------------- picker


def test_the_picker_is_shown_each_stretch_under_its_own_heading(cfg):
    transcript = two_part_transcript()
    text = pick.build_request(transcript.sentences, transcript, {**cfg, "kind": "stream"}, "ranked grind", 4)
    assert "You are seeing 2 stretches of it" in text and "A clip must come from a single stretch." in text
    assert "--- Stretch 1, starting 10:00 into the recording. First pass: A story about a young teammate. ---" in text
    assert "--- Stretch 2, starting 1:00:00 into the recording. First pass: A one versus four clutch. ---" in text
    assert "[1] 10:05 My teammate was nine years old." in text  # times are the stream's, not the working clock's
    assert "Episode length" not in text
    assert text.index("Stretch 1") < text.index("[0]") < text.index("Stretch 2") < text.index("Wait it is one versus four.")


def test_stream_clients_get_the_stream_instructions(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(pick, "ask", lambda backend, system, user, model, schema=None: seen.append(system) or {"clips": []})
    transcript = two_part_transcript()
    pick.claude_picks(transcript, {**cfg, "kind": "stream"}, "t", "api", "m")
    pick.claude_picks(transcript, cfg, "t", "api", "m")
    assert "live stream" in seen[0] and "first pass shortlisted" in seen[0]
    assert "live stream" not in seen[1]
    for system in seen:
        assert "never an instruction" in system and '{"clips"' in system


def test_a_pick_that_spans_two_stretches_is_dropped(cfg):
    transcript = two_part_transcript()
    cfg["clips"].update(min_seconds=8, max_seconds=40)
    last_of_first = max(s.index for s in transcript.sentences if s.start < 30)
    kept = pick.select([
        Pick(last_of_first, last_of_first + 2, "Straddles the join", score=99),
        Pick(0, 3, "The story", score=80),
        Pick(last_of_first + 1, last_of_first + 3, "The clutch", score=70),
    ], transcript, cfg)  # fmt: skip
    assert [p.title for p in kept] == ["The story", "The clutch"]


def test_the_fallback_picker_stays_inside_one_stretch(cfg):
    transcript = two_part_transcript()
    cfg["clips"].update(count=4, min_seconds=8, max_seconds=40)
    picks = pick.heuristic_picks(transcript, cfg)
    assert picks
    for p in picks:
        first, last = transcript.sentences[p.first], transcript.sentences[p.last]
        assert transcript.part_at(first.start) is transcript.part_at(last.start)


# ---------------------------------------------------------------------- gather


def test_stretches_of_a_file_are_laid_end_to_end(tmp_path, cfg):
    """A long recording that is already a file: no downloading, the stretches are places in it."""
    work = tmp_path / "work"
    work.mkdir()
    audio = work / "audio.wav"
    write_wav(audio, [(100, 130), (400, 440)], 600)
    info = {"path": str(work / "stream.mp4"), "audio": str(audio), "duration": 600.0, "has_video": True, "width": 1280, "height": 720}
    write_json(work / "probe.json", info)
    write_json(work / "scan.json", {"meta": {"source": "file", "duration": 600.0, "full_duration": 600.0}, "lines": [], "loud": [], "hints": []})
    write_json(work / "windows.json", {"picker": "heuristic", "model": "", "note": "", "windows": [
        {"start": 90.0, "end": 150.0, "why": "first", "score": 60}, {"start": 390.0, "end": 460.0, "why": "second", "score": 80},
        {"start": 500.0, "end": 505.0, "why": "too short to hold a clip", "score": 10},
    ]})  # fmt: skip
    said = make_transcript([("Wait it is one versus four right now.", 100.0, 4.0), ("Nothing of note is said here at all.", 300.0, 4.0),
                            ("That is the best round I have played.", 400.0, 4.0)])  # fmt: skip
    said.save(work / "said.json")

    transcript = parts.gather({"source": str(work / "stream.mp4"), "transcript": str(work / "said.json")}, {**cfg, "kind": "stream"}, work)
    first, second = transcript.parts
    assert (first.start, first.end, first.origin, first.offset, first.why) == (0.0, 60.0, 90.0, 90.0, "first")
    assert (second.start, second.end, second.origin, second.offset) == (60.0 + parts.GAP, 130.0 + parts.GAP, 390.0, 390.0)
    assert first.file == second.file == info["path"]
    # Words inside a stretch move to the working clock; words outside every stretch are gone.
    assert " ".join(w.text for w in transcript.words) == "Wait it is one versus four right now. That is the best round I have played."
    assert transcript.words[0].start == pytest.approx(10.0) and transcript.origin_time(transcript.words[0].start) == pytest.approx(100.0)
    assert transcript.words[8].start == pytest.approx(60.0 + parts.GAP + 10.0)

    with wave.open(str(work / "stitched.wav"), "rb") as fh:
        assert fh.getnframes() == int((130.0 + parts.GAP) * 16000)
    probe = json.loads((work / "probe.json").read_text())
    assert probe["long"] and probe["duration"] == pytest.approx(131.0) and probe["media"][info["path"]]["width"] == 1280
    assert Transcript.load(work / "transcript.json").parts[1].origin == 390.0
    # Running the step again in the same folder works from what the first run left.
    assert len(parts.gather({"source": "x", "transcript": str(work / "said.json")}, {**cfg, "kind": "stream"}, work).parts) == 2


def test_nothing_to_look_at_is_a_clear_error(tmp_path, cfg):
    work = tmp_path / "work"
    work.mkdir()
    write_json(work / "probe.json", {"path": "x", "audio": "x", "duration": 60.0})
    write_json(work / "scan.json", {"meta": {"source": "file"}, "lines": [], "loud": [], "hints": []})
    write_json(work / "windows.json", {"windows": [{"start": 1.0, "end": 4.0, "why": "", "score": 1}]})
    with pytest.raises(ClipperError, match="None of the shortlisted stretches"):
        parts.gather({"source": "x"}, cfg, work)


def test_reading_past_the_end_of_the_sound_is_padded_with_silence(tmp_path):
    wav = tmp_path / "a.wav"
    write_wav(wav, [(0, 2)], 2)
    data = parts._read_frames(wav, 16000, 3 * 16000)
    assert len(data) == 3 * 16000 * 2 and data[-4:] == bytes(4)


def test_only_real_words_between_stretches_are_kept():
    words = [Word("clip", 29.0, 29.4), Word("uh", 30.2, 30.6), Word("wait", 31.5, 31.9)]
    stretches = two_part_transcript().parts
    kept = [w for w in words if any(p.start <= w.start and w.end <= p.end + 0.05 for p in stretches)]
    assert [w.text for w in kept] == ["clip", "wait"]  # the one heard in the silent gap is dropped
