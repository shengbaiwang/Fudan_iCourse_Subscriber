"""Large range requests, small durable checkpoints, in-memory signatures."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit

import requests

CHUNK_BYTES = 8 * 1024 * 1024
REQUEST_BYTES = 64 * 1024 * 1024
SIGNATURE_TTL = 240
FREE_RESERVE = 1024 * 1024 * 1024


def parse_range(value: str, requested_start: int, requested_end: int, total: int | None):
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", value or "")
    if not match:
        raise ValueError("媒体服务器未提供可验证的字节范围")
    start, end, size = map(int, match.groups())
    if (size <= 0 or start != requested_start or end != min(requested_end, size - 1)
            or end < start or (total is not None and size != total)):
        raise ValueError("媒体字节范围或总大小不符")
    return start, end, size


def download_media(client, course_id: str, sub_id: str, directory: Path, write_private,
                   *, on_progress=None, cancelled=None, on_response=None):
    from curl_cffi.requests import Session
    from src.runtime import config

    if not all(re.fullmatch(r"[0-9]{1,12}", value) for value in (course_id, sub_id)):
        raise ValueError("媒体课程和课次 ID 必须为数字")

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    media = directory / f"{sub_id}.mp4.part"
    checkpoint = directory / f"{sub_id}.json"
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        "course_id": course_id, "sub_id": sub_id, "committed_bytes": 0,
        "total_bytes": None, "source_sha256": None, "etag": None, "route": None,
    }
    if state.get("course_id") != course_id or state.get("sub_id") != sub_id:
        raise ValueError("媒体检查点课程或课次不符")
    offset = int(state["committed_bytes"])
    if offset < 0 or (offset and (not media.exists() or media.stat().st_size < offset)):
        raise ValueError("媒体检查点与本机文件不符，需检查缓存")
    mode = "r+b" if media.exists() else "w+b"
    signed_url, signed_at = None, 0.0
    state.setdefault("request_count", 0)
    state.setdefault("signature_count", 0)

    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("本机取流已停止，已提交检查块保留")

    def notify():
        if on_progress:
            on_progress(dict(state))

    with open(media, mode) as stream, Session(impersonate="chrome", discard_cookies=True) as transport:
        os.chmod(media, 0o600)
        stream.truncate(offset)  # discard an uncommitted interrupted tail
        stream.seek(offset)
        notify()
        while state["total_bytes"] is None or offset < state["total_bytes"]:
            failure = None
            for attempt in range(3):
                response = None
                try:
                    check_cancelled()
                    requested_start = offset
                    requested_end = offset + REQUEST_BYTES - 1
                    if state["total_bytes"] is not None:
                        requested_end = min(requested_end, state["total_bytes"] - 1)
                    if not signed_url or time.monotonic() - signed_at >= SIGNATURE_TTL:
                        signed_url = client.get_video_url(course_id, sub_id)
                        if not signed_url:
                            raise ValueError("无法获取媒体签名")
                        signed_at = time.monotonic()
                        state["signature_count"] += 1
                    source = urlsplit(signed_url)
                    identity = hashlib.sha256(f"{source.scheme}://{source.netloc}{source.path}".encode()).hexdigest()
                    if state["source_sha256"] and state["source_sha256"] != identity:
                        raise ValueError("媒体源文件发生变化，禁止拼接旧缓存")
                    vpn_url, _ = client.get_stream_params(signed_url)
                    if not state.get("route"):
                        direct = None
                        try:
                            direct = transport.get(signed_url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
                                                   stream=True, timeout=(5, 10))
                            if direct.status_code == 206:
                                parse_range(direct.headers.get("Content-Range"), 0, 0, None)
                                received = 0
                                for chunk in direct.iter_content(chunk_size=1):
                                    received += len(chunk)
                                    if received > 1:
                                        break
                                if received == 1:
                                    state["route"] = "direct"
                        except Exception:
                            pass
                        finally:
                            if direct:
                                direct.close()
                        state["route"] = state.get("route") or "webvpn"
                        print(f"[{sub_id}] 媒体读取路径：{state['route']}", flush=True)
                    request_url = signed_url if state["route"] == "direct" else vpn_url
                    prepared = client.vpn.session.prepare_request(requests.Request("GET", request_url))
                    # CookieJar scopes cookies to the WebVPN URL. No credentials
                    # or signed URLs are written to the media checkpoint.
                    headers = {"User-Agent": config.USER_AGENT, "Accept": "*/*",
                               "Accept-Encoding": "identity", "Range": f"bytes={offset}-{requested_end}"}
                    if prepared.headers.get("Cookie"):
                        headers["Cookie"] = prepared.headers["Cookie"]
                    state["request_count"] += 1
                    response = transport.get(request_url, headers=headers, stream=True, timeout=(15, 300))
                    if on_response:
                        on_response(response)
                    if response.status_code != 206:
                        signed_url = None  # refresh on authorization/redirect failures
                        raise ValueError(f"媒体分块请求未返回 206（状态 {response.status_code}）")
                    _, end, total = parse_range(response.headers.get("Content-Range"), offset, requested_end, state["total_bytes"])
                    etag = response.headers.get("ETag") or response.headers.get("Last-Modified")
                    if state["etag"] and etag and state["etag"] != etag:
                        raise ValueError("媒体版本发生变化，禁止拼接旧缓存")
                    if shutil.disk_usage(directory).free < total - offset + FREE_RESERVE:
                        raise ValueError(f"磁盘空间不足：该课次剩余 {(total-offset)/1024**3:.2f} GiB，另需保留 1 GiB")
                    wanted, received = end - offset + 1, 0
                    pending = 0
                    state.update(total_bytes=total, source_sha256=identity, etag=etag or state["etag"])
                    notify()

                    def commit():
                        nonlocal offset, state, pending
                        stream.flush()
                        os.fsync(stream.fileno())
                        next_offset = offset + pending
                        next_state = dict(state, committed_bytes=next_offset)
                        write_private(checkpoint, json.dumps(next_state).encode())
                        offset, state, pending = next_offset, next_state, 0
                        notify()
                        print(f"[{sub_id}] 录播分块 {offset/1024**2:.1f}/{total/1024**2:.1f} MiB ({offset/total:.1%})", flush=True)

                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        check_cancelled()
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > wanted:
                            raise ValueError("媒体分块响应超出请求长度")
                        cursor = 0
                        while cursor < len(chunk):
                            length = min(CHUNK_BYTES - pending, len(chunk) - cursor)
                            stream.write(chunk[cursor:cursor + length])
                            cursor += length
                            pending += length
                            # Hold the response's final block until EOF so an
                            # oversized final response cannot look complete.
                            if pending == CHUNK_BYTES and offset + pending < requested_start + wanted:
                                commit()
                    if received != wanted:
                        raise ValueError(f"媒体分块提前结束：{received}/{wanted} 字节")
                    if pending:
                        commit()
                    if offset != requested_start + wanted:
                        raise ValueError("媒体提交字节数与响应不符")
                    failure = None
                    break
                except Exception as exc:
                    stream.truncate(offset)
                    stream.seek(offset)
                    failure = exc
                    if isinstance(exc, InterruptedError):
                        raise
                    if attempt < 2:
                        time.sleep(2 * (attempt + 1))
                finally:
                    if response:
                        response.close()
                    if on_response:
                        on_response(None)
            if failure:
                raise failure
        if media.stat().st_size != state["total_bytes"]:
            raise ValueError("完整媒体文件大小不符")
    return media, checkpoint
