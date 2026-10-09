"""Decide how each shot of a clip fills a vertical frame.

For every clip the source is scanned once for camera cuts and faces. Each shot
then gets one of these layouts:

  crop   one person: a 9:16 window centred on them
  stack  two people side by side: one above the other, captions on the seam
  fit    anything else: the whole picture over a blurred copy of itself
         (also used, zoomed in around them, for one person sitting very close to the camera)
  split  a live stream with a small camera box over gameplay: the camera on top,
         the middle of the gameplay below

The layout is fixed for the length of a shot, so the frame never drifts.
"""

from __future__ import annotations

import re
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .util import REPO, ClipperError, log

OUT_W, OUT_H = 1080, 1920
MODEL = REPO / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
SAMPLE_FPS = 3
SAMPLE_WIDTH = 640
STREAM_SAMPLE_WIDTH = 960  # a camera box is small, so stream footage is searched for faces at a larger size
SCENE_THRESHOLD = 0.30
MIN_SHOT = 0.5
MIN_FACE_HEIGHT = 0.07  # of the frame height; the main face must be at least this big
TINY_FACE_HEIGHT = 0.03  # below this a detection is ignored outright
TOO_CLOSE = 0.40  # a face wider than this share of a 9:16 crop makes the crop uncomfortably tight
MIN_FACE_SCORE = 0.7
SPLIT_TOP = 656  # height of the camera panel in the split layout; gameplay gets the rest
CAM_MAX_FACE = 0.20  # on a stream, a lone face smaller than this share of the frame height is a camera box
CAM_FACE_SHARE = 0.20  # how wide the face is made within the camera panel


@dataclass
class Face:
    cx: float  # all four are fractions of the frame
    cy: float
    w: float
    h: float
    score: float = 1.0


@dataclass
class Segment:
    start: float  # seconds from the start of the clip
    end: float
    kind: str  # crop | stack | fit | split
    params: dict = field(default_factory=dict)


# ------------------------------------------------------------------ scanning


