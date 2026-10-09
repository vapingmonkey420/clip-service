import re

from clipper import captions


def settings(**overrides) -> dict:
    base = {"show": True, "font": "Poppins", "size": 84, "uppercase": True, "max_words": 3,
            "color": "#FFFFFF", "highlight": "#FFD60A", "outline": "#000000"}  # fmt: skip
    base.update(overrides)
    return base


def timed(text: str, start: float = 0.0, step: float = 0.3):
    return [(word, start + i * step, start + i * step + 0.25) for i, word in enumerate(text.split())]


def events(ass: str, style: str) -> list[tuple[float, float, str]]:
    def seconds(stamp: str) -> float:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    out = []
    for line in ass.splitlines():
        if line.startswith("Dialogue:") and f",{style}," in line:
            _, start, end, *_ = line.split(",", 9)
            out.append((seconds(start), seconds(end), line.split(",", 9)[9]))
    return out


def plain(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text)


def test_bundled_font_is_measured_not_guessed():
    font = captions.load_font("Poppins")
    assert font.path is not None and font.path.name == "Poppins-Bold.ttf"
    assert captions.load_font("Poppins ExtraBold").path.name == "Poppins-ExtraBold.ttf"
    assert font.width("WWWW", 84) > font.width("IIII", 84) * 2
    assert 1.2 < font.line_ratio < 1.8


def test_display_word_cleans_punctuation():
    assert captions.display_word("Hello,", True) == "HELLO"
    assert captions.display_word("“really?”", True) == "REALLY?"
    assert captions.display_word("don't.", False) == "don't"
    assert captions.display_word("$1,000,", True) == "$1,000"
    assert captions.display_word("—", True) == ""
    assert captions.display_word("{\\b1}x", False) == "(/b1)x"  # nothing can smuggle in styling codes


def test_chunks_respect_word_limit_sentence_ends_and_pauses():
    font = captions.load_font("Poppins")
    words = timed("So here is the thing. Nobody tells you")
    chunks = captions.chunk_words(words, font, 84, 3, True)
    assert [c.text for c in chunks] == ["SO HERE IS", "THE THING", "NOBODY TELLS YOU"]

    paused = [("one", 0.0, 0.2), ("two", 0.3, 0.5), ("three", 2.0, 2.2)]
    assert [c.text for c in captions.chunk_words(paused, font, 84, 3, True)] == ["ONE TWO", "THREE"]


def test_chunks_never_exceed_the_line_width():
    font = captions.load_font("Poppins")
    words = timed("international distribution organisations everywhere simultaneously")
    for chunk in captions.chunk_words(words, font, 84, 3, True):
        assert len(chunk.words) == 1 or font.width(chunk.text, 84) <= captions.MAX_LINE


def test_events_are_ordered_and_highlight_the_spoken_word():
    ass = captions.build_ass(timed("Most shows do not have a content problem."), settings(), duration=4.0, caption_y=1267)
    caps = events(ass, "Cap")
    assert len(caps) == 8  # one event per word
    for (s1, e1, _), (s2, _, _) in zip(caps, caps[1:]):
        assert s1 < e1 <= s2 + 1e-6  # never two captions at once
    first = caps[0][2]
    assert plain(first) == "MOST SHOWS DO"
    assert "{\\c&H0AD6FF&}MOST{\\c&HFFFFFF&}" in first  # yellow, in ASS's blue-green-red order
    assert "\\pos(540,1267)" in first
    third = caps[2][2]
    assert "{\\c&H0AD6FF&}DO{\\c&HFFFFFF&}" in third


def test_a_caption_lingers_briefly_then_clears():
    ass = captions.build_ass([("Hello", 1.0, 1.3), ("again", 5.0, 5.3)], settings(), duration=8.0, caption_y=1000)
    caps = events(ass, "Cap")
    assert caps[0][0] == 1.0 and 1.3 < caps[0][1] <= 1.3 + captions.LINGER + 0.01
    assert caps[1][1] <= 8.0


def test_overlong_word_is_shrunk_to_fit():
    ass = captions.build_ass([("Pneumonoultramicroscopicsilicovolcanoconiosis", 0.0, 1.0)], settings(), duration=2.0, caption_y=1000)
    (only,) = events(ass, "Cap")
    scale = int(re.search(r"\\fscx(\d+)", only[2]).group(1))
    assert scale < 100
    font = captions.load_font("Poppins")
    assert font.width(plain(only[2]), 84) * scale / 100 <= captions.MAX_LINE + 12


def title_lines(title: str) -> tuple[list[str], str]:
    ass = captions.build_ass([], settings(), duration=10.0, caption_y=1000, title=title, title_until=3.5)
    ((_, _, text),) = events(ass, "Title")
    return plain(text).split("\\N"), text


def test_headline_modes():
    words = timed("one two three four five six")
    hidden = captions.build_ass(words, settings(), duration=10.0, caption_y=1000, title="A headline", title_until=0)
    assert events(hidden, "Title") == []
    shown = captions.build_ass(words, settings(), duration=10.0, caption_y=1000, title="A headline", title_y=320, title_until=3.5)
    ((start, end, text),) = events(shown, "Title")
    assert (start, end) == (0.0, 3.5) and "\\fad(0,250)" in text  # fades out when it leaves early
    always = captions.build_ass(words, settings(), duration=10.0, caption_y=1000, title="A headline", title_until=10.0)
    assert events(always, "Title")[0][1] == 10.0 and "\\fad" not in events(always, "Title")[0][2]


def test_headline_wraps_into_even_lines():
    font = captions.load_font("Poppins")
    size = round(84 * 0.66)
    assert title_lines("Charge more")[0] == ["Charge more"]

    lines, text = title_lines("Why most podcasts never find an audience")
    assert len(lines) == 2 and "\\fs" not in text
    widths = [font.width(line, size) for line in lines]
    assert max(widths) <= captions.TITLE_LINE and min(widths) > 0.6 * max(widths)  # no orphan word

    # Too long for two lines at full size: set smaller first, and only then given a third line.
    lines, text = title_lines("Most podcasts have a distribution problem, not content")
    assert len(lines) == 2 and "\\fs" in text
    lines, text = title_lines("Most independent podcasts have a serious distribution problem rather than a content problem")
    assert len(lines) == 3 and "\\fs" in text
    assert all(font.width(line, round(size * 0.86)) <= captions.TITLE_LINE for line in lines)

    lines, _ = title_lines(" ".join(["extraordinarily"] * 14))
    assert len(lines) == 3 and lines[-1].endswith("…")  # hopeless length is cut, never allowed to cover the frame


def test_headline_stays_above_a_face_but_below_the_app_controls():
    def y_of(**kwargs):
        ass = captions.build_ass([], settings(), duration=5.0, caption_y=1000, title="Short headline", title_until=5, **kwargs)
        return int(re.search(r"\\pos\(540,(\d+)\)", events(ass, "Title")[0][2]).group(1))

    assert y_of(title_y=320) == 320
    assert y_of(title_y=320, title_floor=300) < 300  # pushed up to clear the head
    assert y_of(title_y=320, title_floor=40) >= captions.TOP_SAFE  # but never into the app's own controls


def test_captions_can_be_turned_off():
    ass = captions.build_ass(timed("one two three"), settings(show=False), duration=3.0, caption_y=1000, title="T", title_until=3)
    assert events(ass, "Cap") == [] and len(events(ass, "Title")) == 1


def test_time_format():
    assert captions.ass_time(0) == "0:00:00.00"
    assert captions.ass_time(75.239) == "0:01:15.24"
    assert captions.ass_time(3661.5) == "1:01:01.50"
