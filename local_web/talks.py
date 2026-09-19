"""Local-only lecture talks: upload audio, transcribe, summarize to notes.

Design notes:
- Talks live in a **local-only** SQLite library
  (``<config>/library/local_talks.db``).  They never touch the ``data``
  branch, shards, email, subscriptions or Obsidian sync — the read-only
  course library stays a pure mirror of the remote run.
- Transcription reuses the **same ASR chain as iCourse lectures**:
  the browser uploads the recording, the console encrypts it (same
  AES-256-CBC + PBKDF2 envelope as the course shards) and pushes it to a
  ``talk-audio`` branch, then dispatches ``talk_transcribe.yml``.  That
  workflow installs the identical ffmpeg + sherpa-onnx stack as
  ``check.yml`` and runs ``scripts/transcribe_talk.py``, which calls
  ``Transcriber.transcribe_video`` directly — same VAD segmentation, same
  SenseVoice backend, same ``sanitize_asr_segment`` cleanup.  The
  transcript comes back as an encrypted ``.enc`` file; the console pulls
  it and stores the plaintext only in the local DB.  No heavy ASR/OCR
  dependencies are imported here so ``requirements-web.txt`` stays light.
- Uploads are **background, chunked and resumable** (see TalkUploadQueue):
  the POST handler only validates + encrypts + stages ciphertext locally
  and returns in <1 s; a worker pushes ~4 MiB chunks with per-chunk retry
  (``TALK_CHUNK_ATTEMPTS`` × ``TALK_CHUNK_TIMEOUT``), records confirmed
  chunks in the DB, and dispatches the workflow only after the manifest
  lands.  This is the long-term fix for home-uplink write timeouts
  (``GitHub API 0``): no request is ever held open across the whole
  upload, a stalled chunk is retried cheaply, and the user resumes with
  one click without re-sending confirmed chunks.
- Summarization reuses the exact course prompt
  (``src.ai.summary_prompt``) and the bucketer's flat format, calling the
  already-configured OpenAI-compatible providers via stdlib
  ``urllib`` — the same endpoint shape ``Summarizer`` uses
  (``POST {base_url}/chat/completions``), **without importing ``openai``**.
  The API key always comes from the current request body (GitHub never
  reveals saved Secrets), is held in memory only, and is never persisted.

Status machine: ``uploading`` → ``transcribing`` → ``transcribed`` →
``summarizing`` → ``ready`` | ``failed``.  ``failed`` carries the stage in
``error_stage`` (``upload`` | ``transcribe`` | ``summarize``) so the UI
can offer retry.  ``draft`` is kept for pasted talks only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import ssl
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import certifi

from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content
from src.ai.title import build_title_material, clean_title, split_generated_title

from .state import default_config_dir
from src.data.crypto_box import decrypt, encrypt


TALK_AUDIO_BRANCH = "talk-audio"
TALK_TRANSCRIBE_WORKFLOW = "talk_transcribe.yml"

# One Contents-API PUT carries base64(chunk) JSON.  A 100 MB recording
# becomes ~133 MB of base64 in a single request — a guaranteed write
# timeout on a home uplink.  So console and workflow both speak chunks in
# one shard dir ``talk_audio/<id>/`` + a ``manifest.json``.
#
# Chunk sizing is deliberately conservative: 4 MiB ciphertext → ~5.6 MB
# JSON per PUT.  Each PUT writes a whole new git blob server-side, so even
# a healthy link stalls on big blobs; a stalled chunk is cheap to retry
# because uploads are resumable per chunk (see ``uploaded_chunks``).
TALK_AUDIO_CHUNK_BYTES = 4 * 1024 * 1024
TALK_AUDIO_MANIFEST_VERSION = 1
# Per-chunk network budget: attempts × timeout.  3 × 120 s tolerates a slow
# uplink without letting one wedged PUT hang the worker forever.
TALK_CHUNK_TIMEOUT = 120
TALK_CHUNK_ATTEMPTS = 3
# Dispatch retry budget: a freshly-pushed workflow file needs a few minutes
# to become dispatchable, and Actions can 500 under load.  8 attempts over
# ~4 min, then the talk parks in failed/upload with a one-click resume.
TALK_DISPATCH_ATTEMPTS = 8
TALK_DISPATCH_RETRY_SECONDS = 30
# Whole-upload budget across resume retries (server-side; the browser polls
# progress meanwhile).  Past this the talk is marked failed/upload and the
# user retries with one click — already-uploaded chunks are skipped.
TALK_UPLOAD_DEADLINE_SECONDS = 45 * 60

# Uploaded recordings: 500 MB cap (≈ 8h of 128kbps audio), audio + video
# containers only.  Video is accepted because phone recordings are usually
# mp4/m4a — the workflow extracts the audio track with ffmpeg.
MAX_AUDIO_BYTES = 500 * 1024 * 1024
ALLOWED_AUDIO_EXTENSIONS = frozenset(
    {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "mp4", "mov", "m4a"}
)
# Extension → expected magic-byte prefixes.  Checked server-side so a
# renamed executable cannot slip through.
AUDIO_MAGIC = {
    "mp3": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
    "m4a": (b"\x00\x00\x00",),  # ftyp box follows at offset 4
    "aac": (b"\xff\xf1", b"\xff\xf9"),
    "wav": (b"RIFF",),
    "flac": (b"fLaC",),
    "ogg": (b"OggS",),
    "opus": (b"OggS",),
    "mp4": (b"\x00\x00\x00",),
    "mov": (b"\x00\x00\x00",),
}
# Offset-4 box names accepted for ISO-BMFF containers (m4a/mp4/mov).
_FTYP_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide")


def validate_audio_upload(filename: str, data: bytes) -> tuple[str, str]:
    """Validate an uploaded recording. Returns (extension, storage_name).

    Raises ValueError on empty data, oversize, bad extension or magic-byte
    mismatch.  ``storage_name`` is the safe remote filename stem
    (``<talk_id>.<ext>`` is composed by the caller).
    """
    if not data:
        raise ValueError("上传的音频文件为空")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(
            f"音频文件过大（{len(data) / 1024 / 1024:.0f} MB），"
            f"上限为 {MAX_AUDIO_BYTES // 1024 // 1024} MB"
        )
    ext = str(filename or "").rsplit(".", 1)[-1].lower().strip() if "." in str(filename or "") else ""
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = "、".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
        raise ValueError(f"不支持的音频格式 .{ext or '?'}，支持：{allowed}")
    head = data[:12]
    if ext in ("m4a", "mp4", "mov"):
        if not (head[:3] == b"\x00\x00\x00" and head[4:8] in _FTYP_BOXES):
            raise ValueError(f".{ext} 文件头校验失败，文件可能已损坏或被改名")
    else:
        prefixes = AUDIO_MAGIC.get(ext, ())
        if prefixes and not head.startswith(prefixes):
            raise ValueError(f".{ext} 文件头校验失败，文件可能已损坏或被改名")
    return ext, ext


def talk_audio_remote_path(talk_id: str, ext: str) -> str:
    """Shard dir on the talk-audio branch: ``talk_audio/<id>/``.

    Audio is pushed as numbered chunks (``0001.part.enc`` …) plus a small
    ``manifest.json`` (ext + chunk list) so uploads stay per-request small.
    """
    return _talk_audio_shard_dir(talk_id)


def _talk_audio_shard_dir(talk_id: str) -> str:
    safe_id = "".join(
        ch for ch in str(talk_id) if ch.isascii() and (ch.isalnum() or ch in "-_")
    ) or "talk"
    return f"talk_audio/{safe_id}"


def talk_audio_manifest_path(talk_id: str) -> str:
    """Manifest path for a talk's chunked upload."""
    return f"{_talk_audio_shard_dir(talk_id)}/manifest.json"