def analyze(path, start: float, duration: float, width: int, height: int, sample_width: int = SAMPLE_WIDTH):
    """Scan one stretch of video. Returns (cut times, [(time, [Face, ...]), ...])."""
    import cv2

    if not MODEL.is_file():
        raise ClipperError(f"Face model missing at {MODEL.relative_to(REPO)}.")
    sample_width = min(sample_width, max(2, width // 2 * 2))  # never enlarge a small source
    small_h = max(2, round(sample_width * height / width / 2) * 2)
    # One decode feeds both jobs: a per-frame scene-change score, and frames for face detection.
    graph = (
        f"[0:V:0]scale={sample_width}:{small_h}:flags=fast_bilinear,split=2[c][f];"
        f"[c]scale=320:-2:flags=fast_bilinear,select='gte(scene,0)',"
        f"metadata=mode=print:key=lavfi.scene_score:file=scores.txt[cuts];"
        f"[f]fps={SAMPLE_FPS}[frames]"
    )
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
           "-i", str(Path(path).resolve()), "-an", "-sn", "-filter_complex", graph,
           "-map", "[cuts]", "-f", "null", "-",
           "-map", "[frames]", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]  # fmt: skip

    detector = cv2.FaceDetectorYN.create(str(MODEL), "", (sample_width, small_h), 0.6, 0.3, 50)
    frame_bytes = sample_width * small_h * 3
    samples = []
    with tempfile.TemporaryDirectory(prefix="clipper-scan-") as scratch:
        with open(Path(scratch) / "errors.txt", "w+b") as errors:
            proc = subprocess.Popen(cmd, cwd=scratch, stdout=subprocess.PIPE, stderr=errors)
            index = 0
            while True:
                raw = proc.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
                image = np.frombuffer(raw, dtype=np.uint8).reshape(small_h, sample_width, 3)
                _, found = detector.detect(image)
                faces = []
                for row in found if found is not None else []:
                    x, y, w, h, score = (float(v) for v in (row[0], row[1], row[2], row[3], row[-1]))
                    faces.append(Face((x + w / 2) / sample_width, (y + h / 2) / small_h, w / sample_width, h / small_h, score))
                samples.append((index / SAMPLE_FPS, faces))
                index += 1
            proc.stdout.close()
            code = proc.wait()
            errors.seek(0)
            text = errors.read().decode("utf-8", "replace")
        scores_file = Path(scratch) / "scores.txt"
        scores_text = scores_file.read_text() if scores_file.is_file() else ""
    if code != 0 and not samples:
        tail = "\n".join(text.strip().splitlines()[-6:])
        raise ClipperError(f"Could not read video frames for framing:\n{tail}")
    times = [float(v) for v in re.findall(r"pts_time:\s*([0-9.]+)", scores_text)]
    scores = [float(v) for v in re.findall(r"lavfi\.scene_score=([0-9.]+)", scores_text)]
    cuts = find_cuts(times[: len(scores)], scores)
    return [c for c in cuts if 0 < c < duration], samples


def find_cuts(times: list[float], scores: list[float]) -> list[float]:
    """Camera cuts from per-frame scene-change scores.

    A hard cut is a one-frame spike. It counts when it is large outright, or
    when it towers over the frames around it, which separates a cut between two
    similar-looking cameras from ordinary movement. A spurious cut costs
    nothing: if the framing on both sides matches, the two shots are merged.
    """
    cuts = []
    count = len(scores)
    for i, score in enumerate(scores):
        if score < 0.06:
            continue
        near = scores[max(0, i - 3) : i] + scores[i + 1 : i + 4]
        if near and score < max(near):
            continue
        around = scores[max(0, i - 15) : i] + scores[i + 1 : min(count, i + 16)]
        baseline = statistics.median(around) if around else 0.0
        if score >= SCENE_THRESHOLD or score >= 5 * baseline + 0.03:
            cuts.append(times[i])
    return cuts


# ------------------------------------------------------------------ planning


def plan(cuts, samples, duration: float, width: int, height: int, layout: str = "auto", stream: bool = False) -> list[Segment]:
    """Turn cut times and face samples into layout segments covering [0, duration].

    `stream` says the footage is a live stream, where a small steady face is a
    camera box laid over gameplay and not a person far from the lens.
    """
    if layout == "fit":
        return [Segment(0.0, duration, "fit")]
    if layout == "split" or (stream and layout == "auto"):
        # A camera box that is there for most of the clip decides the whole clip. Game footage
        # is full of sudden changes that look like cuts, and the layout must not flicker with them.
        cam = camera_box([faces for _, faces in samples], CAM_MAX_FACE if layout == "auto" else 1.0)
        if cam is not None:
            return [Segment(0.0, duration, "split", {"cam": cam})]
        if layout == "split":
            return [Segment(0.0, duration, "fit")]
    window = min(1.0, (OUT_W / OUT_H) / (width / height))  # crop width as a share of the frame
    segments: list[Segment] = []
    for t0, t1 in shot_bounds(cuts, duration):
        in_shot = [(t, faces) for t, faces in samples if t0 <= t < t1]
        cam = camera_box([faces for _, faces in in_shot], CAM_MAX_FACE) if stream and layout == "auto" else None
        if cam is not None:
            segments.append(Segment(t0, t1, "split", {"cam": cam}))
            continue
        shot = [(t, significant(faces)) for t, faces in in_shot]
        if stream and layout == "auto":
            # On a stream a small face is a camera box seen too rarely to trust, never somebody to zoom in on.
            shot = [(t, [f for f in faces if f.h > CAM_MAX_FACE]) for t, faces in shot]
        segments += plan_shot(t0, t1, shot, window, layout)
    segments = stabilize(segments)
    if layout == "auto":
        for seg in segments:
            # Someone sitting right at the camera would fill a 9:16 crop edge to edge.
            # Show a wider window around them over a blurred fill instead.
            if seg.kind == "crop" and seg.params.get("w", 0) > TOO_CLOSE * window:
                seg.kind = "fit"
    return segments


def shot_bounds(cuts, duration: float) -> list[tuple[float, float]]:
    edges = [0.0]
    for cut in sorted(cuts):
        if cut - edges[-1] >= MIN_SHOT and duration - cut >= MIN_SHOT:
            edges.append(float(cut))
    edges.append(float(duration))
    return list(zip(edges[:-1], edges[1:]))


def significant(faces: list[Face]) -> list[Face]:
    """The people a shot is about: the largest face and any of comparable size.

    A face much smaller than the largest is background (a poster, a crew member)
    and is dropped. A row of similar small faces all survive, which is how a
    crowd is told apart from two hosts. If even the largest face is tiny,
    nobody is prominent and the shot counts as having no faces.
    """
    good = [f for f in faces if f.score >= MIN_FACE_SCORE and f.h >= TINY_FACE_HEIGHT]
    if not good:
        return []
    biggest = max(f.h for f in good)
    if biggest < MIN_FACE_HEIGHT:
        return []
    return sorted((f for f in good if f.h >= 0.45 * biggest), key=lambda f: f.cx)


def camera_box(frames: list[list[Face]], max_face: float) -> dict | None:
    """The streamer's camera box, if these frames show one: a lone smallish face that stays put.

    Returns its median position and size, or None. `max_face` is the tallest a
    face may be and still count; above that the camera is filling the screen.
    """
    if not frames:
        return None
    found = []
    crowded = 0
    for faces in frames:
        good = [f for f in faces if f.score >= MIN_FACE_SCORE and f.h >= TINY_FACE_HEIGHT]
        if not good:
            continue
        biggest = max(good, key=lambda f: f.h)
        if biggest.h > max_face:
            crowded += 1  # somebody is filling the screen in this frame
        elif sum(1 for f in good if f.h >= 0.6 * biggest.h) == 1:
            found.append(biggest)
    if len(found) < 0.5 * len(frames) or crowded > 0.25 * len(frames):
        return None
    centre = _median_face(found)
    steady = sum(1 for f in found if abs(f.cx - centre.cx) < 0.05 and abs(f.cy - centre.cy) < 0.06)
    if steady < 0.8 * len(found):
        return None  # it moves about, so it is a person in a scene, not a fixed box
    return _box(centre)


def plan_shot(t0: float, t1: float, shot, window: float, layout: str) -> list[Segment]:
    total = len(shot)
    fallback = Segment(t0, t1, "crop", {"cx": 0.5}) if layout == "crop" else Segment(t0, t1, "fit")
    if total == 0:
        return [fallback]
    pairs = [faces for _, faces in shot if len(faces) >= 2]
    crowds = sum(1 for _, faces in shot if len(faces) >= 3)
    singles = [(t, max(faces, key=lambda f: f.h)) for t, faces in shot if faces]

    if crowds >= 0.5 * total and layout != "crop":
        return [Segment(t0, t1, "fit")]

    if len(pairs) >= 0.35 * total:
        left, right = _median_pair(pairs)
        fits_one_window = (right.cx + right.w * 0.9) - (left.cx - left.w * 0.9) <= window
        if layout == "crop" or (fits_one_window and layout != "stack"):
            cx = (left.cx + right.cx) / 2 if fits_one_window else max(left, right, key=lambda f: f.h).cx
            return [Segment(t0, t1, "crop", {"cx": cx})]
        return [Segment(t0, t1, "stack", {"top": _box(left), "bottom": _box(right)})]

    if len(singles) >= 0.4 * total:
        return _follow_one(t0, t1, singles, window)

    return [fallback]


def _median_pair(pairs) -> tuple[Face, Face]:
    lefts, rights = [], []
    for faces in pairs:
        two = sorted(sorted(faces, key=lambda f: -f.h)[:2], key=lambda f: f.cx)
        lefts.append(two[0])
        rights.append(two[1])
    return _median_face(lefts), _median_face(rights)


def _median_face(faces: list[Face]) -> Face:
    med = statistics.median
    return Face(med(f.cx for f in faces), med(f.cy for f in faces), med(f.w for f in faces), med(f.h for f in faces))


def _box(face: Face) -> dict:
    return {"cx": face.cx, "cy": face.cy, "w": face.w, "h": face.h}


def _follow_one(t0: float, t1: float, singles, window: float) -> list[Segment]:
    """One person on screen. Normally one crop; two if an undetected cut moved them."""
    xs = [face.cx for _, face in singles]
    if max(xs) - min(xs) < 0.5 * window:
        return [Segment(t0, t1, "crop", _box(_median_face([face for _, face in singles])))]

    ordered = sorted(xs)
    gaps = [(b - a, i) for i, (a, b) in enumerate(zip(ordered, ordered[1:]))]
    gap, at = max(gaps)
    low, high = ordered[: at + 1], ordered[at + 1 :]
    if gap < 0.25 * window or min(len(low), len(high)) < 0.2 * len(xs):
        return [Segment(t0, t1, "crop", _box(_median_face([face for _, face in singles])))]

    # Two distinct positions: follow whichever one the person is in, ignoring one-sample blips.
    split = (low[-1] + high[0]) / 2
    sides = [x > split for x in xs]
    for i in range(1, len(sides) - 1):
        if sides[i] != sides[i - 1] and sides[i] != sides[i + 1]:
            sides[i] = sides[i - 1]
    spots = {
        side: _box(_median_face([face for (_, face), s in zip(singles, sides) if s == side]))
        for side in (False, True)
        if side in sides
    }
    segments, begin = [], t0
    for i in range(1, len(sides)):
        if sides[i] != sides[i - 1]:
            switch = (singles[i - 1][0] + singles[i][0]) / 2
            if switch - begin >= MIN_SHOT and t1 - switch >= MIN_SHOT:
                segments.append(Segment(begin, switch, "crop", dict(spots[sides[i - 1]])))
                begin = switch
    segments.append(Segment(begin, t1, "crop", dict(spots[sides[-1]])))
    return segments


def stabilize(segments: list[Segment]) -> list[Segment]:
    """Reuse the same framing each time a camera angle returns, and merge equal neighbours."""
    seen_crops: list[dict] = []
    seen_stacks: list[dict] = []
    seen_cams: list[dict] = []
    for seg in segments:
        if seg.kind == "split":
            cam = seg.params["cam"]
            match = next((c for c in seen_cams if abs(c["cam"]["cx"] - cam["cx"]) < 0.04 and abs(c["cam"]["cy"] - cam["cy"]) < 0.05), None)
            if match is None:
                seen_cams.append(seg.params)
            else:
                seg.params = match
        elif seg.kind == "crop":
            match = next((c for c in seen_crops if abs(c["cx"] - seg.params["cx"]) < 0.04), None)
            if match is None:
                seen_crops.append(seg.params)
            else:
                seg.params = match
        elif seg.kind == "stack":
            match = next(
                (s for s in seen_stacks
                 if abs(s["top"]["cx"] - seg.params["top"]["cx"]) < 0.04
                 and abs(s["bottom"]["cx"] - seg.params["bottom"]["cx"]) < 0.04),
                None,
            )  # fmt: skip
            if match is None:
                seen_stacks.append(seg.params)
            else:
                seg.params = match
    merged: list[Segment] = []
    for seg in segments:
        if merged and merged[-1].kind == seg.kind and merged[-1].params == seg.params:
            merged[-1].end = seg.end
        else:
            merged.append(seg)
    return merged


# ------------------------------------------------------------------ geometry


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def crop_box(params: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """(w, h, x, y) of a 9:16 window in the source.

    Centred on the person where possible. When their face is small in the frame
    the window tightens around them, up to 2x, so they are not lost on a phone.
    """
    full = min(width, height * OUT_W / OUT_H)
    w = full
    if params.get("w"):
        wanted = 5.0 * params["w"] * width  # face about a fifth of the frame width
        if wanted < 0.85 * full:
            w = max(wanted, full / 2, min(full, 400))
    w = _even(w)
    h = _even(min(height, w * OUT_H / OUT_W))
    x = int(round(min(max(params.get("cx", 0.5) * width - w / 2, 0), width - w)))
    if h < height and params.get("cy"):
        y = int(round(min(max(params["cy"] * height - 0.38 * h, 0), height - h)))
    else:
        y = int(round((height - h) / 2))
    return w, h, x, y


def stack_boxes(top: dict, bottom: dict, width: int, height: int):
    """Two (w, h, x, y) windows, each shaped for half of the vertical frame."""
    shape = OUT_W / (OUT_H / 2)
    left, right = sorted((top, bottom), key=lambda person: person["cx"])
    face = max(top["w"], bottom["w"]) * width
    widest = min(abs(right["cx"] - left["cx"]) * width * 1.15, width, height * shape)
    centred = 2 * min(left["cx"], 1 - right["cx"]) * width  # widest window that keeps both people centred
    w = min(4.0 * face, centred, widest)  # aim for a face about a quarter of the panel width
    w = max(w, min(3.0 * face, widest), min(widest, 400))
    w = _even(w)
    h = _even(min(height, w / shape))
    boxes = []
    # The upper person sits a little low in their panel, leaving room above for the headline.
    for person, anchor in ((top, 0.62), (bottom, 0.42)):
        x = int(round(min(max(person["cx"] * width - w / 2, 0), width - w)))
        y = int(round(min(max(person["cy"] * height - anchor * h, 0), height - h)))
        boxes.append((w, h, x, y))
    return boxes


def split_boxes(cam: dict, width: int, height: int):
    """((w, h, x, y) of the camera window, (w, h, x, y) of the gameplay window), both in the source."""
    shape = OUT_W / SPLIT_TOP
    w = cam["w"] * width / CAM_FACE_SHARE
    w = _even(min(max(w, 160), width, height * shape))
    h = _even(min(height, w / shape))
    x = min(max(cam["cx"] * width - w / 2, 0), width - w)
    y = min(max(cam["cy"] * height - 0.44 * h, 0), height - h)

    low = OUT_H - SPLIT_TOP
    gw = _even(min(width, height * OUT_W / low))
    gh = _even(min(height, gw * low / OUT_W))
    gx = (width - gw) / 2
    # Nudge the gameplay window away from the camera box, so less of the streamer shows twice.
    # Only a nudge: the middle of the screen is where the game happens and it has to stay near the middle.
    room = 0.06 * width
    if x + w / 2 < width / 2:
        gx += min(max((x + w) - gx, 0), room, width - gw - gx)
    else:
        gx -= min(max((gx + gw) - x, 0), room, gx)
    return (w, h, int(round(x)), int(round(y))), (gw, gh, int(round(gx)), int(round((height - gh) / 2)))


def head_top(seg: Segment, width: int, height: int) -> float | None:
    """Where the top of the (upper) person's head lands in the output frame, in pixels."""
    if seg.kind == "crop" and seg.params.get("h"):
        person, (w, h, x, y), out_h = seg.params, crop_box(seg.params, width, height), OUT_H
    elif seg.kind == "stack":
        person, out_h = seg.params["top"], OUT_H // 2
        w, h, x, y = stack_boxes(seg.params["top"], seg.params["bottom"], width, height)[0]
    else:
        return None
    hairline = (person["cy"] - 0.85 * person.get("h", person["w"] * 1.2)) * height
    return max(0.0, (hairline - y) * out_h / h)


def layout_filter(seg: Segment, width: int, height: int, tag: str) -> str:
    """ffmpeg filter chain taking one video stream to a 1080x1920 frame. `tag` keeps labels unique."""
    if seg.kind == "crop":
        w, h, x, y = crop_box(seg.params, width, height)
        return f"crop={w}:{h}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=bicubic,setsar=1"
    if seg.kind == "stack":
        (w1, h1, x1, y1), (w2, h2, x2, y2) = stack_boxes(seg.params["top"], seg.params["bottom"], width, height)
        half = OUT_H // 2
        return (
            f"split=2[{tag}a][{tag}b];"
            f"[{tag}a]crop={w1}:{h1}:{x1}:{y1},scale={OUT_W}:{half}:flags=bicubic[{tag}t];"
            f"[{tag}b]crop={w2}:{h2}:{x2}:{y2},scale={OUT_W}:{half}:flags=bicubic[{tag}u];"
            f"[{tag}t][{tag}u]vstack=inputs=2,setsar=1"
        )
    if seg.kind == "split":
        (w1, h1, x1, y1), (w2, h2, x2, y2) = split_boxes(seg.params["cam"], width, height)
        return (
            f"split=2[{tag}a][{tag}b];"
            f"[{tag}a]crop={w1}:{h1}:{x1}:{y1},scale={OUT_W}:{SPLIT_TOP}:flags=bicubic[{tag}t];"
            f"[{tag}b]crop={w2}:{h2}:{x2}:{y2},scale={OUT_W}:{OUT_H - SPLIT_TOP}:flags=bicubic[{tag}u];"
            f"[{tag}t][{tag}u]vstack=inputs=2,setsar=1"
        )
    # fit: the picture (all of it, or a window around one person) over a blurred, darkened copy of itself
    w, h, x, y = fit_box(seg.params, width, height)
    inner_h = _even(OUT_W * h / w)
    window = "" if (w, h) == (width, height) else f"crop={w}:{h}:{x}:{y},"
    return (
        f"{window}split=2[{tag}a][{tag}b];"
        f"[{tag}a]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=10:2,scale={OUT_W}:{OUT_H}:flags=bilinear,"
        f"eq=brightness=-0.12:saturation=0.9[{tag}g];"
        f"[{tag}b]scale={OUT_W}:{inner_h}:flags=bicubic[{tag}f];"
        f"[{tag}g][{tag}f]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def fit_box(params: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """(w, h, x, y) of what the fit layout shows: the whole frame, or a 4:5 window around one person."""
    if not params.get("w"):
        return width, height, 0, 0
    narrowest = min(width, height * OUT_W / OUT_H)
    w = _even(min(width, max(params["w"] * width / 0.28, narrowest)))  # face about 28% of the width
    h = _even(min(height, w * 1.25))
    x = int(round(min(max(params["cx"] * width - w / 2, 0), width - w)))
    y = int(round(min(max(params.get("cy", 0.5) * height - 0.42 * h, 0), height - h)))
    return w, h, x, y


def fit_band(params: dict, width: int, height: int) -> tuple[float, float]:
    """Top and bottom of the picture inside the output frame, in pixels, for the fit layout."""
    w, h, _, _ = fit_box(params, width, height)
    inner = OUT_W * h / w
    return (OUT_H - inner) / 2, (OUT_H + inner) / 2


def describe(segments: list[Segment]) -> str:
    kinds = sorted({seg.kind for seg in segments})
    return "+".join(kinds) if kinds else "none"


def frame_for_layout(path: Path, start: float, duration: float, info: dict, layout: str, stream: bool = False) -> list[Segment]:
    """Scan and plan in one call; falls back to `fit` if the scan itself fails."""
    width, height = info["width"], info["height"]
    if layout == "fit":
        return [Segment(0.0, duration, "fit")]
    if width / height <= 0.62:
        # Already vertical: fill the frame.
        return [Segment(0.0, duration, "crop", {"cx": 0.5})]
    try:
        cuts, samples = analyze(path, start, duration, width, height, STREAM_SAMPLE_WIDTH if stream else SAMPLE_WIDTH)
    except ClipperError as exc:
        log.warning("Framing scan failed, using the safe layout: %s", str(exc).splitlines()[0])
        return [Segment(0.0, duration, "fit")]
    return plan(cuts, samples, duration, width, height, layout, stream)
