"""Cut, reframe, caption and encode each picked clip with ffmpeg."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import captions, reframe, silence
from .reframe import OUT_H, OUT_W, Segment
from .transcript import Transcript
from .util import REPO, ClipperError, clock, log, need, run, slugify

FPS = 30
TARGET_LUFS = -14.0  # the loudness streaming and social platforms normalize to
PEAK_LIMIT = 0.84  # about -1.5 dBFS


def render_all(info: dict, picks: dict, transcript: Transcript, cfg: dict, workdir: Path) -> list[dict]:
    """Render every picked clip. `info` is probe.json: one media file, or several for a long recording."""
    need("ffmpeg")
    outdir = workdir / "out"
    scratch = workdir / "tmp"
    for folder in (outdir, scratch):
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    shutil.copytree(captions.FONTS_DIR, scratch / "fonts")

    silences = silence.find_silences(info["audio"])
    covers: dict[str, Path | None] = {}
    results = []
    for clip in picks["clips"]:
        started = time.monotonic()
        # For a long recording each clip is cut from the file holding its stretch, at that stretch's place in it.
        part = transcript.part_at(float(clip["start"]))
        media = info["media"][part.file] if part is not None else info
        source = Path(media["path"])
        if media["path"] not in covers:
            covers[media["path"]] = find_cover(source, media, cfg, scratch)
        try:
            result = render_clip(source, media, clip, transcript, silences, cfg, outdir, scratch, covers[media["path"]], part)
        except ClipperError as exc:
            log.warning("Clip %d failed and was skipped: %s", clip["rank"], str(exc).splitlines()[-1][:300])
            continue
        log.info(
            "Clip %d/%d  %s  %s  layout %s  (%.0fs to render)",
            clip["rank"], len(picks["clips"]), clock(result["duration"]), result["file"], result["layout"],
            time.monotonic() - started,
        )  # fmt: skip
        results.append(result)
    if not results:
        raise ClipperError("Every clip failed to render. The log above has the ffmpeg error for each.")
    contact_sheet(outdir, results)
    shutil.rmtree(scratch, ignore_errors=True)
    return results


def render_clip(source, info, clip, transcript, silences, cfg, outdir: Path, scratch: Path, cover, part=None) -> dict:
    """Cut one clip. Words and cut points are timed on the transcript's clock; `shift` maps that to the file's."""
    words = transcript.words
    first, last = int(clip["first_word"]), int(clip["last_word"])
    if part is None:
        shift, origin = 0.0, 0.0
        start, end = silence.snap(words, first, last, silences, info["duration"])
    else:
        shift, origin = part.offset - part.start, part.origin - part.start
        start, end = silence.snap(words, first, last, silences, part.end)
        start, end = max(start, part.start), min(end, part.end)  # never reach into the neighbouring stretch
        if end - start < 1.0:
            raise ClipperError("the picked moment falls outside its stretch")

    parts = [(start, end)]
    if cfg["clips"]["tighten"]:
        parts = silence.tighten(start, end, silences, words, float(cfg["clips"]["max_pause"]))
    # Everything below works in whole frames counted from the clip start, so sound and picture stay locked.
    frames = []
    for a, b in parts:
        fa, fb = round((a - start) * FPS), round((b - start) * FPS)
        if fb - fa >= 2:
            frames.append((fa, fb))
    if not frames:
        raise ClipperError("nothing left to show after trimming pauses")
    parts = [(start + fa / FPS, start + fb / FPS) for fa, fb in frames]
    total_frames = sum(fb - fa for fa, fb in frames)
    duration = total_frames / FPS
    span = frames[-1][1] / FPS

    name = f"{clip['rank']:02d}-{slugify(clip['title'])}"
    if info["has_video"]:
        segments = reframe.frame_for_layout(source, start + shift, span, info, cfg["clips"]["layout"], cfg.get("kind") == "stream")
        pieces = cut_segments(segments, frames)
        layout = reframe.describe([seg for _, _, seg in pieces])
        video_graph = video_chain(pieces, info["width"], info["height"])
        caption_y, title_y, title_floor = text_positions(pieces, info)
    else:
        layout = "audiogram"
        video_graph = audiogram_chain(duration, cover is not None, cfg["captions"]["highlight"])
        caption_y, title_y, title_floor = (1420, 190, 280) if cover else (1260, 430, None)

    shown = [
        (w.text, silence.retime(w.start, parts), silence.retime(w.end, parts))
        for w in words[first : last + 1]
    ]
    title_mode = cfg["title"]["show"]
    title_until = {"off": 0.0, "intro": float(cfg["title"]["seconds"]), "always": duration}[title_mode]
    ass = captions.build_ass(
        shown,
        cfg["captions"],
        duration=duration,
        caption_y=caption_y,
        title=clip["title"],
        title_y=title_y,
        title_floor=title_floor,
        title_until=title_until,
    )
    (scratch / f"{name}.ass").write_text(ass, encoding="utf-8")

    inputs = ["-ss", f"{start + shift:.3f}", "-t", f"{span + 1:.3f}", "-i", str(Path(source).resolve())]
    if not info["has_video"] and cover:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(cover)]

    gain = measure_loudness(inputs, frames)
    graph = (
        f"{audio_chain(frames, gain, wave=not info['has_video'])};"
        f"{video_graph};"
        f"[vcat]subtitles=f={name}.ass:fontsdir=fonts,format=yuv420p[vout]"
    )
    target = outdir / f"{name}.mp4"
    run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
         "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", str(cfg["render"]["preset"]), "-crf", str(cfg["render"]["crf"]),
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", "-t", f"{duration:.3f}", str(target.resolve())],
        cwd=scratch,
        timeout=3600,
    )  # fmt: skip

    check_render(target, duration)

    thumb = outdir / f"{name}.jpg"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1.2, duration / 2):.2f}", "-i", target,
         "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", thumb], check=False)  # fmt: skip

    return {
        "rank": clip["rank"],
        "file": target.name,
        "thumb": thumb.name if thumb.is_file() else "",
        "title": clip["title"],
        "caption": clip.get("caption", ""),
        "hashtags": clip.get("hashtags", []),
        "why": clip.get("why", ""),
        "score": clip.get("score", 0),
        "source_start": round(start + origin, 2),  # on the original recording's clock
        "source_end": round(end + origin, 2),
        "duration": round(duration, 2),
        "trimmed": round((end - start) - duration, 2),
        "layout": layout,
        "text": " ".join(w.text for w in words[first : last + 1]),
    }


def check_render(target: Path, expected: float) -> None:
    """Refuse a clip whose picture and sound came out different lengths: that is a sync fault."""
    proc = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", target])
    lengths = {}
    for stream in json.loads(proc.stdout).get("streams", []):
        try:
            lengths[stream["codec_type"]] = float(stream["duration"])
        except (KeyError, ValueError):
            continue
    video, audio = lengths.get("video"), lengths.get("audio")
    if video is None or audio is None:
        target.unlink(missing_ok=True)
        raise ClipperError("the rendered file is missing its picture or its sound")
    if abs(video - audio) > 0.05 or abs(video - expected) > 0.05:
        target.unlink(missing_ok=True)
        raise ClipperError(f"picture is {video:.2f}s and sound is {audio:.2f}s, expected {expected:.2f}s")


# ------------------------------------------------------------- filter graphs


def cut_segments(segments: list[Segment], frames: list[tuple[int, int]]) -> list[tuple[int, int, Segment]]:
    """Intersect layout segments with the kept parts. Returns (first frame, end frame, layout)."""
    pieces: list[tuple[int, int, Segment]] = []
    for fa, fb in frames:
        for seg in segments:
            a, b = max(fa, round(seg.start * FPS)), min(fb, round(seg.end * FPS))
            if b - a < 1:
                continue
            previous = pieces[-1] if pieces else None
            if previous and previous[1] == a and previous[2].kind == seg.kind and previous[2].params == seg.params:
                pieces[-1] = (previous[0], b, seg)
            else:
                pieces.append((a, b, seg))
    # Rounding can leave a kept frame uncovered at a part's edge; stretch the neighbours over it.
    covered: list[tuple[int, int, Segment]] = []
    for fa, fb in frames:
        inside = [p for p in pieces if fa <= p[0] < fb]
        if not inside:
            inside = [(fa, fb, Segment(0, 0, "fit"))]
        inside[0] = (fa, inside[0][1], inside[0][2])
        inside[-1] = (inside[-1][0], fb, inside[-1][2])
        covered += inside
    return covered


def video_chain(pieces, width: int, height: int) -> str:
    count = len(pieces)
    chains = []
    # 0:V:0 is the first real video stream (not cover art). Frames are put on an exact 30 fps grid,
    # and the last one is held briefly in case the picture ends a moment before the sound.
    head = (
        f"[0:V:0]fps={FPS}:start_time=0,tpad=stop_mode=clone:stop_duration=1.5,"
        f"scale={width}:{height}:flags=bicubic,format=yuv420p,setsar=1"
    )
    chains.append(head + (f",split={count}" + "".join(f"[v{i}]" for i in range(count)) if count > 1 else "[v0]"))
    for i, (a, b, seg) in enumerate(pieces):
        # Frame numbers, not seconds: after fps=...:start_time=0, frame n is exactly n/FPS into the clip.
        trim = f"trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS"
        label = f"[p{i}]" if count > 1 else "[vcat]"
        chains.append(f"[v{i}]{trim},{reframe.layout_filter(seg, width, height, f's{i}')}{label}")
    if count > 1:
        chains.append("".join(f"[p{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0[vcat]")
    return ";".join(chains)


def audiogram_chain(duration: float, has_cover: bool, colour: str = "#FFFFFF") -> str:
    """Picture for an audio-only source: cover art if there is any, and a moving waveform."""
    wave = (
        f"[aw]showwaves=s=960x280:mode=cline:rate={FPS}:colors=0x{colour[1:]}:scale=sqrt:draw=full,"
        f"format=yuva420p[wave]"
    )
    if has_cover:
        return (
            f"[1:v]fps={FPS},format=yuv420p,split=2[c1][c2];"
            f"[c1]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,crop={OUT_W // 4}:{OUT_H // 4},"
            f"boxblur=12:2,scale={OUT_W}:{OUT_H}:flags=bilinear,eq=brightness=-0.22:saturation=0.85[bg];"
            f"[c2]scale=640:640:force_original_aspect_ratio=increase,crop=640:640[art];"
            f"[bg][art]overlay=(W-w)/2:300[base];{wave};"
            f"[base][wave]overlay=(W-w)/2:1000:shortest=1,trim=duration={duration:.3f},setsar=1[vcat]"
        )
    return (
        f"color=c=0x12161c:s={OUT_W}x{OUT_H}:r={FPS}:d={duration:.3f}[base];{wave};"
        f"[base][wave]overlay=(W-w)/2:700:shortest=1,setsar=1[vcat]"
    )


def audio_chain(frames: list[tuple[int, int]], gain: str, wave: bool = False) -> str:
    """Join the kept parts (tiny fades hide the joins) and level the loudness.

    With `wave`, a second copy of the sound is exposed as [aw] to draw the waveform from.
    """
    # The first audio track, the same one that was transcribed, padded in case it ends a moment early.
    head = "[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad=pad_dur=1.5"
    count = len(frames)
    chains = []
    if count == 1:
        a, b = frames[0]
        chains.append(f"{head},atrim=start={a / FPS:.5f}:end={b / FPS:.5f},asetpts=PTS-STARTPTS[acat]")
    else:
        chains.append(head + f",asplit={count}" + "".join(f"[a{i}]" for i in range(count)))
        for i, (a, b) in enumerate(frames):
            length = (b - a) / FPS
            fades = []
            if i > 0:
                fades.append("afade=t=in:st=0:d=0.008")
            if i < count - 1:
                fades.append(f"afade=t=out:st={max(0.0, length - 0.008):.5f}:d=0.008")
            chain = ",".join([f"atrim=start={a / FPS:.5f}:end={b / FPS:.5f}", "asetpts=PTS-STARTPTS", *fades])
            chains.append(f"[a{i}]{chain}[q{i}]")
        chains.append("".join(f"[q{i}]" for i in range(count)) + f"concat=n={count}:v=0:a=1[acat]")
    chains.append(f"[acat]{gain},aresample=48000" + (",asplit=2[aout][aw]" if wave else "[aout]"))
    return ";".join(chains)


def measure_loudness(inputs: list[str], frames) -> str:
    """Measure the clip, then return a filter that brings it to the target with one steady gain.

    A fixed gain plus a peak limiter keeps speech sounding natural and, unlike a
    dynamic normalizer, cannot shift the sound against the picture.
    """
    a, b = frames[0][0] / FPS, frames[-1][1] / FPS
    proc = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-y", *inputs[: _first_image(inputs)], "-map", "0:a:0",
         "-af", f"atrim=start={a:.3f}:end={b:.3f},loudnorm=print_format=json", "-f", "null", "-"],
        check=False,
        timeout=900,
    )  # fmt: skip
    gain = 0.0
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    if match:
        try:
            measured = float(json.loads(match.group(0))["input_i"])
            if -70 < measured < 0:
                gain = max(-20.0, min(20.0, TARGET_LUFS - measured))
        except (KeyError, ValueError):
            pass
    return f"volume={gain:.2f}dB,alimiter=limit={PEAK_LIMIT}:attack=5:release=50:level=false:latency=1"


def _first_image(inputs: list[str]) -> int:
    return inputs.index("-loop") if "-loop" in inputs else len(inputs)


def text_positions(pieces, info: dict) -> tuple[int, int, float | None]:
    """Where captions and the headline go: (caption centre y, headline centre y, headline's lowest edge).

    The last value keeps the headline above the head of whoever is on screen when the clip opens.
    """
    weight: dict[str, int] = {}
    for a, b, seg in pieces:
        weight[seg.kind] = weight.get(seg.kind, 0) + (b - a)
    ceiling = reframe.head_top(pieces[0][2], info["width"], info["height"])
    ceiling = ceiling - 24 if ceiling is not None else None
    if max(weight, key=weight.get) == "split":
        # Camera above, gameplay below: the headline sits on the streamer's chest and the captions
        # just under the seam, which keeps both off the face and off the middle of the game.
        return reframe.SPLIT_TOP + 140, reframe.SPLIT_TOP - 60, None
    if "stack" in weight:
        return OUT_H // 2, 200, ceiling  # captions on the seam between the two people
    if max(weight, key=weight.get) == "fit":
        longest = max((p for p in pieces if p[2].kind == "fit"), key=lambda p: p[1] - p[0])[2]
        band_top, band_bottom = reframe.fit_band(longest.params, info["width"], info["height"])
        caption_y = int(min(max(band_bottom + 150, OUT_H * 0.66), OUT_H * 0.775))
        title_y = int(max(min(band_top - 150, OUT_H * 0.17), OUT_H * 0.115))
        if pieces[0][2].kind == "fit":
            ceiling = reframe.fit_band(pieces[0][2].params, info["width"], info["height"])[0] - 24
        return caption_y, title_y, ceiling
    return int(OUT_H * 0.66), int(OUT_H * 0.165), ceiling


# -------------------------------------------------------------------- extras


def find_cover(source: Path, info: dict, cfg: dict, scratch: Path) -> Path | None:
    """Artwork for audio-only episodes: the client's file if set, else art embedded in the audio."""
    if info["has_video"]:
        return None
    configured = str(cfg.get("cover") or "").strip()
    if configured:
        path = (REPO / configured).resolve()
        if path.is_file() and REPO in path.parents:
            return path
        log.warning("Cover image %s not found; continuing without it.", configured)
    if info.get("has_cover"):
        target = scratch / "cover.png"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source, "-an", "-frames:v", "1", target], check=False)
        if target.is_file():
            return target
    return None