def talk_audio_chunk_path(talk_id: str, index: int) -> str:
    """One chunk blob path (1-based ``index`` → zero-padded name)."""
    return f"{_talk_audio_shard_dir(talk_id)}/{index:04d}.part.enc"


def build_talk_audio_manifest(talk_id: str, ext: str, count: int,
                              total_bytes: int,
                              title: str = "") -> dict[str, Any]:
    """Manifest the workflow uses to reassemble + clean up the upload."""
    return {
        "version": TALK_AUDIO_MANIFEST_VERSION,
        "talk_id": talk_id,
        "ext": ext,
        "chunks": [f"{i:04d}.part.enc" for i in range(1, count + 1)],
        "bytes": total_bytes,
        "title": str(title or ""),
    }


def split_talk_blob(data: bytes, chunk_bytes: int = TALK_AUDIO_CHUNK_BYTES) -> list[bytes]:
    """Split an encrypted blob into ≤ ``chunk_bytes`` pieces."""
    step = max(1, int(chunk_bytes))
    return [data[i:i + step] for i in range(0, len(data), step)] or [b""]


def talk_transcript_remote_path(talk_id: str) -> str:
    """Transcript blob path the workflow writes back."""
    safe_id = "".join(
        ch for ch in str(talk_id) if ch.isascii() and (ch.isalnum() or ch in "-_")
    ) or "talk"
    return f"talk_transcripts/{safe_id}.json.enc"


def encrypt_talk_blob(data: bytes, stuid: str, uispsw: str) -> bytes:
    """Encrypt with the same v2 password as the course shards."""
    from src.data.crypto_box import derive_new_password

    return encrypt(data, derive_new_password(stuid, uispsw))


