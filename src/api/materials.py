"""Recording metadata and transcript checks independent of slides and speech.

Only explicit media duration fields with known units are trusted. Timetable
lengths, screenshot offsets and the transcript's own last timestamp are not
evidence of the recording's full length.
"""
from __future__ import annotations

import json
import math
import re


def _seconds(value, unit: str):
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value) / (1000 if unit == "ms" else 1)
    except (ValueError, TypeError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def recording_duration(payload, source="metadata", *, media=False):
    """Return {seconds, source} from recording fields, or None if uncertain.

    Unitless numeric ``duration`` is deliberately ignored. A media object's
    duration can instead declare duration_unit=s/ms, or use HH:MM:SS.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if isinstance(payload, list):
        # Multiple camera streams may have different lengths; retain the
        # longest so a shorter stream cannot hide a missing transcript tail.
        values = [recording_duration(item, f"{source}[{i}]", media=media)
                  for i, item in enumerate(payload)]
        return max((v for v in values if v), key=lambda v: v["seconds"], default=None)
    if not isinstance(payload, dict):
        return None
    values = []
    fields = {"video_duration_seconds": "s", "video_duration_ms": "ms",
              "recording_duration_seconds": "s", "recording_duration_ms": "ms"}
    if media:
        fields.update({"duration_seconds": "s", "duration_sec": "s",
                       "duration_ms": "ms"})
    for key, unit in fields.items():
        seconds = _seconds(payload.get(key), unit)
        if seconds:
            values.append({"seconds": seconds, "source": f"{source}.{key}"})
    if media:
        duration = payload.get("duration")
        unit = payload.get("duration_unit")
        seconds = _seconds(duration, unit) if unit in ("s", "ms") else None
        if isinstance(duration, str) and re.fullmatch(r"\d+:[0-5]\d:[0-5]\d(?:\.\d+)?", duration):
            h, m, s = map(float, duration.split(":"))
            seconds = h * 3600 + m * 60 + s
        if seconds and seconds > 0:
            values.append({"seconds": seconds, "source": f"{source}.duration"})
    for key in ("content", "video_list", "playurl", "playback", "recording", "media"):
        value = recording_duration(payload.get(key), f"{source}.{key}",
                                   media=key != "content")
        if value:
            values.append(value)
    # video_list can also be a map keyed by stream ID.
    if media:
        for key, item in payload.items():
            if str(key).isdigit() and isinstance(item, (dict, list)):
                value = recording_duration(item, f"{source}.{key}", media=True)
                if value:
                    values.append(value)
    return max(values, key=lambda v: v["seconds"], default=None)


def transcript_problem(segments, duration_seconds):
    """None means timing coverage passed; otherwise a safe, readable reason."""
    duration = _seconds(duration_seconds, "s")
    if not duration:
        return "缺少带明确单位的独立录制时长，官方转录待核验"
    if not isinstance(segments, list) or not segments:
        return "官方转录为空或不可读取"
    if any(not isinstance(s, dict) or not isinstance(s.get("text"), str) for s in segments):
        return "官方转录格式异常"
    if len(" ".join(s["text"] for s in segments).strip()) < 200:
        return "官方转录文字不足"
    end = 0
    edge = min(300_000, max(3000, duration * 1000 * .05))
    for i, segment in enumerate(segments):
        start, stop = segment.get("start_ms"), segment.get("end_ms")
        if (isinstance(start, bool) or isinstance(stop, bool)
                or not isinstance(start, (int, float)) or not isinstance(stop, (int, float))
                or not math.isfinite(start) or not math.isfinite(stop)
                or start < 0 or stop <= start):
            return "官方转录时间戳异常"
        if i and start < segments[i - 1]["start_ms"]:
            return "官方转录时间戳顺序异常"
        if not i and start > edge:
            return "官方转录缺少课程开头"
        if i and start - end > 20 * 60_000:
            return "官方转录中间存在超过 20 分钟的空段"
        end = max(end, stop)
    if end > duration * 1000 + max(3000, duration * 1000 * .005):
        return "官方转录时间戳超出独立录制时长"
    if duration * 1000 - end > edge:
        return "官方转录缺少课程结尾"
    return None
