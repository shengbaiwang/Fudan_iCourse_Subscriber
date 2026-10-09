"""Actions-compatible audio handles backed by resumable local media.

FFmpeg reads only durable, validated bytes. Fast-start MP4s are fed while
the download grows; other layouts wait for the complete seekable file.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import struct
import subprocess
import threading
import time

from scripts.local_media import download_media
from src.runtime.scheduler import AudioHandle


def streaming_layout(path: Path, committed: int) -> bool | None:
    """True for a fully committed moov before mdat; None needs more bytes."""
    if committed < 8:
        return None
    position, moov = 0, False
    with path.open("rb") as source:
        for _ in range(100):
            if position + 8 > committed:
                return None
            source.seek(position)
            header = source.read(8)
            if len(header) != 8:
                return None
            size, kind = struct.unpack(">I4s", header)
            if size == 1:
                if position + 16 > committed:
                    return None
                size = struct.unpack(">Q", source.read(8))[0]
            if kind == b"mdat":
                return moov
            if size < 8:
                return False
            if position + size > committed:
                return None
            moov = moov or kind == b"moov"
            position += size
    return False


def feed_committed(job, pipe):
    """Keep network, FFmpeg and ASR independent; never expose partial blocks."""
    position = 0
    try:
        with job.media.open("rb") as source:
            while True:
                with job.condition:
                    job.condition.wait_for(lambda: job.cancelled.is_set() or job.error is not None
                                            or job.done or job.committed > position)
                    if job.cancelled.is_set() or job.error is not None:
                        break
                    available = job.committed - position
                    done = job.done
                if available:
                    block = source.read(min(64 * 1024, available))
                    if not block:
                        raise ValueError("已提交媒体块无法读取")
                    pipe.write(block)
                    position += len(block)
                elif done:
                    break
    except (BrokenPipeError, OSError):
        pass  # FFmpeg exit/duration checks decide whether decoding succeeded.
    finally:
        try:
            pipe.close()
        except OSError:
            pass


@dataclass
class MediaJob:
    sub_id: str
    media: Path
    checkpoint: Path
    condition: threading.Condition = field(default_factory=threading.Condition)
    cancelled: threading.Event = field(default_factory=threading.Event)
    committed: int = 0
    total: int | None = None
    done: bool = False
    error: BaseException | None = None
    response: object = None
    handle: AudioHandle | None = None
    future: object = None
    feeder: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    mode: str = ""


class LocalAudioDownloader:
    """Drop-in schedule/get/release interface used by Actions LectureRunner.

    One network slot prioritizes the current lecture. The next download
    starts as soon as this one finishes, overlapping ASR/OCR/API work.
    Only the current lecture is decoded, bounding PCM disk consumption.
    """
    def __init__(self, directory: Path, write_private, *, reporter=None, workers=1):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self._write_private = write_private
        self._reporter = reporter
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="local-media")
        self._lock = threading.Lock()
        self._jobs = {}
        self._file_fallback = set()

    @property
    def active_count(self):
        with self._lock:
            return sum(not job.done and job.error is None for job in self._jobs.values())

    def schedule(self, client, course_id, sub_id):
        sub_id = str(sub_id)
        with self._lock:
            if sub_id in self._jobs:
                return
            job = MediaJob(sub_id, self.directory / f"{sub_id}.mp4.part",
                           self.directory / f"{sub_id}.json")
            self._jobs[sub_id] = job
            job.future = self._pool.submit(self._download, client, course_id, job)

    def _download(self, client, course_id, job):
        def progress(state):
            with job.condition:
                job.committed, job.total = state["committed_bytes"], state["total_bytes"]
                job.condition.notify_all()

        def response(value):
            with job.condition:
                job.response = value

        try:
            download_media(client, course_id, job.sub_id, self.directory, self._write_private,
                           on_progress=progress, cancelled=job.cancelled, on_response=response)
            with job.condition:
                job.done = True
                job.condition.notify_all()
        except BaseException as exc:
            with job.condition:
                job.error = exc
                job.condition.notify_all()

    def get(self, sub_id, timeout=600):
        sub_id = str(sub_id)
        with self._lock:
            job = self._jobs.get(sub_id)
        if job is None:
            return None
        deadline = time.monotonic() + timeout
        while True:
            with job.condition:
                if job.error:
                    raise job.error
                if job.cancelled.is_set():
                    raise InterruptedError("本机音频准备已停止")
                if job.handle:
                    return job.handle
                if job.total and job.committed:
                    layout = streaming_layout(job.media, job.committed)
                    if job.done or (layout is True and sub_id not in self._file_fallback):
                        return self._decode(job, stream=not job.done)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("媒体尚未具备解码条件，已提交进度保留")
                job.condition.wait(min(remaining, 1))

    def _decode(self, job, *, stream):
        path = self.directory / f"{job.sub_id}.raw"
        job.mode = "committed-pipe" if stream else "complete-file"
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i",
                   "pipe:0" if stream else str(job.media), "-vn", "-ar", "16000",
                   "-ac", "1", "-f", "f32le", str(path)]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE if stream else subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        chunks = []

        def stderr():
            for line in proc.stderr:
                chunks.append(line)
                # Preserve duration/header evidence as well as a bounded tail.
                if len(chunks) > 2048:
                    del chunks[64:1024]
            proc.stderr.close()

        job.stderr_thread = threading.Thread(target=stderr, name=f"local-stderr-{job.sub_id}", daemon=True)
        job.stderr_thread.start()
        job.handle = AudioHandle(job.sub_id, str(path), proc, chunks)
        if stream:
            job.feeder = threading.Thread(target=feed_committed, args=(job, proc.stdin),
                                          name=f"local-feed-{job.sub_id}", daemon=True)
            job.feeder.start()
        print(f"[{job.sub_id}] FFmpeg 已启动：{job.mode}；下载与本机转录可重叠", flush=True)
        return job.handle

    def verify(self, sub_id, timeout=7200):
        job = self._jobs[str(sub_id)]
        with job.condition:
            if not job.condition.wait_for(lambda: job.done or job.error is not None, timeout):
                raise TimeoutError("媒体完整性检查等待超时")
            if job.error:
                raise job.error
            if not job.total or job.committed != job.total or job.media.stat().st_size != job.total:
                raise ValueError("完整媒体大小不符，禁止保存转录")
        if job.stderr_thread:
            job.stderr_thread.join(timeout=5)
        state = json.loads(job.checkpoint.read_text())
        return dict(state, decode_mode=job.mode)

    def release(self, sub_id, *, verified=False):
        with self._lock:
            job = self._jobs.pop(str(sub_id), None)
        if job is None:
            return
        job.cancelled.set()
        with job.condition:
            job.condition.notify_all()
            response = job.response
        if response is not None:
            response.close()
        if job.handle and job.handle.process.poll() is None:
            job.handle.process.terminate()
            try:
                job.handle.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                job.handle.process.kill()
                job.handle.process.wait()
        if job.future:
            job.future.result(timeout=320)
        if job.feeder:
            job.feeder.join(timeout=5)
        if job.stderr_thread:
            job.stderr_thread.join(timeout=5)
        if job.handle:
            Path(job.handle.path).unlink(missing_ok=True)
        if verified:
            job.media.unlink(missing_ok=True)
            job.checkpoint.unlink(missing_ok=True)
        else:
            self._file_fallback.add(str(sub_id))

    def shutdown(self):
        with self._lock:
            sub_ids = list(self._jobs)
        for sub_id in sub_ids:
            self.release(sub_id)
        self._pool.shutdown(wait=True, cancel_futures=True)