def decrypt_talk_blob(blob: bytes, stuid: str, uispsw: str) -> bytes:
    """Decrypt a talk blob. Raises TalkTranscribeError on failure."""
    from src.data.crypto_box import derive_new_password

    try:
        return decrypt(blob, derive_new_password(stuid, uispsw))
    except Exception as exc:
        raise TalkTranscribeError(f"讲座数据解密失败：{exc}") from exc


class TalkTranscribeError(RuntimeError):
    pass


TALKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS talks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'paste',
    status TEXT NOT NULL DEFAULT 'draft',
    transcript TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    summary_model TEXT NOT NULL DEFAULT '',
    ai_title TEXT NOT NULL DEFAULT '',
    audio_ext TEXT NOT NULL DEFAULT '',
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    remote_audio_path TEXT NOT NULL DEFAULT '',
    upload_total_chunks INTEGER NOT NULL DEFAULT 0,
    uploaded_chunks TEXT NOT NULL DEFAULT '',
    upload_attempts INTEGER NOT NULL DEFAULT 0,
    error_stage TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS talk_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    talk_id TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL,
    FOREIGN KEY (talk_id) REFERENCES talks(id)
);
CREATE INDEX IF NOT EXISTS idx_talk_versions_talk
    ON talk_versions(talk_id, generated_at DESC);
"""

# Pasted transcripts are capped so one paste cannot blow up the local DB
# or the downstream LLM prompt.  ~200k chars ≈ 3h of dense speech.
MAX_TRANSCRIPT_CHARS = 200_000
MAX_TITLE_CHARS = 100

_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _strip_markdown(value: str) -> str:
    text = re.sub(r"\$\$[\s\S]*?\$\$", " ", value)
    text = re.sub(r"\$[^$\n]+\$", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`~#>|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def auto_talk_title(summary: str | None, title: str | None) -> str:
    """Derive a display name: summary heading → first line → given title → ''."""
    text = str(summary or "")
    if text.strip():
        candidates: list[str] = []
        heading = _HEADING_PATTERN.search(text)
        if heading:
            candidates.append(heading.group(1))
        for line in text.splitlines():
            if line.strip():
                candidates.append(line)
                break
        for candidate in candidates:
            clean = _strip_markdown(candidate)
            if clean:
                if len(clean) > 40:
                    clean = clean[:39].rstrip() + "…"
                return clean
    return str(title or "").strip()


def validate_talk_input(title: str, transcript: str) -> tuple[str, str]:
    """Normalize + validate a pasted talk. Raises ValueError on bad input."""
    clean_title_text = str(title or "").strip()[:MAX_TITLE_CHARS]
    text = str(transcript or "").strip()
    if not text:
        raise ValueError("转写文本不能为空")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise ValueError(
            f"转写文本过长（{len(text)} 字），上限为 {MAX_TRANSCRIPT_CHARS} 字"
        )
    return clean_title_text, text


def build_talk_prompt(transcript: str) -> tuple[str, str]:
    """Assemble the LLM input in the bucketer's flat format (no PPT pages)."""
    parts = ["【音频转录（无时间轴）】", transcript.strip(), ""]
    return "\n".join(parts).strip(), "flat"


def split_summary_output(summary: str) -> tuple[str, str]:
    """Split the leading ``# <title>`` off; fall back to a tiny title call."""
    return split_generated_title(summary)


class TalkLLMError(RuntimeError):
    pass


def _redact(value: str, api_key: str) -> str:
    return value.replace(api_key, "***") if api_key else value


