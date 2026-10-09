import json
from pathlib import Path
import struct
import subprocess
import threading
import time
from types import SimpleNamespace

import imageio_ffmpeg
import pytest

from scripts import local_audio, local_course, local_pipeline


def box(kind, body=b""):
    return struct.pack(">I4s", len(body) + 8, kind) + body


def test_mp4_streaming_requires_complete_front_metadata(tmp_path):
    path = tmp_path / "media.mp4"
    data = box(b"ftyp", b"isom") + box(b"moov", b"metadata") + box(b"mdat", b"audio")
    path.write_bytes(data)
    assert local_audio.streaming_layout(path, 15) is None
    assert local_audio.streaming_layout(path, len(data)) is True
    path.write_bytes(box(b"ftyp") + box(b"mdat", b"audio") + box(b"moov"))
    assert local_audio.streaming_layout(path, path.stat().st_size) is False


def test_ffmpeg_produces_audio_before_download_finishes(tmp_path, monkeypatch):
    binary = imageio_ffmpeg.get_ffmpeg_exe()
    monkeypatch.setenv("PATH", str(Path(binary).parent) + ":" + __import__("os").environ["PATH"])
    (tmp_path / "ffmpeg").symlink_to(binary)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + __import__("os").environ["PATH"])
    fixture = tmp_path / "source.m4a"
    subprocess.run([binary, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", "30", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(fixture)], check=True)
    data = fixture.read_bytes()
    finish = threading.Event()

    def download(client, course_id, sub_id, directory, writer, *, on_progress, cancelled, on_response):
        media, checkpoint = directory / f"{sub_id}.mp4.part", directory / f"{sub_id}.json"
        first = len(data) // 2
        media.write_bytes(data[:first])
        state = {"total_bytes": len(data), "committed_bytes": first, "source_sha256": "fixture", "request_count": 1, "signature_count": 1}
        writer(checkpoint, json.dumps(state).encode())
        on_progress(state)
        assert finish.wait(15)
        with media.open("ab") as stream:
            stream.write(data[first:])
        state["committed_bytes"] = len(data)
        writer(checkpoint, json.dumps(state).encode())
        on_progress(state)
        return media, checkpoint

    monkeypatch.setattr(local_audio, "download_media", download)
    downloader = local_audio.LocalAudioDownloader(tmp_path / "cache", local_course.write_private)
    try:
        downloader.schedule(None, "4048", "101945")
        handle = downloader.get("101945", timeout=5)
        deadline = time.monotonic() + 8
        path = Path(handle.path)
        while time.monotonic() < deadline and (not path.exists() or path.stat().st_size < 64000):
            time.sleep(0.05)
        assert path.exists() and path.stat().st_size >= 64000
        assert not finish.is_set() and not downloader._jobs["101945"].done
        finish.set()
        handle.process.wait(timeout=10)
        state = downloader.verify("101945")
        assert state["decode_mode"] == "committed-pipe"
        # AAC may pad the last frame, but all 30 seconds must be decoded.
        assert 30 * 64000 <= path.stat().st_size <= 30.1 * 64000
        downloader.release("101945", verified=True)
        assert not path.exists() and not (tmp_path / "cache/101945.mp4.part").exists()
    finally:
        finish.set()
        downloader.shutdown()


def test_prefetch_failure_cannot_become_successful_zero_page_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    with local_course.local_database(creds) as (db, checkpoint):
        def fail(*_): raise TimeoutError("PPT list timeout")
        client = local_pipeline.StrictPPTClient(SimpleNamespace(get_ppt_list=fail), db, local_course)
        with pytest.raises(RuntimeError, match="PPT list timeout"):
            client.get_ppt_list("4048", "101945")
        with pytest.raises(RuntimeError, match="PPT list timeout"):
            client.source_count("101945")


@pytest.mark.parametrize("expected,actual,accepted", [(100, 100, True), (100, 80, False), (0, 100, False)])
def test_shared_transcriber_only_commits_verified_complete_audio(tmp_path, monkeypatch, expected, actual, accepted):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("101945", "4048", "测试", "2024-04-10")
        downloader = SimpleNamespace(schedule=lambda *_: None, get=lambda *_, **__: SimpleNamespace(path="audio", process=None, stderr_chunks=[]),
            verify=lambda *_: {"total_bytes": 1000, "source_sha256": "verified", "decode_mode": "committed-pipe"})
        transcriber = SimpleNamespace(transcribe_tail=lambda *_: ("完整课堂文字", []), _media_duration=expected, _last_duration=actual, _num_threads=4)
        pipeline = local_pipeline.LocalLectureRunner(None, db, SimpleNamespace(audio_downloader=downloader), transcriber, None,
            checkpoint=checkpoint, runner=local_course, summary_worker=None)
        if accepted:
            pipeline._get_transcript(db.get_lecture("101945"), "4048", "101945")
            assert local_course.meta(db, "101945", "audio")["decoded_seconds"] == 100
        else:
            with pytest.raises(ValueError):
                pipeline._get_transcript(db.get_lecture("101945"), "4048", "101945")
            assert not local_course.meta(db, "101945", "audio")
            assert not db.get_lecture("101945")["transcript"]


def test_connector_result_imports_and_exports_while_prepare_owns_database(tmp_path, monkeypatch):
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    lecture = {"sub_id": "101945", "sub_title": "测试", "date": "2024-04-10", "has_playback": True}
    manifest = {"title": local_course.TITLE, "teacher": local_course.TEACHER, "lectures": [lecture]}
    with local_course.running_job(), local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("101945", "4048", "测试", "2024-04-10")
        db.update_transcript("101945", "经过完整性检查的课程文字")
        local_course.save_meta(db, "101945", "audio", {"expected_seconds": 100, "decoded_seconds": 100})
        local_course.save_meta(db, "101945", "ocr", {"source_pages": 0})
        request = local_course.summary_request(db, lecture)
        worker = local_pipeline.SummaryWorker(db, checkpoint, manifest, local_course)
        try:
            result = {"sub_id": "101945", "prompt_sha256": request["prompt_sha256"], "finish_reason": "stop",
                      "text": "# 第一讲标题\n\n第一讲完整笔记", "model": "test-model"}
            local_course.write_private(worker.inbox / "101945.json", json.dumps(result, ensure_ascii=False).encode())
            worker.consume()
            assert db.get_lecture("101945")["summary"]
            assert (tmp_path / "notes/101945.md").exists()
            assert json.loads((tmp_path / "validation.json").read_text())[0]["status"] == "已完成"
            with local_course.read_snapshot(creds) as snapshot:
                assert snapshot.get_lecture("101945")["summary"]
        finally:
            worker.shutdown()


def test_shared_actions_phases_prefetch_next_before_asr_and_keep_notes_pending(tmp_path, monkeypatch):
    import src.pipeline.lecture_runner as shared
    monkeypatch.setattr(local_course, "RUN_DIR", tmp_path)
    events = []
    class PPT:
        def __init__(self, *args): pass
        def submit(self, *args, **kwargs):
            events.append("ppt")
            return SimpleNamespace(drain=lambda: events.append("ocr-drain"))
        def prefetch_and_ocr(self, *args): events.append("next-ocr")
    monkeypatch.setattr(shared, "PPTPipeline", PPT)
    creds = SimpleNamespace(stuid="test", uispsw="test-only")
    lecture = {"sub_id": "101945", "sub_title": "第一讲", "date": "2024-04-10"}
    with local_course.local_database(creds) as (db, checkpoint):
        db.upsert_course("4048", local_course.TITLE, local_course.TEACHER)
        db.insert_lecture("101945", "4048", "第一讲", "2024-04-10")
        def transcribe(*_):
            events.append("asr")
            return "经过验收的完整课程文字", []
        downloader = SimpleNamespace(schedule=lambda *_: events.append("current-audio"),
            get=lambda *_, **__: SimpleNamespace(path="audio", process=None, stderr_chunks=[]),
            verify=lambda *_: {"total_bytes": 1000, "source_sha256": "verified", "decode_mode": "committed-pipe"},
            release=lambda *_, **kwargs: events.append("verified-release" if kwargs["verified"] else "failed-release"))
        scheduler = SimpleNamespace(audio_downloader=downloader, prefetch_lecture=lambda *_, **__: events.append("next-prefetch"))
        reporter = SimpleNamespace(lecture_start=lambda *_: None)
        transcriber = SimpleNamespace(transcribe_tail=transcribe, _media_duration=100, _last_duration=100, _num_threads=4)
        summary_worker = SimpleNamespace(schedule=lambda *_: events.append("async-summary"))
        pipeline = local_pipeline.LocalLectureRunner(SimpleNamespace(source_count=lambda *_: 0), db, scheduler, transcriber, reporter,
            checkpoint=checkpoint, runner=local_course, summary_worker=summary_worker)
        pipeline.run("4048", local_course.TITLE, lecture, next_info=("4048", "101947"))
        assert events.index("next-prefetch") < events.index("asr") < events.index("ocr-drain") < events.index("async-summary")
        assert events[-1] == "verified-release" and "failed-release" not in events
        assert local_course.meta(db, "101945", "audio") and local_course.meta(db, "101945", "ocr")
        assert not db.get_lecture("101945")["processed_at"]
        assert (tmp_path / "summary-requests/101945.json").exists()
