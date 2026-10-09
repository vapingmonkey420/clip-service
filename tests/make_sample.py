"""Build a short synthetic "podcast" for testing the pipeline end to end.

Speech comes from ffmpeg's built-in flite voices and the two hosts are drawn,
so nothing is downloaded and nobody real appears. Alongside the media file this
writes <name>.transcript.json with word timings estimated from the known
script, which lets the rest of the pipeline run without a speech model.

    python tests/make_sample.py work/sample.mp4                   # two hosts in one wide shot
    python tests/make_sample.py work/sample.mp4 --mode switched   # the camera cuts to whoever speaks
    python tests/make_sample.py work/sample.mp4 --mode screen     # no people on screen
    python tests/make_sample.py work/sample.mp3 --mode audio      # audio only
    python tests/make_sample.py work/stream.mp4 --mode stream     # a five-minute "gaming stream"

The stream is mostly quiet gameplay with a small camera in the corner, a few
things worth clipping, one loud moment, a hard scene change, and a closing
stretch with the camera full-screen: the shapes a real stream throws at the pipeline.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipper.transcript import Transcript, Word  # noqa: E402

RATE = 48000
SIZE = (1920, 1080)

# (speaker, line, pause afterwards in seconds)
SCRIPT = [
    ("A", "Welcome back to the show. Before we start, a quick thank you to everyone who left a review last week.", 0.4),
    ("B", "Yeah, thanks for that. We read all of them.", 1.3),
    ("A", "So here is the thing nobody tells you about growing a podcast.", 0.3),
    ("A", "Most shows do not have a content problem. They have a distribution problem.", 0.3),
    ("A", "You record a great hour, you post it once, and then you never talk about it again.", 1.6),
    ("B", "Right. And the fix is boring. Cut five short clips from every episode and post one a day.", 0.3),
    ("B", "That is it. That is the whole strategy.", 1.2),
    ("A", "Okay, let me check my notes for a second.", 0.5),
    ("A", "We also wanted to mention the newsletter. It goes out on Fridays.", 0.9),
    ("B", "Can I ask you something? What is the biggest mistake you made in your first year?", 0.4),
    ("A", "Easy. I waited for the audio to be perfect before I published anything.", 0.3),
    ("A", "I spent three months on gear and zero days on finding listeners.", 1.5),
    ("A", "If I started again, I would publish ten rough episodes first, and fix the sound later.", 1.0),
    ("B", "That is great advice. Alright, that is all the time we have. See you next week.", 0.6),
]
VOICES = {"A": "slt", "B": "rms"}

STREAM_SIZE = (1280, 720)
STREAM_CAM = (896, 426, 360, 270)  # x, y, width, height of the camera box over the gameplay
STREAM_SCENES = (136.0, 276.0)  # gameplay changes look at the first; the camera goes full-screen at the second
# (earliest start in seconds, line, loudness in dB relative to normal talking)
STREAM_SCRIPT = [
    (4.0, "Alright chat, we are live. Give me a second to get set up.", 0),
    (18.0, "Okay. Loading into the first match now.", 0),
    (40.0, "So I have to tell you what happened yesterday.", 0),
    (44.0, "I queued into ranked, and my teammate was nine years old.", 0),
    (49.0, "He carried the whole lobby. I got outplayed by a child.", 0),
    (54.5, "And he was nicer about it than I would have been.", 0),
    (75.0, "Reloading. Pushing the left side.", 0),
    (96.0, "Checking the map. Nothing here.", 0),
    (128.0, "Wait. Wait, wait, wait. It is one versus four.", 7),
    (133.5, "No way. No way! He did not see me!", 9),
    (138.5, "That is the best round I have ever played!", 9),
    (143.5, "I am shaking. Somebody clip that.", 4),
    (175.0, "Let me fix my settings quickly.", 0),
    (205.0, "Here is my honest opinion about ranked.", 0),
    (209.0, "Most players do not lose because of their aim.", 0),
    (213.5, "They lose because they refuse to talk to their team.", 0),
    (218.5, "Turn your microphone on and you will climb. It is that simple.", 0),
    (250.0, "Thanks for the follow. Welcome in.", 0),
    (284.0, "My cat just walked across the keyboard and bought three items.", 0),
    (289.5, "Honestly, that is a better build than mine.", 0),
    (318.0, "Alright, that is the stream. Thank you all for hanging out.", 0),
    (323.5, "See you tomorrow.", 0),
]


def ffmpeg(*args: str) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{proc.stderr[-800:]}")


def speak(text: str, voice: str, out: Path) -> np.ndarray:
    script = out.with_suffix(".txt")
    script.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"flite=textfile={script.name}:voice={voice}", "-ar", str(RATE), "-ac", "1", out.name],
        cwd=out.parent, capture_output=True, text=True,
    )  # fmt: skip
    if proc.returncode != 0:
        raise SystemExit(
            "Could not synthesize speech. This needs an ffmpeg build with libflite "
            f"(Ubuntu's ffmpeg package has it).\n{proc.stderr[-400:]}"
        )
    with wave.open(str(out), "rb") as fh:
        samples = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16)
    return _trim_silence(samples)


def _trim_silence(samples: np.ndarray, floor: int = 200) -> np.ndarray:
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > floor)
    if loud.size == 0:
        return samples
    return samples[max(0, loud[0] - 480) : loud[-1] + 480]


def build_audio(tmp: Path):
    """Return (samples, per-line (start, end)) for the whole script."""
    rng = np.random.default_rng(7)
    chunks, spans, cursor = [], [], 0
    lead = np.zeros(int(0.5 * RATE), dtype=np.int16)
    chunks.append(lead)
    cursor += lead.size
    for n, (speaker, line, pause) in enumerate(SCRIPT):
        voice = speak(line, VOICES[speaker], tmp / f"line{n:02d}.wav")
        spans.append((cursor / RATE, (cursor + voice.size) / RATE))
        gap = np.zeros(int(pause * RATE), dtype=np.int16)
        chunks += [voice, gap]
        cursor += voice.size + gap.size
    audio = np.concatenate(chunks).astype(np.float32)
    audio += rng.normal(0, 6, audio.size)  # faint room noise, about -75 dBFS
    return np.clip(audio, -32768, 32767).astype(np.int16), spans


def estimate_words(spans, lines=None) -> list[Word]:
    """Spread each line's words across its time span by length."""
    words: list[Word] = []
    for (start, end), line in zip(spans, lines or [line for _, line, _ in SCRIPT]):
        parts = line.split()
        weights = np.array([len(p) + 1.5 for p in parts], dtype=float)
        edges = start + np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * (end - start)
        for part, a, b in zip(parts, edges[:-1], edges[1:]):
            words.append(Word(part, float(a), float(b - 0.02)))
    return words


