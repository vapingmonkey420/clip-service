import pytest

from clipper import silence
from clipper.transcript import Word

from conftest import write_wav


def near(value: float, target: float, tolerance: float = 0.05) -> bool:
    return abs(value - target) <= tolerance


@pytest.fixture
def speech(tmp_path):
    """Sound from 1-3s, 4.5-6s and 6.2-9s; quiet everywhere else in a 12s file."""
    path = tmp_path / "audio.wav"
    write_wav(path, [(1.0, 3.0), (4.5, 6.0), (6.2, 9.0)], total=12.0)
    return path


def test_finds_the_real_pauses(speech):
    found = silence.find_silences(speech)
    assert len(found) == 3  # the 0.2s gap at 6.0 is too short to count
    assert near(found[0][0], 0.0) and near(found[0][1], 1.0)
    assert near(found[1][0], 3.0) and near(found[1][1], 4.5)
    assert near(found[2][0], 9.0) and near(found[2][1], 12.0)


def test_no_pauses_reported_when_there_is_no_quiet(tmp_path):
    path = tmp_path / "busy.wav"
    write_wav(path, [(0.0, 5.0)], total=5.0)
    assert silence.find_silences(path) == []


def test_snap_puts_cuts_inside_the_surrounding_pauses(speech):
    pauses = silence.find_silences(speech)
    words = [Word("before", 0.2, 0.6), Word("Hello", 1.05, 1.9), Word("world.", 2.0, 3.2), Word("Next", 4.5, 5.0)]
    start, end = silence.snap(words, 1, 2, pauses, duration=12.0)
    assert near(start, 1.0 - silence.LEAD_IN)  # just ahead of where the sound starts
    assert near(end, 3.0 + silence.TAIL)  # a short tail after the sound stops, not the transcript's 3.2
    assert end < 4.5


def test_snap_ignores_a_word_stretched_back_over_a_pause(speech):
    pauses = silence.find_silences(speech)
    # The transcript claims "Next" starts at 3.2, in the middle of the pause; the sound starts at 4.5.
    words = [Word("world.", 2.0, 3.0), Word("Next", 3.2, 5.0), Word("part", 5.1, 5.9)]
    start, _ = silence.snap(words, 1, 2, pauses, duration=12.0)
    assert near(start, 4.5 - silence.LEAD_IN)


def test_snap_without_pauses_stays_between_neighbouring_words():
    words = [Word("a", 0.0, 1.0), Word("b", 1.04, 2.0), Word("c", 2.02, 3.0), Word("d", 3.06, 4.0)]
    start, end = silence.snap(words, 1, 2, [], duration=10.0)
    assert 1.0 < start <= 1.04
    assert 3.0 <= end < 3.06


def test_tighten_removes_the_middle_of_a_long_pause(speech):
    pauses = silence.find_silences(speech)
    words = [Word("one", 1.0, 3.0), Word("two", 4.5, 6.0), Word("three", 6.2, 9.0)]
    parts = silence.tighten(0.9, 9.3, pauses, words, max_pause=0.6)
    assert len(parts) == 2
    (a0, a1), (b0, b1) = parts
    assert a0 == 0.9 and b1 == 9.3
    assert near(a1, 3.0 + silence.KEEP_AFTER) and near(b0, 4.5 - silence.KEEP_BEFORE)
    kept = (a1 - a0) + (b1 - b0)
    assert near(kept, 8.4 - 1.5 + silence.KEEP_AFTER + silence.KEEP_BEFORE, 0.08)


def test_tighten_leaves_short_pauses_and_quiet_speech_alone(speech):
    pauses = silence.find_silences(speech)
    words = [Word("one", 1.0, 3.0), Word("two", 4.5, 6.0)]
    assert silence.tighten(0.9, 6.3, pauses, words, max_pause=2.0) == [(0.9, 6.3)]
    # A whole word sits in the "pause": someone spoke too softly for the level check, so keep it.
    whispered = words + [Word("psst", 3.4, 3.9)]
    assert silence.tighten(0.9, 6.3, pauses, whispered, max_pause=0.6) == [(0.9, 6.3)]


def test_retime_maps_episode_time_to_clip_time():
    parts = [(10.0, 12.0), (13.0, 15.0)]
    assert silence.retime(10.0, parts) == 0.0
    assert silence.retime(11.5, parts) == 1.5
    assert silence.retime(12.5, parts) == 2.0  # inside the removed stretch: pinned to the join
    assert silence.retime(13.5, parts) == 2.5
    assert silence.retime(99.0, parts) == 4.0


def test_each_pause_is_attached_to_the_word_before_it(speech):
    pauses = silence.find_silences(speech)  # quiet at 0-1, 3-4.5 and 9-12
    words = [Word("one", 1.0, 3.0), Word("two", 4.5, 6.0), Word("three", 6.2, 9.0)]
    found = silence.pauses_after_words(words, pauses)
    assert list(found) == [0] and abs(found[0] - 1.5) < 0.06  # nothing before the first word or after the last

    # "two" is stretched back to 3.3, inside the pause; the pause still belongs after "one".
    stretched = [Word("one", 1.0, 3.0), Word("two", 3.3, 6.0), Word("three", 6.2, 9.0)]
    assert list(silence.pauses_after_words(stretched, pauses)) == [0]
