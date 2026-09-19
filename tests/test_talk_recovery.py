from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_web.github_client import GitHubAPIError, GitHubClient
from local_web.talks import TalkStore, TalkUploadQueue, encrypt_talk_blob, decrypt_talk_blob, summarize_talk_text, TalkLLMError
from local_web.talk_cloud import TalkCloudSync
from local_web.state import RuntimeCredentials
from scripts import transcribe_talk as pipeline


class MemoryGitHub:
    def __init__(self):
        self.files = {}
        self.runs = None
        self.dispatches = []
    def default_branch(self): return "main"
    def workflow_file_on_ref(self, *args): return True
    def ensure_branch(self, *args, **kwargs): return "sha"
    def read_branch_file(self, branch, path): return self.files.get(path)
    def write_branch_file(self, branch, path, data, message): self.files[path] = data
    def set_timeout(self, timeout): pass
    def reset_timeout(self): pass
    def dispatch_workflow(self, name, **kwargs): self.dispatches.append(kwargs)
    def talk_workflow_run(self, *args): return self.runs


class TalkRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TalkStore(Path(self.tmp.name) / "library")
        self.client = MemoryGitHub()
        self.queue = TalkUploadQueue(self.store, lambda: self.client)
        self.item = self.store.create_audio_talk("讲座", "mp3", 0, "")
        self.id = self.item["id"]
        self.credentials = RuntimeCredentials("token", "student", "password")
        self.cloud = TalkCloudSync(self.store, self.queue, lambda: self.client, lambda: self.credentials)

    def test_unexpected_upload_error_is_visible_and_resumable(self):
        self.queue.stage_blob(self.id, b"audio", "mp3", "讲座")
        self.client.ensure_branch = Mock(side_effect=TimeoutError("timeout"))
        self.queue._run(self.id)
        self.assertEqual(self.store.get_talk(self.id)["status"], "failed")
        self.assertEqual(self.store.get_talk(self.id)["error_stage"], "upload")
        self.assertEqual(self.queue.staged_blob(self.id), b"audio")

    def test_no_staging_can_resume_from_cloud_manifest(self):
        self.client.files[f"talk_audio/{self.id}/manifest.json"] = b'{}'
        self.queue._run(self.id)
        self.assertEqual(self.store.get_talk(self.id)["status"], "transcribing")
        self.assertEqual(len(self.client.dispatches), 1)

    def test_startup_only_resumes_incomplete_uploads(self):
        self.store.set_status(self.id, "transcribing")
        self.queue.stage_blob(self.id, b"audio", "mp3", "讲座")
        with patch.object(self.queue, "submit", return_value=True) as submit:
            self.assertEqual(self.queue.resume_pending(), 0)
            submit.assert_not_called()
            self.store.set_status(self.id, "dispatching")
            self.assertEqual(self.queue.resume_pending(), 1)

    def test_deleted_talk_is_not_recreated_by_cloud_save(self):
        self.store.delete_talk(self.id)
        with self.assertRaises(ValueError):
            self.store.save_cloud_result(self.id, "转写", "正文")

    def test_duplicate_sync_preserves_versions_and_newer_manual_note(self):
        request = self.store.begin_cloud_request(self.id)
        data = {"talk_id": self.id, "request_id": request, "status": "ready",
                "transcript": "转写", "summary": "云端正文", "ai_title": "标题", "summary_model": "p/m"}
        self.client.files[f"talk_transcripts/{self.id}.json.enc"] = encrypt_talk_blob(json.dumps(data).encode(), "student", "password")
        self.cloud.sync(self.id)
        self.cloud.sync(self.id)
        self.assertEqual(len(self.store.get_talk(self.id)["versions"]), 1)
        self.store.save_summary(self.id, "人工重新生成", "", "p/m2")
        self.cloud.sync(self.id)
        self.assertEqual(self.store.get_talk(self.id)["summary"], "人工重新生成")

    def test_old_failed_result_does_not_cancel_retry(self):
        self.store.begin_cloud_request(self.id)
        data = {"talk_id": self.id, "request_id": "old", "status": "failed", "error": "old failure"}
        self.client.files[f"talk_transcripts/{self.id}.json.enc"] = encrypt_talk_blob(json.dumps(data).encode(), "student", "password")
        self.assertEqual(self.cloud.sync(self.id)["status"], "dispatching")

    def test_actions_failure_before_script_is_visible(self):
        self.store.begin_cloud_request(self.id)
        self.store.mark_transcribing(self.id)
        self.client.runs = {"status": "completed", "conclusion": "failure"}
        result = self.cloud.sync(self.id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("failure", result["error"])

    def test_summary_failure_preserves_transcript(self):
        request = self.store.begin_cloud_request(self.id)
        data = {"talk_id": self.id, "request_id": request, "status": "failed", "error": "model offline",
                "error_stage": "summarize", "transcript": "完整转写"}
        self.client.files[f"talk_transcripts/{self.id}.json.enc"] = encrypt_talk_blob(json.dumps(data).encode(), "student", "password")
        self.assertEqual(self.cloud.sync(self.id)["transcript"], "完整转写")
        self.assertEqual(self.store.get_talk(self.id)["error_stage"], "summarize")

    def test_local_summary_interrupted_on_restart_can_retry(self):
        paste = self.store.create_talk("", "转写")
        self.store.set_status(paste["id"], "summarizing")
        self.store.recover_interrupted()
        self.assertEqual(self.store.get_talk(paste["id"])["status"], "failed")

    def test_title_only_is_not_a_note(self):
        with patch("local_web.talks.chat_complete", return_value="# 标题"):
            with self.assertRaises(TalkLLMError):
                summarize_talk_text("https://example.com", "model", "key", "讲座", "转写")


class CloudPipelineTest(unittest.TestCase):
    def test_full_upload_failure_retry_and_duplicate_run(self):
        """Encrypted real envelope, >1 MB chunk, failed LLM, checkpoint reuse."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TalkStore(Path(tmp) / "library")
            client = MemoryGitHub()
            queue = TalkUploadQueue(store, lambda: client)
            item = store.create_audio_talk("讲座", "mp3", 0, "")
            talk_id = item["id"]
            audio = b"ID3" + b"x" * (5 * 1024 * 1024)
            blob = encrypt_talk_blob(audio, "student", "password")
            queue.stage_blob(talk_id, blob, "mp3", "讲座")
            queue._run(talk_id)
            self.assertEqual(store.get_talk(talk_id)["status"], "transcribing")
            outputs = []
            def write(branch, path, data, message):
                client.files[path] = data
                outputs.append(json.loads(decrypt_talk_blob(data, "student", "password")))
            transcriber = Mock()
            transcriber.transcribe_video.return_value = ("可重用的转写内容", [{"text": "语音"}])
            summarizer = Mock()
            summarizer.summarize.side_effect = RuntimeError("model unavailable")
            asr_module = types.SimpleNamespace(Transcriber=Mock(return_value=transcriber))
            llm_module = types.SimpleNamespace(Summarizer=Mock(return_value=summarizer))
            env = {"TALK_ID": talk_id, "TALK_REQUEST_ID": store.get_talk(talk_id)["cloud_request_id"],
                   "STUID": "student", "UISPSW": "password"}
            with patch.dict(os.environ, env), patch.dict(sys.modules, {"src.ai.transcriber": asr_module, "src.ai.summarizer": llm_module}), patch.object(pipeline, "read_branch_file", client.read_branch_file), patch.object(pipeline, "write_branch_file", write):
                self.assertEqual(pipeline.run(), 1)
                self.assertEqual(outputs[-1]["status"], "failed")
                self.assertEqual(outputs[-1]["transcript"], "可重用的转写内容")
                self.assertEqual(outputs[0]["status"], "summarizing")
                summarizer.summarize.side_effect = None
                summarizer.summarize.return_value = ("# 笔记标题\n\n完整笔记", "p/m")
                self.assertEqual(pipeline.run(), 0)
                self.assertEqual(pipeline.run(), 0)
            self.assertEqual(transcriber.transcribe_video.call_count, 1)
            self.assertEqual(summarizer.summarize.call_count, 2)
            self.assertNotIn("以下是课程", summarizer.summarize.call_args.args[1])
            cloud = TalkCloudSync(store, queue, lambda: client, lambda: RuntimeCredentials("token", "student", "password"))
            self.assertEqual(cloud.sync(talk_id)["summary"], "完整笔记")
            self.assertIsNone(queue.staged_blob(talk_id))
            self.assertEqual(len(store.get_talk(talk_id)["versions"]), 1)

    def test_large_file_uses_raw_contents_api(self):
        with patch.object(pipeline, "_api", return_value=b"large audio") as api:
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
                self.assertEqual(pipeline.read_branch_file("talk-audio", "part.enc"), b"large audio")
            self.assertTrue(api.call_args.kwargs["raw"])
        gh = GitHubClient("owner", "repo", "token")
        with patch.object(gh, "_json", return_value={"type": "file", "encoding": "none", "content": ""}), patch.object(gh, "_request", return_value=b"large result") as raw:
            self.assertEqual(gh.read_branch_file("talk-audio", "large.enc"), b"large result")
            self.assertEqual(raw.call_args.kwargs["accept"], "application/vnd.github.raw+json")

    def test_invalid_manifest_cannot_escape_audio_directory(self):
        with self.assertRaises(ValueError):
            pipeline.validate_manifest({"talk_id": "t1", "version": 1, "ext": "mp3", "bytes": 42,
                                        "chunks": ["../../secret"]}, "t1")


class TranscriberProcessTest(unittest.TestCase):
    def transcriber(self):
        import array
        numpy = types.SimpleNamespace(float32="float32", frombuffer=lambda data, dtype: array.array("f", data))
        with patch.dict(sys.modules, {"numpy": numpy, "sherpa_onnx": types.ModuleType("sherpa_onnx")}):
            spec = importlib.util.spec_from_file_location("test_transcriber_runtime", Path("src/ai/transcriber.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        obj = module.Transcriber()
        obj._init = lambda: None
        obj._reset_vad = lambda: None
        obj._vad = Mock()
        obj._vad.empty.return_value = True
        return obj

    def test_stalled_process_has_a_real_deadline(self):
        import time
        obj = self.transcriber()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            obj._transcribe_with_inline_ffmpeg([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)

    def test_nonzero_decoder_exit_never_counts_as_success(self):
        obj = self.transcriber()
        with self.assertRaises(RuntimeError):
            obj._transcribe_with_inline_ffmpeg([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\0'*64000); sys.exit(7)"], timeout=3)

    def test_unaligned_read_and_final_flush_are_preserved(self):
        obj = self.transcriber()
        blocks = iter([b"\0", b"\0\0\0", b"", b"\0\0\0\0", b"", b""])
        obj._consume_pcm_stream(lambda n: next(blocks), lambda: True, lambda: b"", lambda: 0, 3)
        self.assertEqual(obj._last_duration, 2 / 16000)
