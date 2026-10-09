import hashlib
import json
from types import SimpleNamespace

import pytest
import requests
from curl_cffi import requests as curl_requests

from scripts import local_course, local_media


@pytest.mark.parametrize("header,start,end,total", [
    ("bytes 0-7/16", 8, 15, 16), ("bytes 8-15/20", 8, 15, 16),
    ("bytes 8-12/16", 8, 15, 16), ("", 8, 15, 16),
])
def test_wrong_or_unknown_range_is_rejected(header, start, end, total):
    with pytest.raises(ValueError):
        local_media.parse_range(header, start, end, total)


def test_small_final_range_is_valid():
    assert local_media.parse_range("bytes 8-10/11", 8, 15, 11) == (8, 10, 11)


def test_interrupted_range_resumes_only_verified_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(local_media, "CHUNK_BYTES", 4)
    monkeypatch.setattr(local_media, "FREE_RESERVE", 0)
    monkeypatch.setattr(local_media.time, "sleep", lambda _: None)
    source = "https://source.test/media.mp4"
    state = {"course_id": "4048", "sub_id": "101945", "committed_bytes": 4, "total_bytes": 8,
             "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "etag": "v1", "route": "direct"}
    checkpoint = tmp_path / "101945.json"
    checkpoint.write_text(json.dumps(state))
    media = tmp_path / "101945.mp4.part"
    media.write_bytes(b"abcdUNCOMMITTED")
    session = requests.Session()
    session.cookies.set("vpn_session", "test-only", domain="webvpn.fudan.edu.cn")
    client = SimpleNamespace(vpn=SimpleNamespace(session=session),
                             get_video_url=lambda *_: source + "?signature=renewed",
                             get_stream_params=lambda _: ("https://webvpn.fudan.edu.cn/media", ""))
    calls = []
    truncated = True

    class Transport:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            calls.append(kwargs["headers"]["Range"])
            assert "Cookie" not in kwargs["headers"]  # VPN cookie stays off origin
            return SimpleNamespace(status_code=206, headers={"Content-Range": "bytes 4-7/8", "ETag": "v1"},
                                   iter_content=lambda **_: iter([b"ef" if truncated else b"efgh"]), close=lambda: None)

    monkeypatch.setattr(curl_requests, "Session", Transport)
    with pytest.raises(ValueError, match="提前结束"):
        local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private)
    assert json.loads(checkpoint.read_text())["committed_bytes"] == 4
    assert media.read_bytes() == b"abcd"
    truncated = False
    result, _ = local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private)
    assert result.read_bytes() == b"abcdefgh"
    assert json.loads(checkpoint.read_text())["committed_bytes"] == 8
    assert all(value == "bytes=4-7" for value in calls)


def test_large_requests_keep_small_checkpoints_and_reuse_signatures(tmp_path, monkeypatch):
    monkeypatch.setattr(local_media, "CHUNK_BYTES", 4)
    monkeypatch.setattr(local_media, "REQUEST_BYTES", 8)
    monkeypatch.setattr(local_media, "FREE_RESERVE", 0)
    monkeypatch.setattr(local_media.time, "sleep", lambda _: None)
    data = b"abcdefghijklmnop"
    source = "https://source.test/media.mp4"
    (tmp_path / "101945.json").write_text(json.dumps({"course_id": "4048", "sub_id": "101945",
        "committed_bytes": 0, "total_bytes": 16, "source_sha256": None, "etag": None, "route": "direct"}))
    signatures, calls, committed = [], [], []
    def signed(*_):
        signatures.append(1)
        return source + "?private=signature"
    client = SimpleNamespace(vpn=SimpleNamespace(session=requests.Session()), get_video_url=signed,
                             get_stream_params=lambda _: ("https://webvpn.fudan.edu.cn/media", ""))
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            value = kwargs["headers"]["Range"]
            calls.append(value)
            start, end = map(int, value.removeprefix("bytes=").split("-"))
            body = [b"abcd", b"ef"] if len(calls) == 1 else [data[start:end+1]]
            return SimpleNamespace(status_code=206, headers={"Content-Range": f"bytes {start}-{end}/16", "ETag": "v1"},
                                   iter_content=lambda **_: iter(body), close=lambda: None)
    monkeypatch.setattr(curl_requests, "Session", Transport)
    path, checkpoint = local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private,
                                                   on_progress=lambda state: committed.append(state["committed_bytes"]))
    assert path.read_bytes() == data
    assert calls == ["bytes=0-7", "bytes=4-11", "bytes=12-15"]
    assert len(signatures) == 1
    assert 4 in committed and 8 in committed and 12 in committed
    state = json.loads(checkpoint.read_text())
    assert state["request_count"] == 3 and state["signature_count"] == 1
    assert "private=signature" not in checkpoint.read_text()