HOSTS = {
    "A": {"skin": (150, 180, 225), "hair": (40, 50, 70), "shirt": (160, 90, 60)},
    "B": {"skin": (120, 150, 200), "hair": (20, 20, 25), "shirt": (70, 140, 80)},
}


def draw_host(frame, cx: int, cy: int, size: int, skin, hair, shirt) -> None:
    """A simple head and shoulders, plain enough to draw and face-like enough to be detected.

    `size` is the height of the face in pixels; colours are blue-green-red.
    """
    w, h = int(size * 0.74), size
    shade = tuple(int(c * 0.8) for c in skin)
    line = int(max(2, h * 0.02))
    cv2.ellipse(frame, (cx, cy + int(h * 1.45)), (int(w * 1.5), int(h * 0.75)), 0, 180, 360, shirt, -1, cv2.LINE_AA)
    cv2.rectangle(frame, (cx - int(w * 0.22), cy + int(h * 0.35)), (cx + int(w * 0.22), cy + int(h * 0.8)), tuple(int(c * 0.9) for c in skin), -1)
    cv2.ellipse(frame, (cx, cy - int(h * 0.08)), (int(w * 0.58), int(h * 0.60)), 0, 0, 360, hair, -1, cv2.LINE_AA)
    cv2.ellipse(frame, (cx, cy), (w // 2, h // 2), 0, 0, 360, skin, -1, cv2.LINE_AA)
    cv2.ellipse(frame, (cx, cy - int(h * 0.36)), (int(w * 0.50), int(h * 0.22)), 0, 180, 360, hair, -1, cv2.LINE_AA)
    for side in (-1, 1):
        ear = (cx + side * int(w * 0.5), cy + int(h * 0.02))
        cv2.ellipse(frame, ear, (int(w * 0.07), int(h * 0.1)), 0, 0, 360, tuple(int(c * 0.93) for c in skin), -1, cv2.LINE_AA)
        eye = (cx + side * int(w * 0.21), cy - int(h * 0.08))
        cv2.ellipse(frame, eye, (int(w * 0.11), int(h * 0.045)), 0, 0, 360, (245, 245, 245), -1, cv2.LINE_AA)
        cv2.circle(frame, eye, int(h * 0.036), (60, 40, 30), -1, cv2.LINE_AA)
        cv2.circle(frame, eye, int(h * 0.016), (10, 10, 10), -1, cv2.LINE_AA)
        cv2.ellipse(frame, (eye[0], eye[1] - int(h * 0.085)), (int(w * 0.14), int(h * 0.03)), 0, 180, 360, hair, line, cv2.LINE_AA)
    nose = (cx - int(w * 0.04), cy + int(h * 0.13))
    cv2.line(frame, (cx, cy - int(h * 0.02)), nose, shade, line, cv2.LINE_AA)
    cv2.line(frame, nose, (cx + int(w * 0.04), nose[1]), shade, line, cv2.LINE_AA)
    cv2.ellipse(frame, (cx, cy + int(h * 0.26)), (int(w * 0.18), int(h * 0.06)), 0, 0, 180, (70, 70, 170), -1, cv2.LINE_AA)


def backdrop(tint) -> np.ndarray:
    ramp = np.linspace(0.6, 1.0, SIZE[1], dtype=np.float32)[:, None, None]
    return (np.ones((SIZE[1], SIZE[0], 3), np.float32) * np.array(tint, np.float32) * ramp).astype(np.uint8)


def build_frames(mode: str, tmp: Path) -> dict[str, Path]:
    """One still per speaker: what is on screen while that host talks."""
    frames: dict[str, Path] = {}
    for speaker in "AB":
        if mode == "wide":  # both hosts, always; a small light shows who is talking
            frame = backdrop((70, 45, 30))
            draw_host(frame, 480, 430, 280, **HOSTS["A"])
            draw_host(frame, 1440, 430, 280, **HOSTS["B"])
            cv2.circle(frame, (480 if speaker == "A" else 1440, 70), 12, (60, 60, 230), -1)
        elif mode == "switched":  # each host has a camera, framed a little off-centre
            frame = backdrop((70, 45, 30) if speaker == "A" else (45, 60, 40))
            draw_host(frame, 780 if speaker == "A" else 1180, 450, 300, **HOSTS[speaker])
        else:  # "screen": a slide, nobody in shot
            frame = backdrop((60, 60, 60))
            cv2.rectangle(frame, (160, 140), (1760, 940), (235, 235, 235), -1)
            for row, width in enumerate((1100, 900, 1300, 700)):
                cv2.rectangle(frame, (260, 260 + row * 150), (260 + width, 320 + row * 150), (90, 90, 90), -1)
            cv2.circle(frame, (200, 100), 12, (60, 60, 230) if speaker == "A" else (60, 200, 90), -1)
        frames[speaker] = tmp / f"frame{speaker}.png"
        cv2.imwrite(str(frames[speaker]), frame)
    return frames


def build_stream_audio(tmp: Path):
    """Game noise all the way through, with the streamer talking over it now and then."""
    rng = np.random.default_rng(11)
    voices, cursor = [], 0.0
    for n, (at, line, gain) in enumerate(STREAM_SCRIPT):
        voice = speak(line, VOICES["A"], tmp / f"line{n:02d}.wav").astype(np.float32)
        voice *= 0.45 * 10 ** (gain / 20)
        start = max(at, cursor + 0.35)
        voices.append((start, voice))
        cursor = start + voice.size / RATE
    total = cursor + 6.0
    t = np.arange(int(total * RATE)) / RATE
    audio = rng.normal(0, 90, t.size).astype(np.float32) + 160 * np.sin(2 * np.pi * 110 * t).astype(np.float32)
    for at in np.arange(7.0, total - 1, 6.5):  # a game sound every few seconds
        blip = (t >= at) & (t < at + 0.09)
        audio[blip] += 900 * np.sin(2 * np.pi * 880 * t[blip])
    spans = []
    for start, voice in voices:
        a = int(start * RATE)
        audio[a : a + voice.size] += voice
        spans.append((start, start + voice.size / RATE))
    return np.clip(audio, -32768, 32767).astype(np.int16), spans


def build_stream_video(tmp: Path, wav: Path, total: float, out: Path) -> None:
    """Moving shapes for gameplay with a camera box over them, then the camera full-screen."""
    width, height = STREAM_SIZE
    x, y, w, h = STREAM_CAM
    cam = np.full((h, w, 3), (34, 26, 22), np.uint8)
    draw_host(cam, w // 2, int(h * 0.44), 122, **HOSTS["A"])
    cv2.rectangle(cam, (0, 0), (w - 1, h - 1), (235, 235, 235), 3)
    cv2.imwrite(str(tmp / "cam.png"), cam)
    full = cv2.resize(backdrop((70, 45, 30)), (width, height))
    draw_host(full, 560, 300, 250, **HOSTS["A"])
    cv2.imwrite(str(tmp / "full.png"), full)

    first, second = STREAM_SCENES
    lengths = (first, second - first, total - second)
    looks = ("c0=0x14203a:c1=0x2f6b4f:c2=0x6b2f4f:c3=0x1a1a2e", "c0=0x3a2a14:c1=0x7a5a1e:c2=0x1e4a7a:c3=0x2e1a1a")
    shapes = (
        "drawbox=x='mod(t*260,{w})':y=300:w=90:h=90:color=0xffb000@0.9:t=fill,"
        "drawbox=x='{w}-100-mod(t*180,{w})':y=120:w=60:h=140:color=0xff3355@0.9:t=fill,"
        "drawbox=x=40:y={bar}:w='40+mod(t*30,400)':h=18:color=0x39d98a@0.9:t=fill"
    ).format(w=width, bar=height - 60)
    inputs, graph = [], ["[2:v]split=2[c0][c1]"]
    for i, look in enumerate(looks):
        inputs += ["-f", "lavfi", "-t", f"{lengths[i]:.3f}", "-i", f"gradients=s=320x180:r=30:speed=0.05:{look}:nb_colors=4"]
        graph.append(f"[{i}:v]scale={width}:{height}:flags=fast_bilinear,{shapes}[g{i}];[g{i}][c{i}]overlay={x}:{y}[s{i}]")
    inputs += ["-i", str(tmp / "cam.png"), "-loop", "1", "-framerate", "30", "-t", f"{lengths[2]:.3f}", "-i", str(tmp / "full.png"), "-i", str(wav)]
    graph.append(f"[3:v]fps=30,scale={width}:{height}[s2];[s0][s1][s2]concat=n=3:v=1:a=0,format=yuv420p[v]")
    ffmpeg(
        *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "4:a",
        # A keyframe exactly every two seconds, as streaming encoders produce, so the file splits cleanly into pieces.
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", str(out),
    )  # fmt: skip


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", type=Path)
    parser.add_argument("--mode", choices=("wide", "switched", "screen", "audio", "stream"), default="wide")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    lines = [line for _, line, _ in (STREAM_SCRIPT if args.mode == "stream" else SCRIPT)]

    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        audio, spans = build_stream_audio(tmp) if args.mode == "stream" else build_audio(tmp)
        total = audio.size / RATE
        wav = tmp / "episode.wav"
        with wave.open(str(wav), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(RATE)
            fh.writeframes(audio.tobytes())

        if args.mode == "audio":
            ffmpeg("-i", str(wav), "-c:a", "libmp3lame", "-b:a", "128k", str(args.out))
        elif args.mode == "stream":
            build_stream_video(tmp, wav, total, args.out)
        else:
            frames = build_frames(args.mode, tmp)
            starts = [span[0] for span in spans]
            cuts = [0.0] + starts[1:] + [total]
            lines = []
            for n, (speaker, _, _) in enumerate(SCRIPT):
                lines += [f"file '{frames[speaker].name}'", f"duration {cuts[n + 1] - cuts[n]:.4f}"]
            lines.append(f"file '{frames[SCRIPT[-1][0]].name}'")
            (tmp / "shots.txt").write_text("\n".join(lines) + "\n")
            ffmpeg(
                "-f", "concat", "-safe", "0", "-i", str(tmp / "shots.txt"), "-i", str(wav),
                "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", str(args.out),
            )  # fmt: skip

    transcript = Transcript(words=estimate_words(spans, lines), duration=total, model="script")
    transcript.save(args.out.with_suffix(".transcript.json"))
    print(f"Wrote {args.out} ({total:.1f}s, {args.mode}) and {args.out.with_suffix('.transcript.json').name}")


if __name__ == "__main__":
    main()
