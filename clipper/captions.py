"""Burned-in captions as an ASS subtitle file.

A few words show at a time and the word being spoken is highlighted. Text is
measured with the real font, so a caption line never runs off the frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path

from .util import REPO, log

FONTS_DIR = REPO / "assets" / "fonts"
WIDTH, HEIGHT = 1080, 1920
MAX_LINE = 900  # widest a caption line may be, in pixels
TITLE_LINE = 840
TOP_SAFE = 120  # apps draw their own controls over the top of the screen
NEW_CHUNK_GAP = 0.45  # a pause this long always starts a new caption
LINGER = 0.35  # how long a caption may stay up after its last word


@dataclass
class Chunk:
    words: list[tuple[str, float, float]]  # (display text, start, end)

    @property
    def start(self) -> float:
        return self.words[0][1]

    @property
    def end(self) -> float:
        return self.words[-1][2]

    @property
    def text(self) -> str:
        return " ".join(w[0] for w in self.words)


# -------------------------------------------------------------- font metrics


class Font:
    """Letter widths for one font file, so text can be measured before rendering."""

    def __init__(self, path: Path | None):
        self.path = path
        self._advance: dict[int, float] = {}
        self._fallback = 0.62
        self.line_ratio = 1.2  # (ascent + descent) / em, which is how ASS counts font size
        if path is None:
            return
        from fontTools.ttLib import TTFont

        font = TTFont(str(path), lazy=True)
        em = font["head"].unitsPerEm
        metrics, cmap = font["hmtx"], font.getBestCmap()
        self._advance = {code: metrics[glyph][0] / em for code, glyph in cmap.items()}
        os2 = font["OS/2"] if "OS/2" in font else None
        if os2 is not None and os2.usWinAscent + os2.usWinDescent > 0:
            self.line_ratio = (os2.usWinAscent + os2.usWinDescent) / em
        else:
            self.line_ratio = (font["hhea"].ascent - font["hhea"].descent) / em

    def width(self, text: str, size: float) -> float:
        return size * sum(self._advance.get(ord(ch), self._fallback) for ch in text)

    def ass_size(self, size: float) -> float:
        return round(size * self.line_ratio, 1)


@lru_cache(maxsize=8)
def load_font(name: str) -> Font:
    """Find `name` among assets/fonts by family or full name."""
    wanted = name.strip().lower()
    matches = []
    for path in sorted(FONTS_DIR.glob("*.[ot]tf")):
        try:
            from fontTools.ttLib import TTFont

            names = TTFont(str(path), lazy=True)["name"]
            family, full, typographic = (str(names.getDebugName(i) or "").lower() for i in (1, 4, 16))
            weight = TTFont(str(path), lazy=True)["OS/2"].usWeightClass
        except Exception:  # an unreadable font file should not stop a render
            continue
        if wanted in (family, full):
            matches.append((0, abs(weight - 700), path))
        elif wanted == typographic:
            matches.append((1, abs(weight - 700), path))
    if not matches:
        log.warning("Font '%s' is not in assets/fonts; caption widths will be estimated.", name)
        return Font(None)
    return Font(min(matches)[2])


# ------------------------------------------------------------------ chunking


def display_word(text: str, uppercase: bool) -> str:
    """How a transcript word appears on screen."""
    out = text.strip().replace("—", "").replace("–", "-")
    out = out.strip("\"“”‘’()[]…")
    out = re.sub(r"^[.,;:]+|[.,;:]+$", "", out)
    out = out.replace("{", "(").replace("}", ")").replace("\\", "/")
    return out.upper() if uppercase else out


def ends_sentence(text: str) -> bool:
    return text.rstrip("\"”’)]").endswith((".", "?", "!"))


def chunk_words(words, font: Font, size: float, max_words: int, uppercase: bool) -> list[Chunk]:
    """Group timed words into short captions that each fit on one line."""
    chunks: list[Chunk] = []
    current: list[tuple[str, float, float]] = []
    force_break = False
    previous_end = None
    for raw, start, end in words:
        shown = display_word(raw, uppercase)
        if not shown:
            continue
        gap = start - previous_end if previous_end is not None else 0.0
        candidate = " ".join([w[0] for w in current] + [shown])
        if current and (
            force_break
            or len(current) >= max_words
            or gap >= NEW_CHUNK_GAP
            or font.width(candidate, size) > MAX_LINE
        ):
            chunks.append(Chunk(current))
            current = []
        current.append((shown, start, max(end, start + 0.03)))
        force_break = ends_sentence(raw)
        previous_end = end
    if current:
        chunks.append(Chunk(current))
    return chunks


# ------------------------------------------------------------------- writing


def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_colour(hex_colour: str, alpha: int = 0) -> str:
    r, g, b = hex_colour[1:3], hex_colour[3:5], hex_colour[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _inline(hex_colour: str) -> str:
    return "\\c&H" + (hex_colour[5:7] + hex_colour[3:5] + hex_colour[1:3]).upper() + "&"


def wrap_title(title: str, font: Font, size: float, max_lines: int = 3) -> list[str]:
    """Break a headline into the fewest lines that fit, as even in length as possible."""
    words = title.split()
    if not words:
        return []
    for count in range(1, max_lines + 1):
        best = None
        for breaks in combinations(range(1, len(words)), count - 1):
            edges = (0, *breaks, len(words))
            lines = [" ".join(words[a:b]) for a, b in zip(edges, edges[1:])]
            widest = max(font.width(line, size) for line in lines)
            if widest <= TITLE_LINE and (best is None or widest < best[0]):
                best = (widest, lines)
        if best:
            return best[1]
    # Too long even for max_lines: fill greedily and cut the rest off.
    lines = [""]
    for word in words:
        candidate = f"{lines[-1]} {word}".strip()
        if lines[-1] and font.width(candidate, size) > TITLE_LINE:
            lines.append(word)
        else:
            lines[-1] = candidate
    lines = lines[:max_lines]
    lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return lines


def build_ass(
    words,
    settings: dict,
    *,
    duration: float,
    caption_y: int,
    title: str = "",
    title_y: int = 320,
    title_floor: float | None = None,
    title_until: float = 0.0,
) -> str:
    """Return the text of an ASS file.

    `words` are (text, start, end) with times measured from the start of the
    finished clip. `title_until` is when the headline disappears (0 hides it).
    `title_floor` is the lowest the headline may reach, so it clears a face.
    """
    font = load_font(settings["font"])
    size = float(settings["size"])
    title_size = round(size * 0.66)
    outline = max(3, round(size * 0.085))
    primary, highlight = settings["color"], settings["highlight"]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{settings["font"]},{font.ass_size(size)},{ass_colour(primary)},{ass_colour(primary)},{ass_colour(settings["outline"])},{ass_colour("#000000", 0x78)},-1,0,0,0,100,100,0,0,1,{outline},{max(2, outline // 2)},5,40,40,0,1
Style: Title,{settings["font"]},{font.ass_size(title_size)},{ass_colour("#111111")},{ass_colour("#111111")},{ass_colour("#FFFFFF")},{ass_colour("#000000", 0xFF)},-1,0,0,0,100,100,0,0,3,{round(title_size * 0.28)},0,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    if title and title_until > 0:
        # Two lines is the aim. A long headline is set a little smaller before it is given a third.
        text_size = title_size
        lines = wrap_title(display_word(title, False), font, text_size)
        if len(lines) > 2:
            text_size = round(title_size * 0.86)
            lines = wrap_title(display_word(title, False), font, text_size)
        text = "\\N".join(lines)
        resize = f"\\fs{font.ass_size(text_size)}" if text_size != title_size else ""
        fade = "\\fad(0,250)" if title_until < duration - 0.3 else ""
        block = len(lines) * text_size * font.line_ratio + 2 * round(title_size * 0.28)
        y = title_y
        if title_floor is not None:
            y = min(y, title_floor - block / 2)
        y = int(max(y, TOP_SAFE + block / 2))
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(min(title_until, duration))},Title,,0,0,0,,"
            f"{{\\an5\\pos({WIDTH // 2},{y}){resize}{fade}}}{text}"
        )

    if settings.get("show", True):
        chunks = chunk_words(words, font, size, int(settings["max_words"]), bool(settings["uppercase"]))
        for n, chunk in enumerate(chunks):
            following = chunks[n + 1].start if n + 1 < len(chunks) else duration
            chunk_end = min(following, max(chunk.end, chunk.start + 0.25) + LINGER)
            if following - chunk.end < 0.12:
                chunk_end = following
            width = font.width(chunk.text, size)
            squeeze = min(1.0, MAX_LINE / width) if width else 1.0
            for i, (_, start, _) in enumerate(chunk.words):
                until = chunk.words[i + 1][1] if i + 1 < len(chunk.words) else chunk_end
                if until - start < 0.02:
                    continue
                tags = f"\\an5\\pos({WIDTH // 2},{caption_y})"
                if squeeze < 1.0:
                    tags += f"\\fscx{squeeze * 100:.0f}\\fscy{squeeze * 100:.0f}"
                elif i == 0:
                    tags += "\\fscx92\\fscy92\\t(0,70,\\fscx100\\fscy100)"
                parts = [
                    f"{{{_inline(highlight)}}}{w[0]}{{{_inline(primary)}}}" if k == i else w[0]
                    for k, w in enumerate(chunk.words)
                ]
                events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(until)},Cap,,0,0,0,,{{{tags}}}{' '.join(parts)}")

    return header + "\n".join(events) + "\n"