def chat_complete(
    base_url: str,
    model: str,
    api_key: str,
    system: str,
    user: str,
    timeout: int = 180,
) -> str:
    """One OpenAI-compatible ``POST /chat/completions`` call (stdlib only)."""
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {"model": model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Fudan-iCourse-Subscriber-local-web/0.3",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout,
            context=ssl.create_default_context(cafile=certifi.where()),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise TalkLLMError(
            f"模型 API {exc.code}: {_redact(detail, api_key)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TalkLLMError(f"无法连接模型 API: {exc.reason}") from exc
    except (ValueError, TimeoutError) as exc:
        raise TalkLLMError(f"模型 API 响应无效: {exc}") from exc
    if not isinstance(data, dict):
        raise TalkLLMError("模型 API 响应不是 JSON object")
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise TalkLLMError("模型 API 返回成功，但没有 choices")
    if choices[0].get("finish_reason") in {"length", "content_filter"}:
        raise TalkLLMError("模型输出不完整，请更换模型或缩短转写后重试")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise TalkLLMError("模型返回了空内容")
    return str(content)


TITLE_SYSTEM = (
    "你是课程助教。根据用户提供的讲座转写文本，为这场讲座生成一个简短准确的"
    "笔记标题。要求：不超过 20 字；概括本讲的核心内容；只输出标题本身，"
    "不要日期、引号或任何解释；不要给整个标题加书名号，"
    "但标题中提到的著作名必须保留其书名号。"
)


def generate_talk_title(
    base_url: str, model: str, api_key: str, transcript: str
) -> str:
    """Tiny fallback title call; returns "" on any failure (never raises)."""
    try:
        material = build_title_material(None, transcript, None)
        if not material:
            return ""
        raw = chat_complete(
            base_url, model, api_key, TITLE_SYSTEM,
            f"讲座转写（节选）如下：\n\n{material}", timeout=60,
        )
        return clean_title(raw)
    except TalkLLMError:
        return ""


class TalkStore:
    """Local-only SQLite library for uploaded/pasted talks."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir() / "library"
        self.path = self.directory / "local_talks.db"
        self._lock = threading.RLock()
        self._init_tables()

    def _init_tables(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(sqlite3.connect(self.path)) as db:
            db.executescript(TALKS_SCHEMA)
            # Migrate pre-audio libraries: add the new columns idempotently.
            existing = {
                row[1] for row in db.execute("PRAGMA table_info(talks)").fetchall()
            }
            for col, typedef in (
                ("audio_ext", "TEXT NOT NULL DEFAULT ''"),
                ("audio_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("remote_audio_path", "TEXT NOT NULL DEFAULT ''"),
                ("upload_total_chunks", "INTEGER NOT NULL DEFAULT 0"),
                ("uploaded_chunks", "TEXT NOT NULL DEFAULT ''"),
                ("upload_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("error_stage", "TEXT NOT NULL DEFAULT ''"),
                ("cloud_request_id", "TEXT NOT NULL DEFAULT ''"),
                ("cloud_requested_at", "TEXT NOT NULL DEFAULT ''"),
                ("cloud_result_hash", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in existing:
                    db.execute(f"ALTER TABLE talks ADD COLUMN {col} {typedef}")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def create_talk(self, title: str, transcript: str,
                    source: str = "paste") -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        talk_id = f"t{uuid4().hex[:12]}"
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """INSERT INTO talks
                           (id, title, source, status, transcript,
                            created_at, updated_at)
                       VALUES (?, ?, ?, 'draft', ?, ?, ?)""",
                    (talk_id, title, source, transcript, now, now),
                )
        item = self.get_talk(talk_id)
        assert item is not None
        return item

    def create_audio_talk(self, title: str, ext: str, audio_bytes: int,
                          remote_path: str, total_chunks: int = 0) -> dict[str, Any]:
        """Register an uploaded recording awaiting cloud transcription."""
        now = datetime.now(timezone.utc).isoformat()
        talk_id = f"t{uuid4().hex[:12]}"
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """INSERT INTO talks
                            (id, title, source, status, audio_ext,
                             audio_bytes, remote_audio_path,
                             upload_total_chunks,
                             created_at, updated_at)
                        VALUES (?, ?, 'upload', 'uploading',
                                ?, ?, ?, ?, ?, ?)""",
                    (talk_id, title, ext, audio_bytes, remote_path,
                     total_chunks, now, now),
                )
        item = self.get_talk(talk_id)
        assert item is not None
        return item

    def parse_uploaded_chunks(self, talk_id: str) -> set[int]:
        """1-based chunk indexes already confirmed on the branch."""
        item = self.get_talk(talk_id)
        if item is None:
            return set()
        raw = str(item.get("uploaded_chunks") or "")
        done: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                done.add(int(part))
        return done

    def note_chunk_uploaded(self, talk_id: str, index: int,
                            total_chunks: int) -> None:
        """Record one confirmed chunk; idempotent across retries.

        ``index`` 0 is the intake marker (no chunk confirmed yet) — it only
        persists the total so the progress loop has a denominator.
        """
        done = self.parse_uploaded_chunks(talk_id)
        if int(index) > 0:
            done.add(int(index))
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """UPDATE talks
                        SET uploaded_chunks = ?, upload_total_chunks = ?,
                            status = 'uploading', updated_at = ?
                        WHERE id = ?""",
                    (",".join(str(i) for i in sorted(done)),
                     total_chunks, now, talk_id),
                )

    def upload_progress(self, talk_id: str) -> dict[str, int]:
        """Small progress snapshot for the browser poll loop."""
        item = self.get_talk(talk_id)
        if item is None:
            return {"done": 0, "total": 0}
        try:
            total = int(item.get("upload_total_chunks") or 0)
        except (TypeError, ValueError):
            total = 0
        return {"done": len(self.parse_uploaded_chunks(talk_id)),
                "total": total}

    def bump_upload_attempts(self, talk_id: str) -> int:
        """Count one upload (re)try; returns the new attempt number."""
        item = self.get_talk(talk_id)
        try:
            attempts = int((item or {}).get("upload_attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        attempts += 1
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """UPDATE talks
                        SET upload_attempts = ?, status = 'uploading',
                            error = '', error_stage = '', updated_at = ?
                        WHERE id = ?""",
                    (attempts, now, talk_id),
                )
        return attempts

    def mark_transcribing(self, talk_id: str) -> None:
        """All chunks + manifest + dispatch done; hand over to the workflow."""
        self.set_status(talk_id, "transcribing")

    def note_audio_pushed(self, talk_id: str, audio_bytes: int,
                           remote_path: str) -> None:
        """Record the pushed blob size + path after a successful upload."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """UPDATE talks
                       SET audio_bytes = ?, remote_audio_path = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (audio_bytes, remote_path, now, talk_id),
                )

    def save_transcript(self, talk_id: str, transcript: str) -> None:
        """Store the workflow-produced transcript; back to draft for review.

        Uploaded talks land here after the cloud ASR run; pasted talks
        already carry their transcript at creation time.
        """
        clean = str(transcript or "").strip()
        if not clean:
            raise ValueError("转写结果为空")
        if len(clean) > MAX_TRANSCRIPT_CHARS:
            raise ValueError("转写结果超出上限")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """UPDATE talks
                       SET transcript = ?, status = 'transcribed',
                           error = '', error_stage = '', updated_at = ?
                       WHERE id = ?""",
                    (clean, now, talk_id),
                )

    def begin_cloud_request(self, talk_id: str) -> str:
        request_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db, db:
            db.execute("UPDATE talks SET cloud_request_id=?, cloud_requested_at=?, "
                       "status='dispatching', error='', error_stage='', updated_at=? WHERE id=?",
                       (request_id, now, now, talk_id))
        return request_id

    def recover_interrupted(self) -> None:
        # Local model keys are memory-only; a restart cannot resume that request.
        with self._lock, closing(self._connect()) as db, db:
            db.execute("UPDATE talks SET status='failed', error_stage='summarize', "
                       "error='服务重启中断了笔记生成，请重试；转写已保留' "
                       "WHERE status='summarizing' AND cloud_request_id=''")

    def save_cloud_result(self, talk_id: str, transcript: str,
                          summary: str = "", ai_title: str = "",
                          model: str = "", *, result_hash: str = "",
                          status: str = "", error: str = "",
                          error_stage: str = "") -> str:
        clean = str(transcript or "").strip()
        body = str(summary or "").strip()
        if not clean and status != "failed":
            raise ValueError("转写结果为空")
        target = status or ("ready" if body else "transcribed")
        if target not in {"ready", "transcribed", "summarizing", "failed"}:
            raise ValueError("云端处理状态无效")
        if target == "ready" and not body:
            raise ValueError("笔记正文为空")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM talks WHERE id=?", (talk_id,)).fetchone()
            if row is None:
                raise ValueError("讲座已删除")
            if result_hash and row["cloud_result_hash"] == result_hash:
                return row["status"]
            # Never replace a finished/manual note with an older checkpoint.
            if row["summary"] and not body:
                return row["status"]
            db.execute("UPDATE talks SET transcript=?, summary=?, ai_title=?, "
                       "summary_model=?, status=?, error=?, error_stage=?, "
                       "cloud_result_hash=?, updated_at=? WHERE id=?",
                       (clean, body, ai_title, model, target, error, error_stage,
                        result_hash, now, talk_id))
            if body and (row["summary"] != body or row["summary_model"] != model):
                db.execute("INSERT INTO talk_versions(talk_id,model,summary,generated_at) "
                           "VALUES(?,?,?,?)", (talk_id, model or "cloud", body, now))
        return target

    def list_talks(self) -> list[dict[str, Any]]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(
                """SELECT id, title AS custom_title, source, status,
                          LENGTH(transcript) AS transcript_chars,
                          LENGTH(summary) AS summary_chars,
                          summary_model, ai_title, error, error_stage,
                          audio_ext, audio_bytes,
                          created_at, updated_at
                   FROM talks ORDER BY created_at DESC, id DESC"""
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            custom = item.pop("custom_title", "")
            item["display_title"] = (
                custom or item.get("ai_title")
                or auto_talk_title("", custom)
            )
            if not item["display_title"]:
                item["display_title"] = item["id"]
            items.append(item)
        return items

    def get_talk(self, talk_id: str) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM talks WHERE id = ?", (talk_id,)
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["versions"] = [
                dict(version) for version in db.execute(
                    """SELECT model, summary, generated_at
                       FROM talk_versions WHERE talk_id = ?
                       ORDER BY generated_at DESC, id DESC""",
                    (talk_id,),
                )
            ]
        custom = item.pop("title", "") or ""
        item["custom_title"] = custom
        item["display_title"] = (
            custom or item.get("ai_title")
            or auto_talk_title(item.get("summary"), custom)
            or item["id"]
        )
        return item

    def delete_talk(self, talk_id: str) -> bool:
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    "DELETE FROM talk_versions WHERE talk_id = ?", (talk_id,)
                )
                cursor = db.execute(
                    "DELETE FROM talks WHERE id = ?", (talk_id,)
                )
                return (cursor.rowcount or 0) > 0

    def rename_talk(self, talk_id: str, name: str) -> dict[str, Any] | None:
        clean = str(name or "").strip()[:MAX_TITLE_CHARS]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                cursor = db.execute(
                    "UPDATE talks SET title = ?, updated_at = ? WHERE id = ?",
                    (clean, now, talk_id),
                )
                if not cursor.rowcount:
                    return None
        return self.get_talk(talk_id)

    def set_status(self, talk_id: str, status: str, error: str = "",
                   error_stage: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    "UPDATE talks SET status = ?, error = ?,"
                    " error_stage = ?, updated_at = ? WHERE id = ?",
                    (status, error, error_stage, now, talk_id),
                )

    def save_summary(self, talk_id: str, body: str, ai_title: str,
                     model: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    """UPDATE talks
                       SET summary = ?, ai_title = ?, summary_model = ?,
                           status = 'ready', error = '', error_stage = '',
                           updated_at = ?
                       WHERE id = ?""",
                    (body, ai_title, model, now, talk_id),
                )
                db.execute(
                    """INSERT INTO talk_versions
                           (talk_id, model, summary, generated_at)
                       VALUES (?, ?, ?, ?)""",
                    (talk_id, model or "unknown", body, now),
                )

    def update_transcript(self, talk_id: str, transcript: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as db:
            with db:
                db.execute(
                    "UPDATE talks SET transcript = ?, updated_at = ?"
                    " WHERE id = ?",
                    (transcript, now, talk_id),
                )


def summarize_talk_text(
    base_url: str,
    model: str,
    api_key: str,
    title: str,
    transcript: str,
) -> tuple[str, str, str]:
    """Run one summary + fallback title. Returns (body, ai_title, model_id)."""
    prompt_text, _mode = build_talk_prompt(transcript)
    raw = chat_complete(
        base_url, model, api_key, SYSTEM_PROMPT,
        summary_user_content(title or "未命名讲座", prompt_text),
    )
    ai_title, body = split_summary_output(raw)
    if not body.strip() or (raw.strip().startswith("# ") and not raw.strip().partition("\n")[2].strip()):
        raise TalkLLMError("模型只返回标题，没有笔记正文")
    if not ai_title:
        ai_title = generate_talk_title(base_url, model, api_key, transcript)
    return body, ai_title, f"{model}"


class TalkBusyError(RuntimeError):
    pass


def _upload_staging_dir() -> Path:
    """Directory holding encrypted blobs awaiting chunk upload.

    Under the console config dir so it survives restarts but never leaves
    this machine: ``<config>/talk_uploads/<id>.enc`` + ``<id>.json`` sidecar
    (ext/title/total).  Plaintext audio is never staged — only ciphertext.
    """
    override = os.environ.get("ICOURSE_WEB_CONFIG_DIR", "").strip()
    base = Path(override).expanduser() if override else default_config_dir()
    directory = base / "talk_uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class TalkUploadQueue:
    """One background worker pushing encrypted chunks with resume + retry.

    Why this exists: a 100 MB+ recording cannot be PUT in one request on a
    home uplink — GitHub's write times out (``GitHub API 0``) and the old
    synchronous handler turned that into a dead ``failed/upload`` row with
    no way forward.  The queue instead:

    - stages the ciphertext on local disk (never the plaintext),
    - pushes ≤4 MiB chunks with per-chunk retry (3 × 120 s) and records
      each confirmed chunk in the DB,
    - survives console restarts (staging dir + DB resume the same queue),
    - only dispatches the workflow after the manifest lands, so the runner
      never sees a half upload.
    """

    def __init__(self, store: TalkStore, make_client,
                 max_workers: int = 1):
        self._store = store
        self._make_client = make_client
        self._max_workers = max(1, max_workers)
        self._lock = threading.RLock()
        self._queued: list[str] = []
        self._active: set[str] = set()
        self._stopping = threading.Event()
        self.directory = store.directory.parent / "talk_uploads"

    # ── intake ──────────────────────────────────────────────────────
    def stage_blob(self, talk_id: str, encrypted: bytes,
                   ext: str, title: str) -> int:
        """Persist ciphertext + sidecar; returns the chunk count."""
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f"{talk_id}.enc.tmp"
        temporary.write_bytes(encrypted)
        temporary.replace(directory / f"{talk_id}.enc")
        total = max(1, len(split_talk_blob(encrypted)))
        (directory / f"{talk_id}.json").write_text(
            json.dumps({"ext": ext, "title": title, "total": total,
                        "bytes": len(encrypted)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return total

    def staged_blob(self, talk_id: str) -> bytes | None:
        path = self.directory / f"{talk_id}.enc"
        try:
            return path.read_bytes()
        except OSError:
            return None

    def drop_staging(self, talk_id: str) -> None:
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        for suffix in (".enc", ".json"):
            try:
                (directory / f"{talk_id}{suffix}").unlink()
            except OSError:
                pass

    def pending_staged_ids(self) -> list[str]:
        try:
            return sorted(
                path.name[:-4] for path in self.directory.glob("*.enc")
            )
        except OSError:
            return []

    # ── scheduling ──────────────────────────────────────────────────
    def submit(self, talk_id: str) -> bool:
        """Queue a talk for background upload.  False if already queued."""
        with self._lock:
            if talk_id in self._queued or talk_id in self._active:
                return False
            if len(self._active) + len(self._queued) >= self._max_workers:
                # Single worker today: extras wait in order rather than
                # failing — uploads are cheap to serialize, unlike LLM jobs.
                pass
            self._queued.append(talk_id)
            self._pump_locked()
            return True

    def is_active(self, talk_id: str) -> bool:
        with self._lock:
            return talk_id in self._active or talk_id in self._queued

    def _pump_locked(self) -> None:
        while not self._stopping.is_set() and self._queued and len(self._active) < self._max_workers:
            talk_id = self._queued.pop(0)
            self._active.add(talk_id)
            thread = threading.Thread(
                target=self._run, args=(talk_id,),
                name=f"talk-upload-{talk_id}", daemon=True,
            )
            thread.start()

    def resume_pending(self) -> int:
        resumed = 0
        for row in self._store.list_talks():
            if row["source"] == "upload" and row["status"] in {"uploading", "dispatching"}:
                resumed += int(self.submit(row["id"]))
        return resumed

    def close(self) -> None:
        self._stopping.set()

    def preflight(self, client) -> str:
        branch = client.default_branch()
        if not client.workflow_file_on_ref(TALK_TRANSCRIBE_WORKFLOW, branch):
            raise TalkTranscribeError(
                f"云端转写尚未部署到 {branch}，请先完成讲座工作流部署；录音已保留，可稍后重试"
            )
        return branch

    def _retry(self, operation, attempts: int, delay: float = 2):
        from .github_client import GitHubAPIError
        for attempt in range(attempts):
            if self._stopping.is_set():
                raise InterruptedError("服务正在退出，重启后继续")
            try:
                return operation()
            except GitHubAPIError as exc:
                if exc.status not in {0, 409, 429, 500, 502, 503, 504} or attempt == attempts - 1:
                    raise
                if self._stopping.wait(delay):
                    raise InterruptedError("服务正在退出，重启后继续")

    def _run(self, talk_id: str) -> None:
        import time
        stage = "upload"
        try:
            item = self._store.get_talk(talk_id)
            if item is None:
                return
            client = self._make_client()
            stage = "dispatch"
            branch = self.preflight(client)
            stage = "upload"
            client.ensure_branch(TALK_AUDIO_BRANCH, default_branch=branch)
            blob = self.staged_blob(talk_id)
            if blob is not None:
                total = max(1, (len(blob) + TALK_AUDIO_CHUNK_BYTES - 1) // TALK_AUDIO_CHUNK_BYTES)
                done = self._store.parse_uploaded_chunks(talk_id)
                deadline = time.monotonic() + TALK_UPLOAD_DEADLINE_SECONDS
                for index in range(1, total + 1):
                    if self._store.get_talk(talk_id) is None:
                        return
                    if index in done:
                        continue
                    if time.monotonic() > deadline:
                        raise TimeoutError("上传超时，已完成分片已保留，可继续上传")
                    piece = blob[(index - 1) * TALK_AUDIO_CHUNK_BYTES:index * TALK_AUDIO_CHUNK_BYTES]
                    client.set_timeout(TALK_CHUNK_TIMEOUT)
                    try:
                        self._retry(lambda: client.write_branch_file(
                            TALK_AUDIO_BRANCH, talk_audio_chunk_path(talk_id, index), piece,
                            message=f"talk audio {talk_id} part {index}/{total}"), TALK_CHUNK_ATTEMPTS)
                    finally:
                        client.reset_timeout()
                    self._store.note_chunk_uploaded(talk_id, index, total)
                manifest = build_talk_audio_manifest(
                    talk_id, item["audio_ext"], total, len(blob), title=item["custom_title"])
                manifest["sha256"] = hashlib.sha256(blob).hexdigest()
                # Keep an existing matching manifest byte-for-byte: rename and
                # retries must not invalidate an already completed ASR checkpoint.
                previous = client.read_branch_file(TALK_AUDIO_BRANCH, talk_audio_manifest_path(talk_id))
                if previous:
                    old = json.loads(previous)
                    if old.get("sha256") == manifest["sha256"]:
                        manifest = old
                self._retry(lambda: client.write_branch_file(
                    TALK_AUDIO_BRANCH, talk_audio_manifest_path(talk_id),
                    json.dumps(manifest, ensure_ascii=False).encode(),
                    message=f"talk audio {talk_id} manifest"), TALK_CHUNK_ATTEMPTS)
                self._store.note_audio_pushed(talk_id, len(blob), talk_audio_remote_path(talk_id, item["audio_ext"]))
            elif client.read_branch_file(TALK_AUDIO_BRANCH, talk_audio_manifest_path(talk_id)) is None:
                raise TalkTranscribeError("本地暂存与云端录音均不存在，请重新选择录音上传")
            stage = "dispatch"
            # Preserve request identity if an accepted dispatch lost its reply.
            request_id = item.get("cloud_request_id") if item["status"] == "dispatching" else ""
            request_id = request_id or self._store.begin_cloud_request(talk_id)
            self._retry(lambda: client.dispatch_workflow(
                TALK_TRANSCRIBE_WORKFLOW, ref=branch,
                inputs={"talk_id": talk_id, "request_id": request_id}),
                TALK_DISPATCH_ATTEMPTS, TALK_DISPATCH_RETRY_SECONDS)
            self._store.mark_transcribing(talk_id)
            # Retain staged audio until a complete result is safely in SQLite.
        except InterruptedError:
            pass  # durable state is resumed at the next service startup
        except Exception as exc:
            self._store.set_status(talk_id, "failed", str(exc), error_stage=stage)
        finally:
            with self._lock:
                self._active.discard(talk_id)
                self._pump_locked()


class TalkJobs:
    """Single-background-worker queue so summaries never block the server.

    One shared instance per process also bounds concurrency: at most
    ``max_workers`` summaries run at once, extras fail fast with
    ``TalkBusyError`` so the UI can ask the user to wait instead of
    silently queueing expensive LLM calls.
    """

    def __init__(self, store: TalkStore, max_workers: int = 2):
        self._store = store
        self._max_workers = max(1, max_workers)
        self._lock = threading.RLock()
        self._running: set[str] = set()

    def is_running(self, talk_id: str) -> bool:
        with self._lock:
            return talk_id in self._running

    def submit(self, talk_id: str, title: str, transcript: str,
               base_url: str, model: str, api_key: str,
               provider_name: str) -> bool:
        """Start a background summary.

        Returns True on launch.  Returns False when this talk already has
        a run in flight; raises TalkBusyError when the worker pool is full.
        """
        with self._lock:
            if talk_id in self._running:
                return False
            if len(self._running) >= self._max_workers:
                raise TalkBusyError(
                    f"已有 {self._max_workers} 个讲座正在生成，请稍后再试"
                )
            self._running.add(talk_id)
        with self._store._lock, closing(self._store._connect()) as db, db:
            db.execute("UPDATE talks SET cloud_request_id='' WHERE id=?", (talk_id,))
        self._store.set_status(talk_id, "summarizing")
        model_id = f"{provider_name}/{model}"

        def worker() -> None:
            try:
                body, ai_title, _ = summarize_talk_text(
                    base_url, model, api_key, title, transcript,
                )
                self._store.save_summary(talk_id, body, ai_title, model_id)
            except TalkLLMError as exc:
                self._store.set_status(
                    talk_id, "failed", str(exc), error_stage="summarize"
                )
            except Exception as exc:  # never leave a talk stuck mid-flight
                self._store.set_status(
                    talk_id, "failed", f"{type(exc).__name__}: {exc}",
                    error_stage="summarize",
                )
            finally:
                with self._lock:
                    self._running.discard(talk_id)

        thread = threading.Thread(
            target=worker, name=f"talk-summary-{talk_id}", daemon=True
        )
        thread.start()
        return True


def _default_talk_dir() -> Path:
    override = os.environ.get("ICOURSE_WEB_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "library"
    return default_config_dir() / "library"
