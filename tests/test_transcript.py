from clipper.transcript import Transcript, Word, apply_replacements, build_sentences, clean_words

from conftest import make_transcript


def words(text: str, gap_after: dict | None = None) -> list[Word]:
    """Evenly spaced words; gap_after maps a word index to extra silence after it."""
    out, t = [], 0.0
    for i, part in enumerate(text.split()):
        out.append(Word(part, t, t + 0.25))
        t += 0.3 + (gap_after or {}).get(i, 0.0)
    return out


def test_sentences_split_on_end_punctuation():
    sentences = build_sentences(words("Hello there. How are you? Fine!"))
    assert [s.text for s in sentences] == ["Hello there.", "How are you?", "Fine!"]
    assert [(s.first, s.last) for s in sentences] == [(0, 1), (2, 4), (5, 5)]


def test_abbreviations_do_not_end_a_sentence():
    sentences = build_sentences(words("I met Dr. Smith at 9 a.m. today. He was late."))
    assert [s.text for s in sentences] == ["I met Dr. Smith at 9 a.m. today.", "He was late."]


def test_long_pause_splits_unpunctuated_speech():
    sentences = build_sentences(words("so that was it and then we moved on", gap_after={3: 1.5}))
    assert [s.text for s in sentences] == ["so that was it", "and then we moved on"]


def test_runaway_sentence_is_broken_up():
    sentences = build_sentences(words(" ".join(["word"] * 120)), max_words=45)
    assert len(sentences) == 3
    assert all(s.last - s.first + 1 <= 45 for s in sentences)


def test_sentence_times_come_from_their_words():
    transcript = make_transcript([("One two three.", 5.0, 3.0), ("Four five.", 9.0, 2.0)])
    first, second = transcript.sentences
    assert first.start == 5.0 and second.start == 9.0
    assert first.end < second.start


def test_clean_words_drops_blanks_and_fixes_order():
    cleaned = clean_words([Word(" hi ", 1.0, 1.2), Word("  ", 1.2, 1.3), Word("there", 0.9, 0.9)])
    assert [w.text for w in cleaned] == ["hi", "there"]
    assert cleaned[1].start >= cleaned[0].start
    assert cleaned[1].end > cleaned[1].start


def test_replacement_merges_words_and_keeps_punctuation():
    source = [Word("We", 0, 0.2), Word("use", 0.2, 0.4), Word("crowd", 0.4, 0.7), Word("strike,", 0.7, 1.0), Word("daily.", 1.0, 1.3)]
    fixed = apply_replacements(source, {"Crowd Strike": "CrowdStrike"})
    assert [w.text for w in fixed] == ["We", "use", "CrowdStrike,", "daily."]
    assert fixed[2].start == 0.4 and fixed[2].end == 1.0


def test_replacement_can_expand_and_ignores_case():
    fixed = apply_replacements([Word("the", 0, 0.2), Word("SIEM", 0.2, 0.8)], {"siem": "S I E M"})
    assert [w.text for w in fixed] == ["the", "S", "I", "E", "M"]
    assert fixed[1].start == 0.2 and abs(fixed[-1].end - 0.8) < 1e-9


def test_save_and_load_round_trip(tmp_path):
    transcript = make_transcript([("Hello there friend.", 1.0, 2.0)])
    transcript.save(tmp_path / "t.json")
    loaded = Transcript.load(tmp_path / "t.json")
    assert [w.text for w in loaded.words] == ["Hello", "there", "friend."]
    assert len(loaded.sentences) == 1


def test_a_comma_followed_by_a_real_pause_ends_a_unit():
    """Speech recognition often joins spoken sentences with commas; measured pauses pull them apart."""
    run_on = words("So here is the thing about pricing, most founders charge too little, they are scared.")
    assert len(build_sentences(run_on)) == 1
    split = build_sentences(run_on, pauses={6: 0.4, 11: 0.2})
    assert [s.text for s in split] == ["So here is the thing about pricing,", "most founders charge too little, they are scared."]


def test_a_long_pause_ends_a_unit_even_without_punctuation():
    split = build_sentences(words("and then we moved on to the next thing"), pauses={3: 0.9})
    assert [s.text for s in split] == ["and then we moved", "on to the next thing"]
    assert len(build_sentences(words("and then we moved on to the next thing"), pauses={3: 0.4})) == 1


def test_pauses_survive_save_and_load(tmp_path):
    transcript = make_transcript([("First part, second part.", 0.0, 2.0)])
    transcript.set_pauses({1: 0.5})
    assert len(transcript.sentences) == 2
    transcript.save(tmp_path / "t.json")
    loaded = Transcript.load(tmp_path / "t.json")
    assert loaded.pauses == {1: 0.5} and [s.text for s in loaded.sentences] == ["First part,", "second part."]
