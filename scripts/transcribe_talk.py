#!/usr/bin/env python3
"""Resumable recording → ASR checkpoint → notes, with encrypted results."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

API_ROOT = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"GitHub API {status}: {message}")


def _env(name, default=None):
    value = os.environ.get(name, default or "").strip()
    if not value and default is None:
        raise ValueError(f"环境变量 {name} 不能为空")
    return value


def _api(path, *, method="GET", payload=None, raw=False):
    request = urllib.request.Request(
        API_ROOT + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "Authorization": f"Bearer {_env('GITHUB_TOKEN')}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "iCourse-talk-transcribe",
        }, method=method,
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error = GitHubError(exc.code, exc.read().decode(errors="replace")[:300])
            if exc.code not in {429, 500, 502, 503, 504}:
                raise error from exc
        except (urllib.error.URLError, OSError) as exc:
            error = GitHubError(0, str(exc))
        if attempt == 2:
            raise error
        time.sleep(2 ** attempt)


def _contents(branch, path):
    repo = _env("GITHUB_REPOSITORY")
    safe = urllib.parse.quote(path, safe="/")
    return f"/repos/{repo}/contents/{safe}?ref={urllib.parse.quote(branch, safe='')}"


def read_branch_file(branch, path):
    # JSON content is empty for files above 1 MB; chunks are 4 MB.
    try:
        return _api(_contents(branch, path), raw=True)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise


def write_branch_file(branch, path, data, message):
    for attempt in range(4):
        try:
            current = json.loads(_api(_contents(branch, path)))
        except GitHubError as exc:
            if exc.status != 404:
                raise
            current = {}
        payload = {"message": message, "branch": branch,
                   "content": base64.b64encode(data).decode()}
        if current.get("sha"):
            payload["sha"] = current["sha"]
        try:
            _api(_contents(branch, path).split("?", 1)[0], method="PUT", payload=payload)
            return
        except GitHubError as exc:
            if exc.status not in {409, 422} or attempt == 3:
                raise
            time.sleep(2 ** attempt)


def validate_manifest(manifest, talk_id):
    if not isinstance(manifest, dict) or manifest.get("talk_id") != talk_id:
        raise ValueError("录音清单与讲座 ID 不匹配")
    names = manifest.get("chunks")
    if (manifest.get("version") != 1 or not isinstance(names, list) or not names
            or len(names) > 128 or names != [f"{i:04d}.part.enc" for i in range(1, len(names) + 1)]):
        raise ValueError("录音分片清单无效")
    if manifest.get("ext") not in {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "mp4", "mov"}:
        raise ValueError("录音格式无效")
    size = manifest.get("bytes")
    if not isinstance(size, int) or not 0 < size <= 501 * 1024 * 1024:
        raise ValueError("录音大小无效")


def _error_text(exc):
    text = f"{type(exc).__name__}: {exc}"
    for name, value in os.environ.items():
        if value and (name.endswith(("KEY", "TOKEN", "PASSWORD")) or name == "UISPSW"):
            text = text.replace(value, "***")
    return text[:1000]


def run():
    from src.data.crypto_box import derive_new_password, decrypt, encrypt
    talk_id = _env("TALK_ID")
    request_id = _env("TALK_REQUEST_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", talk_id):
        raise ValueError("讲座 ID 无效")
    if request_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id):
        raise ValueError("请求 ID 无效")
    branch = _env("TALK_AUDIO_BRANCH", "talk-audio")
    password = derive_new_password(_env("STUID"), _env("UISPSW"))
    directory = f"talk_audio/{talk_id}"
    output = f"talk_transcripts/{talk_id}.json.enc"
    result = {"talk_id": talk_id, "request_id": request_id, "transcript": "",
              "summary": "", "ai_title": "", "summary_model": ""}
    stage = "transcribe"

    def save(status, error=""):
        result.update(status=status, error=error, error_stage=stage if error else "")
        write_branch_file(branch, output,
                          encrypt(json.dumps(result, ensure_ascii=False).encode(), password),
                          message=f"talk {talk_id} {status}")

    try:
        manifest_bytes = read_branch_file(branch, directory + "/manifest.json")
        if manifest_bytes is None:
            raise ValueError("录音清单不存在，请恢复上传")
        manifest = json.loads(manifest_bytes)
        validate_manifest(manifest, talk_id)
        fingerprint = hashlib.sha256(manifest_bytes).hexdigest()
        result["manifest_sha256"] = fingerprint
        previous = read_branch_file(branch, output)
        if previous:
            cached = json.loads(decrypt(previous, password))
            if cached.get("manifest_sha256") == fingerprint and cached.get("transcript", "").strip():
                for key in ("transcript", "summary", "ai_title", "summary_model", "segments"):
                    result[key] = cached.get(key, "")
                if result["summary"].strip() and cached.get("status") == "ready":
                    save("ready")
                    return 0
        if not result["transcript"]:
            # Import models only if a valid ASR checkpoint is unavailable.
            from src.ai.transcriber import Transcriber
            with tempfile.TemporaryDirectory(prefix="talk-transcribe-") as tmp:
                encrypted = Path(tmp) / "audio.enc"
                digest = hashlib.sha256()
                size = 0
                with encrypted.open("wb") as stream:
                    for name in manifest["chunks"]:
                        chunk = read_branch_file(branch, f"{directory}/{name}")
                        if not chunk or len(chunk) > 4 * 1024 * 1024:
                            raise ValueError(f"音频分片缺失或大小异常：{name}")
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != manifest["bytes"]:
                    raise ValueError("音频分片总大小不符，请恢复上传")
                if manifest.get("sha256") and digest.hexdigest() != manifest["sha256"]:
                    raise ValueError("音频分片校验失败，请恢复上传")
                audio = Path(tmp) / f"audio.{manifest['ext']}"
                audio.write_bytes(decrypt(encrypted.read_bytes(), password))
                transcriber = Transcriber()
                transcript, segments = transcriber.transcribe_video(str(audio))
                if not transcript.strip():
                    raise ValueError("录音中未识别到语音，请检查录音是否有声音")
                result.update(transcript=transcript, segments=len(segments))
        stage = "summarize"
        save("summarizing")  # Durable checkpoint BEFORE any model request.
        from src.ai.summarizer import Summarizer
        from src.ai.title import split_generated_title
        summarizer = Summarizer()
        transcript = result["transcript"]
        # Bound each prompt for long recordings without dropping the tail.
        # Keep individual notes in the checkpoint so retries reuse completed parts.
        chunks = [transcript[i:i + 24000] for i in range(0, len(transcript), 24000)]
        notes = []
        if previous and cached.get("manifest_sha256") == fingerprint:
            notes = cached.get("note_parts", [])
            if not isinstance(notes, list) or len(notes) > len(chunks):
                notes = []
        result["note_parts"] = notes
        for index in range(len(notes), len(chunks)):
            raw, model = summarizer.summarize(
                str(manifest.get("title") or "未命名讲座"),
                "【音频转录（无时间轴）】\n" + chunks[index])
            title, body = split_generated_title(raw)
            if not body.strip() or (raw.strip().startswith("# ") and not raw.strip().partition("\n")[2].strip()):
                raise ValueError("模型没有返回笔记正文")
            notes.append({"title": title, "body": body, "model": model})
            save("summarizing")
        result["ai_title"] = notes[0]["title"]
        result["summary"] = "\n\n".join(
            (f"### 第 {i + 1} 部分 · {note['title']}\n\n" if len(notes) > 1 else "") + note["body"]
            for i, note in enumerate(notes))
        result["summary_model"] = ", ".join(dict.fromkeys(note["model"] for note in notes))
        save("ready")
        print(f"[Talk {talk_id}] complete: {len(transcript)} transcript characters")
        return 0
    except Exception as exc:
        error = _error_text(exc)
        save("failed", error)
        print(f"::error::{stage}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