def test_changed_server_version_never_joins_cached_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(local_media, "FREE_RESERVE", 0)
    monkeypatch.setattr(local_media.time, "sleep", lambda _: None)
    source = "https://source.test/media.mp4"
    (tmp_path / "101945.json").write_text(json.dumps({"course_id": "4048", "sub_id": "101945",
        "committed_bytes": 4, "total_bytes": 8, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "etag": "v1", "route": "direct"}))
    media = tmp_path / "101945.mp4.part"
    media.write_bytes(b"abcd")
    client = SimpleNamespace(vpn=SimpleNamespace(session=requests.Session()), get_video_url=lambda *_: source,
                             get_stream_params=lambda _: ("https://webvpn.fudan.edu.cn/media", ""))
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=206, headers={"Content-Range": "bytes 4-7/8", "ETag": "v2"}, close=lambda: None)
    monkeypatch.setattr(curl_requests, "Session", Transport)
    with pytest.raises(ValueError, match="媒体版本发生变化"):
        local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private)
    assert media.read_bytes() == b"abcd"


@pytest.mark.parametrize("failure", ["expired", "oversized", "cancelled"])
def test_expired_signature_oversized_body_and_cancellation(tmp_path, monkeypatch, failure):
    import threading
    monkeypatch.setattr(local_media, "CHUNK_BYTES", 4)
    monkeypatch.setattr(local_media, "REQUEST_BYTES", 8)
    monkeypatch.setattr(local_media, "FREE_RESERVE", 0)
    monkeypatch.setattr(local_media.time, "sleep", lambda _: None)
    source, signatures = "https://source.test/media.mp4", []
    state = {"course_id": "4048", "sub_id": "101945", "committed_bytes": 0,
             "total_bytes": 8, "source_sha256": None, "etag": None, "route": "direct"}
    (tmp_path / "101945.json").write_text(json.dumps(state))
    def signed(*_):
        signatures.append(1)
        return source + f"?sig={len(signatures)}"
    client = SimpleNamespace(vpn=SimpleNamespace(session=requests.Session()), get_video_url=signed,
                             get_stream_params=lambda _: ("https://webvpn.fudan.edu.cn/media", ""))
    stop = threading.Event()
    class Transport:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            start, end = map(int, kwargs["headers"]["Range"].removeprefix("bytes=").split("-"))
            code = 403 if failure == "expired" and len(signatures) == 1 else 206
            body = [bytes([value]) for value in b"abcdefgh"[start:end+1]]
            if failure == "oversized": body += [b"EXTRA"]
            return SimpleNamespace(status_code=code, headers={"Content-Range": f"bytes {start}-{end}/8", "ETag": "v1"},
                                   iter_content=lambda **_: iter(body), close=lambda: None)
    def progress(value):
        if failure == "cancelled" and value["committed_bytes"] == 4: stop.set()
    monkeypatch.setattr(curl_requests, "Session", Transport)
    if failure == "expired":
        media, _ = local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private)
        assert len(signatures) == 2 and media.read_bytes() == b"abcdefgh"
    else:
        expected = InterruptedError if failure == "cancelled" else ValueError
        with pytest.raises(expected):
            local_media.download_media(client, "4048", "101945", tmp_path, local_course.write_private,
                                       cancelled=stop, on_progress=progress)
        assert json.loads((tmp_path / "101945.json").read_text())["committed_bytes"] == 4
        assert (tmp_path / "101945.mp4.part").read_bytes() == b"abcd"