def contact_sheet(outdir: Path, results: list[dict]) -> None:
    """One image showing a frame from every clip, for a quick look before opening any of them."""
    thumbs = [outdir / r["thumb"] for r in results if r.get("thumb")]
    if not thumbs:
        return
    columns = min(5, len(thumbs))
    rows = -(-len(thumbs) // columns)
    inputs = [arg for thumb in thumbs for arg in ("-i", str(thumb))]
    scaled = "".join(f"[{i}:v]scale=360:640,setsar=1[t{i}];" for i in range(len(thumbs)))
    if len(thumbs) == 1:
        graph = scaled.rstrip(";").replace("[t0]", "[sheet]")
    else:
        layout = "|".join(f"{(i % columns) * 368}_{(i // columns) * 648}" for i in range(len(thumbs)))
        graph = scaled + "".join(f"[t{i}]" for i in range(len(thumbs))) + f"xstack=inputs={len(thumbs)}:layout={layout}:fill=black[sheet]"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[sheet]",
         "-frames:v", "1", "-q:v", "4", outdir / "contact-sheet.jpg"], check=False)  # fmt: skip
    for thumb in thumbs:
        thumb.unlink(missing_ok=True)
    for r in results:
        r.pop("thumb", None)
    log.debug("Contact sheet: %d clips in %dx%d", len(thumbs), columns, rows)
