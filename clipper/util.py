"""Small shared helpers: subprocess, ffprobe, JSON, formatting."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

log = logging.getLogger("clipper")

REPO = Path(__file__).resolve().parent.parent


class ClipperError(Exception):
    """An expected failure, with a message a person can act on."""


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ClipperError(f"`{tool}` is not installed or not on PATH.")
    return path


def run(cmd, *, cwd=None, check=True, timeout=None, env=None, input=None):
    """Run a command without a shell. Raises ClipperError with the stderr tail."""
    cmd = [str(c) for c in cmd]
    log.debug("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=input,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise ClipperError(f"`{cmd[0]}` is not installed or not on PATH.") from None
    except subprocess.TimeoutExpired:
        raise ClipperError(f"`{cmd[0]}` timed out after {timeout}s.") from None
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-12:])
        raise ClipperError(f"`{cmd[0]}` failed (exit {proc.returncode}):\n{tail}")
    return proc


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", (text or "").replace("’", "'")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.replace("'", "")).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text or "clip"


def clock(seconds: float) -> str:
    """12.3 -> '0:12', 3723 -> '1:02:03'."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def probe(path) -> dict:
    """Describe a media file: duration, audio, and the displayed video size."""
    need("ffprobe")
    proc = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    )
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ClipperError(f"Could not read media info from {path}.") from None

    info = {
        "format": raw.get("format", {}).get("format_name", ""),
        "duration": 0.0,
        "has_audio": False,
        "has_video": False,
        "has_cover": False,
        "width": 0,
        "height": 0,
        "fps": 0.0,
    }
    try:
        info["duration"] = float(raw.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        pass

    for stream in raw.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "audio":
            info["has_audio"] = True
        elif kind == "video":
            # Podcast audio files often carry cover art as a one-frame "video".
            if stream.get("disposition", {}).get("attached_pic"):
                info["has_cover"] = True
                continue
            if info["has_video"]:
                continue
            width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
            if width < 16 or height < 16:
                continue
            width = int(round(width * _pixel_shape(stream) / 2)) * 2  # non-square pixels
            if abs(_rotation(stream)) % 180 == 90:
                width, height = height, width
            info.update(has_video=True, width=width, height=height, fps=_rate(stream))
    return info


def _pixel_shape(stream: dict) -> float:
    value = stream.get("sample_aspect_ratio") or "1:1"
    try:
        num, den = (float(part) for part in value.split(":", 1))
        return num / den if num > 0 and den > 0 else 1.0
    except ValueError:
        return 1.0


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return int(round(float(side["rotation"])))
            except (TypeError, ValueError):
                return 0
    try:
        return int(stream.get("tags", {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0


def _rate(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key) or ""
        if "/" in value:
            num, den = value.split("/", 1)
            try:
                if float(den):
                    return float(num) / float(den)
            except ValueError:
                continue
    return 0.0
