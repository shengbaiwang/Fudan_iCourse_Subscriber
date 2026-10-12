"""Recording metadata independent of slides and speech.

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


def recording_duration(payload, source="metadata", *, media=False, duration_unit=None):
    """Return {seconds, source} from recording fields, or None if uncertain.

    Unitless numeric ``duration`` is ignored unless the caller supplies an
    endpoint's known unit. This hint applies only to the root object; nested
    room metadata and timetable lengths must not inherit it.
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
    if duration_unit in ("s", "ms"):
        seconds = _seconds(payload.get("duration"), duration_unit)
        if seconds:
            values.append({"seconds": seconds, "source": f"{source}.duration"})
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
