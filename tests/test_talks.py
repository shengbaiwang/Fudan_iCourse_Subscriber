from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web.server import create_app
from local_web.state import (
    RepositorySettings,
    RuntimeCredentials,
    RuntimeState,
    SettingsStore,
)
from local_web.talks import (
    ALLOWED_AUDIO_EXTENSIONS,
    MAX_AUDIO_BYTES,
    TALK_AUDIO_BRANCH,
    TALK_AUDIO_CHUNK_BYTES,
    TALK_CHUNK_ATTEMPTS,
    TALK_CHUNK_TIMEOUT,
    TALK_DISPATCH_ATTEMPTS,
    TALK_DISPATCH_RETRY_SECONDS,
    TALK_TRANSCRIBE_WORKFLOW,
    TALK_UPLOAD_DEADLINE_SECONDS,
    TalkBusyError,
    TalkJobs,
    TalkStore,
    TalkTranscribeError,
    TalkUploadQueue,
    auto_talk_title,
    build_talk_audio_manifest,
    build_talk_prompt,
    decrypt_talk_blob,
    encrypt_talk_blob,
    split_talk_blob,
    summarize_talk_text,
    talk_audio_chunk_path,
    talk_audio_manifest_path,
    talk_audio_remote_path,
    talk_transcript_remote_path,
    validate_audio_upload,
    validate_talk_input,
)
from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content


class EmptyKeychain:
    available = False

    def load(self, _settings):
        return None


def _state(directory: Path) -> RuntimeState:
    return RuntimeState(
        store=SettingsStore(directory), credential_store=EmptyKeychain()
    )


class TalkValidationTest(unittest.TestCase):
    def test_rejects_empty_transcript(self):
        with self.assertRaises(ValueError):
            validate_talk_input("标题", "   ")

    def test_truncates_title_and_accepts_text(self):
        title, text = validate_talk_input("t" * 200, "  转写内容  ")
        self.assertEqual(len(title), 100)
        self.assertEqual(text, "转写内容")

    def test_rejects_oversized_transcript(self):
        with self.assertRaises(ValueError):
            validate_talk_input("", "x" * 200_001)

    def test_prompt_uses_flat_format(self):
        prompt, mode = build_talk_prompt("转写")
        self.assertEqual(mode, "flat")
        self.assertIn("【音频转录（无时间轴）】", prompt)
        self.assertIn("转写", prompt)


class TalkAudioValidationTest(unittest.TestCase):
    def test_accepts_common_containers(self):
        ext, _ = validate_audio_upload("讲座.MP3", b"ID3" + b"x" * 100)
        self.assertEqual(ext, "mp3")
        ext, _ = validate_audio_upload("a.m4a", b"\x00\x00\x00\x14ftyp" + b"x" * 100)
        self.assertEqual(ext, "m4a")
        ext, _ = validate_audio_upload("a.wav", b"RIFF" + b"x" * 100)
        self.assertEqual(ext, "wav")

    def test_rejects_bad_extension_and_magic(self):
        with self.assertRaises(ValueError):
            validate_audio_upload("evil.exe", b"MZ" + b"x" * 100)
        with self.assertRaises(ValueError):
            validate_audio_upload("fake.mp3", b"RIFF" + b"x" * 100)
        with self.assertRaises(ValueError):
            validate_audio_upload("empty.mp3", b"")

    def test_rejects_oversize(self):
        with patch("local_web.talks.MAX_AUDIO_BYTES", 10):
            with self.assertRaises(ValueError):
                validate_audio_upload("a.mp3", b"ID3" + b"x" * 100)

    def test_remote_paths_are_namespaced(self):
        self.assertEqual(
            talk_audio_remote_path("t123", "m4a"), "talk_audio/t123"
        )
        self.assertEqual(
            talk_audio_manifest_path("t123"), "talk_audio/t123/manifest.json"
        )
        self.assertEqual(
            talk_audio_chunk_path("t123", 1), "talk_audio/t123/0001.part.enc"
        )
        self.assertEqual(
            talk_transcript_remote_path("t123"),
            "talk_transcripts/t123.json.enc",
        )
        # Path traversal in the id cannot escape the namespace.
        self.assertNotIn(
            "..", talk_audio_remote_path("../../etc", "mp3")
        )
        self.assertEqual(TALK_AUDIO_BRANCH, "talk-audio")
        self.assertEqual(TALK_TRANSCRIBE_WORKFLOW, "talk_transcribe.yml")
        self.assertIn("mp4", ALLOWED_AUDIO_EXTENSIONS)
        self.assertEqual(MAX_AUDIO_BYTES, 500 * 1024 * 1024)

    def test_chunking_roundtrip(self):
        self.assertEqual(TALK_AUDIO_CHUNK_BYTES, 4 * 1024 * 1024)
        self.assertEqual(TALK_CHUNK_TIMEOUT, 120)
        self.assertEqual(TALK_CHUNK_ATTEMPTS, 3)
        self.assertGreaterEqual(TALK_UPLOAD_DEADLINE_SECONDS, 30 * 60)
        self.assertGreaterEqual(TALK_DISPATCH_ATTEMPTS, 5)
        self.assertGreaterEqual(TALK_DISPATCH_RETRY_SECONDS, 10)
        data = b"x" * (2 * TALK_AUDIO_CHUNK_BYTES + 7)
        chunks = split_talk_blob(data)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(len(c) <= TALK_AUDIO_CHUNK_BYTES for c in chunks))
        manifest = build_talk_audio_manifest(
            "t1", "m4a", len(chunks), len(data), title="标题"
        )
        self.assertEqual(
            manifest["chunks"],
            ["0001.part.enc", "0002.part.enc", "0003.part.enc"],
        )
        self.assertEqual(manifest["ext"], "m4a")
        self.assertEqual(manifest["title"], "标题")
        self.assertEqual(
            {talk_audio_chunk_path("t1", i + 1).rsplit("/", 1)[-1]
             for i in range(len(chunks))},
            set(manifest["chunks"]),
        )

    def test_encrypt_decrypt_roundtrip(self):
        blob = encrypt_talk_blob(b"audio-bytes", "stu", "psw")
        self.assertEqual(decrypt_talk_blob(blob, "stu", "psw"), b"audio-bytes")
        with self.assertRaises(TalkTranscribeError):
            decrypt_talk_blob(blob, "stu", "wrong")


class TalkPromptParityTest(unittest.TestCase):
    def test_local_prompt_shape(self):
        # The local console must send the same prompt family as the CI run:
        # shared module, flat format, intentional 1:7 length hint.
        self.assertTrue(SYSTEM_PROMPT.startswith("你是一个专业的课程助教"))
        self.assertIn("第一行必须是一个一级标题", SYSTEM_PROMPT)
        self.assertIn("1:8", SYSTEM_PROMPT)
        user = summary_user_content("课程", "x" * 7000)
        self.assertIn("1000字", user)

    def test_ci_summarizer_reuses_shared_prompt(self):
        # summarizer.py must import from summary_prompt, not carry its own
        # copy — compare source text instead of importing (openai is a
        # CI-only dependency, absent from .venv-web).
        source = Path("src/ai/summarizer.py").read_text("utf-8")
        self.assertIn(
            "from src.ai.summary_prompt import SYSTEM_PROMPT", source
        )
        self.assertNotIn("你是一个专业的课程助教", source)

    def test_title_derivation(self):
        self.assertEqual(
            auto_talk_title("## 注意力机制\n正文", ""), "注意力机制"
        )
        self.assertEqual(auto_talk_title("", "文件名讲座"), "文件名讲座")
        self.assertEqual(auto_talk_title("", ""), "")


class TalkStoreTest(unittest.TestCase):
    def test_crud_and_summary_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            created = store.create_talk("自定义名", "转写文本")
            self.assertEqual(created["status"], "draft")
            self.assertEqual(created["display_title"], "自定义名")

            renamed = store.rename_talk(created["id"], "")
            assert renamed is not None
            self.assertEqual(renamed["display_title"], created["id"])

            store.save_summary(
                created["id"], "# AI标题\n正文", "AI标题", "p/m"
            )
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "ready")
            # Custom rename wins over the AI title.
            self.assertEqual(fetched["display_title"], "AI标题")
            self.assertEqual(len(fetched["versions"]), 1)
            self.assertEqual(fetched["versions"][0]["model"], "p/m")

            items = store.list_talks()
            self.assertEqual(len(items), 1)
            self.assertNotIn("transcript", items[0])
            self.assertNotIn("summary", items[0])

            self.assertTrue(store.delete_talk(created["id"]))
            self.assertIsNone(store.get_talk(created["id"]))
            self.assertEqual(store.list_talks(), [])

    def test_summary_rerun_appends_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            created = store.create_talk("", "转写")
            store.save_summary(created["id"], "正文一", "", "p/m1")
            store.save_summary(created["id"], "正文二", "", "p/m2")
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["summary"], "正文二")
            self.assertEqual(len(fetched["versions"]), 2)

    def test_audio_talk_lifecycle_and_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            created = store.create_audio_talk("录音", "m4a", 123, "talk_audio/t1")
            self.assertEqual(created["status"], "uploading")
            self.assertEqual(created["source"], "upload")
            self.assertEqual(created["audio_ext"], "m4a")
            # Chunk progress is tracked per chunk and survives re-opens.
            store.note_chunk_uploaded(created["id"], 0, 3)
            self.assertEqual(
                store.upload_progress(created["id"]), {"done": 0, "total": 3}
            )
            store.note_chunk_uploaded(created["id"], 1, 3)
            store.note_chunk_uploaded(created["id"], 1, 3)  # idempotent
            self.assertEqual(
                store.upload_progress(created["id"]), {"done": 1, "total": 3}
            )
            store.mark_transcribing(created["id"])
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "transcribing")
            # Transcript arrives from the workflow → back to a reviewable state.
            store.save_transcript(created["id"], "  转写文本  ")
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "transcribed")
            self.assertEqual(fetched["transcript"], "转写文本")
            with self.assertRaises(ValueError):
                store.save_transcript(created["id"], "   ")
            # Cloud payload with a summary lands at ready with a version —
            # same path as a manual save_summary (iCourse parity).
            status = store.save_cloud_result(
                created["id"], "转写文本", "正文", "云笔记", "p/m",
            )
            self.assertEqual(status, "ready")
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "ready")
            self.assertEqual(fetched["summary"], "正文")
            self.assertEqual(fetched["ai_title"], "云笔记")
            self.assertEqual(fetched["summary_model"], "p/m")
            self.assertEqual(len(fetched["versions"]), 1)
            # Transcript-only payload parks at transcribed for manual notes.
            created2 = store.create_talk("", "转写二")
            self.assertEqual(
                store.save_cloud_result(created2["id"], "转写二"), "transcribed"
            )
            with self.assertRaises(ValueError):
                store.save_cloud_result(created2["id"], "   ")
            # Pre-audio libraries gain the new columns without data loss.
            store2 = TalkStore(Path(directory))
            refetched = store2.get_talk(created["id"])
            assert refetched is not None
            self.assertEqual(refetched["remote_audio_path"], "talk_audio/t1")


class TalkJobsTest(unittest.TestCase):
    def test_background_summary_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            created = store.create_talk("", "转写文本")
            jobs = TalkJobs(store, max_workers=1)
            with patch(
                "local_web.talks.summarize_talk_text",
                return_value=("正文", "标题", "p/m"),
            ) as summary:
                self.assertTrue(
                    jobs.submit(
                        created["id"], "", "转写文本",
                        "https://x/v1", "m", "k", "p",
                    )
                )
                # Same talk twice → second is a no-op, not a duplicate run.
                self.assertFalse(
                    jobs.submit(
                        created["id"], "", "转写文本",
                        "https://x/v1", "m", "k", "p",
                    )
                )
                summary.assert_called_once()
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "ready")
            self.assertEqual(fetched["summary"], "正文")

    def test_pool_full_fails_fast(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            jobs = TalkJobs(store, max_workers=1)
            jobs._running.add("other")
            with self.assertRaises(TalkBusyError):
                jobs.submit("new", "", "t", "https://x", "m", "k", "p")

    def test_llm_failure_marks_failed(self):
        from local_web.talks import TalkLLMError

        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory))
            created = store.create_talk("", "转写文本")
            jobs = TalkJobs(store)
            with patch(
                "local_web.talks.summarize_talk_text",
                side_effect=TalkLLMError("模型 API 500"),
            ):
                self.assertTrue(
                    jobs.submit(
                        created["id"], "", "转写文本",
                        "https://x/v1", "m", "k", "p",
                    )
                )
            fetched = store.get_talk(created["id"])
            assert fetched is not None
            self.assertEqual(fetched["status"], "failed")
            self.assertIn("500", fetched["error"])


class TalkGitHubClientHelperTest(unittest.TestCase):
    def test_workflow_file_probe_and_latest_run(self):
        from local_web.github_client import GitHubAPIError, GitHubClient

        class FakeClient(GitHubClient):
            def __init__(self):
                super().__init__("alice", "fork", "secret")
                self.calls: list[str] = []

            def _request(self, path, *, method="GET", payload=None, accept=""):
                self.calls.append(path)
                if "workflows/talk_transcribe.yml/runs" in path:
                    return (
                        b'{"workflow_runs": [{"id": 7, "name": "T",'
                        b' "status": "completed"}]}'
                    )
                if "workflows/missing.yml/runs" in path:
                    return b'{"workflow_runs": []}'
                if ".github/workflows/talk_transcribe.yml" in path:
                    return b"{}"
                raise GitHubAPIError(404, "Not Found")

        client = FakeClient()
        self.assertTrue(client.workflow_file_on_ref("talk_transcribe.yml"))
        self.assertFalse(client.workflow_file_on_ref("nope.yml"))
        run = client.latest_workflow_run("talk_transcribe.yml")
        assert run is not None
        self.assertEqual(run["id"], 7)
        self.assertIsNone(client.latest_workflow_run("missing.yml"))


class TalkGitHubClientTest(unittest.TestCase):
    def test_branch_file_helpers(self):
        import base64

        from local_web.github_client import GitHubAPIError, GitHubClient

        class FakeClient(GitHubClient):
            def __init__(self):
                super().__init__("alice", "fork", "secret")
                self.files: dict[str, bytes] = {}
                self.branches = {"main": "sha-main"}

            def _json(self, path, **kwargs):
                if "/git/ref/heads/" in path:
                    branch = path.rsplit("/", 1)[-1]
                    if branch in self.branches:
                        return {"object": {"sha": self.branches[branch]}}
                    raise GitHubAPIError(404, "nope")
                if path.endswith("/git/refs") and kwargs.get("method") == "POST":
                    return {}
                if "/contents/" in path:
                    return self._contents_json(path, kwargs.get("method", "GET"), kwargs.get("payload"))
                raise AssertionError(path)

            def _contents_json(self, path, method, payload):
                key = path.split("/contents/")[1].split("?")[0]
                if method == "GET":
                    if key not in self.files:
                        raise GitHubAPIError(404, "nope")
                    return {
                        "type": "file",
                        "encoding": "base64",
                        "sha": "blob-sha",
                        "content": base64.b64encode(self.files[key]).decode(),
                    }
                raise AssertionError((method, path))

            def _request(self, path, *, method="GET", payload=None, accept=""):
                if path.endswith("/git/refs") and method == "POST":
                    self.branches["talk-audio"] = payload["sha"]
                    return b"{}"
                if "/contents/" in path:
                    key = path.split("/contents/")[1].split("?")[0]
                    if method == "GET":
                        if key not in self.files:
                            raise GitHubAPIError(404, "nope")
                        return (
                            '{"type":"file","encoding":"base64","content":"'
                            + base64.b64encode(self.files[key]).decode()
                            + '"}'
                        ).encode()
                    if method == "PUT":
                        self.files[key] = base64.b64decode(payload["content"])
                        return b"{}"
                    if method == "DELETE":
                        return b"{}" if self.files.pop(key, None) else (_ for _ in ()).throw(GitHubAPIError(404, "nope"))
                raise AssertionError((method, path))

        client = FakeClient()
        # Missing branch is created from main.
        self.assertEqual(client.ensure_branch("talk-audio"), "sha-main")
        self.assertIn("talk-audio", client.branches)
        self.assertIsNone(client.read_branch_file("talk-audio", "a/b.enc"))
        client.write_branch_file("talk-audio", "a/b.enc", b"data", message="m")
        self.assertEqual(client.read_branch_file("talk-audio", "a/b.enc"), b"data")
        self.assertTrue(client.delete_branch_file("talk-audio", "a/b.enc", "m"))
        self.assertFalse(client.delete_branch_file("talk-audio", "a/b.enc", "m"))


class TalkUploadQueueTest(unittest.TestCase):
    def _queue(self, store, client, directory):
        import os

        os.environ["ICOURSE_WEB_CONFIG_DIR"] = str(directory)
        return TalkUploadQueue(store, lambda: client)

    def test_dispatch_missing_workflow_file_gives_actionable_error(self):
        import tempfile

        from local_web.github_client import GitHubAPIError

        class NoWorkflowClient:
            def default_branch(self):
                return "main"

            def read_branch_file(self, branch, path):
                return getattr(self, "files", {}).get(path)

            def ensure_branch(self, branch, default_branch="main"):
                return "sha"

            def set_timeout(self, timeout):
                pass

            def reset_timeout(self):
                pass

            def write_branch_file(self, branch, path, data, message):
                pass

            def dispatch_workflow(self, workflow, ref="main", inputs=None):
                raise GitHubAPIError(404, "Not Found")

            def workflow_file_on_ref(self, workflow, ref="main"):
                return False

        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory) / "db")
            created = store.create_audio_talk("录音", "m4a", 0, "talk_audio/x")
            talk_id = created["id"]
            queue = self._queue(
                store, NoWorkflowClient(), Path(directory) / "cfg"
            )
            queue.stage_blob(talk_id, b"y", "m4a", "录音")
            with patch(
                "local_web.talks.TALK_DISPATCH_ATTEMPTS", 1
            ), patch(
                "local_web.talks.TALK_DISPATCH_RETRY_SECONDS", 0
            ):
                queue._run(talk_id)
            item = store.get_talk(talk_id)
            assert item is not None
            self.assertEqual(item["status"], "failed")
            self.assertEqual(item["error_stage"], "dispatch")
            self.assertIn("main", item["error"])
            self.assertIn("部署", item["error"])

    def test_dispatch_transient_failure_retries_then_succeeds(self):
        import tempfile

        from local_web.github_client import GitHubAPIError

        class FlakyDispatchClient:
            def __init__(self):
                self.calls = 0

            def default_branch(self):
                return "main"

            def read_branch_file(self, branch, path):
                return getattr(self, "files", {}).get(path)

            def ensure_branch(self, branch, default_branch="main"):
                return "sha"

            def set_timeout(self, timeout):
                pass

            def reset_timeout(self):
                pass

            def write_branch_file(self, branch, path, data, message):
                pass

            def dispatch_workflow(self, workflow, ref="main", inputs=None):
                self.calls += 1
                if self.calls < 3:
                    raise GitHubAPIError(500, "boom")

            def workflow_file_on_ref(self, workflow, ref="main"):
                return True

        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory) / "db")
            created = store.create_audio_talk("录音", "m4a", 0, "talk_audio/x")
            talk_id = created["id"]
            client = FlakyDispatchClient()
            queue = self._queue(store, client, Path(directory) / "cfg")
            queue.stage_blob(talk_id, b"y", "m4a", "录音")
            store.note_chunk_uploaded(talk_id, 0, 1)
            with patch(
                "local_web.talks.TALK_DISPATCH_RETRY_SECONDS", 0
            ):
                queue._run(talk_id)
            self.assertEqual(client.calls, 3)
            item = store.get_talk(talk_id)
            assert item is not None
            self.assertEqual(item["status"], "transcribing")

    def test_background_upload_skips_done_chunks_and_dispatches(self):
        import asyncio
        import os
        import tempfile

        from local_web.github_client import GitHubAPIError

        class FakeClient:
            def __init__(self, fail_first: set[str] | None = None):
                self.files: dict[str, bytes] = {}
                self.fail_first = set(fail_first or set())
                self.attempts: dict[str, int] = {}
                self.dispatched: list[tuple] = []
                self.timeouts: list[int] = []

            def default_branch(self):
                return "main"

            def read_branch_file(self, branch, path):
                return getattr(self, "files", {}).get(path)

            def ensure_branch(self, branch, default_branch="main"):
                return "sha"

            def set_timeout(self, timeout):
                self.timeouts.append(timeout)

            def reset_timeout(self):
                pass

            def write_branch_file(self, branch, path, data, message):
                self.attempts[path] = self.attempts.get(path, 0) + 1
                if path in self.fail_first and self.attempts[path] == 1:
                    raise GitHubAPIError(0, "The write operation timed out")
                self.files[path] = data

            def workflow_file_on_ref(self, workflow, ref="main"):
                return True

            def dispatch_workflow(self, workflow, ref="main", inputs=None):
                self.dispatched.append((workflow, ref, inputs))

        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory) / "db")
            created = store.create_audio_talk("录音", "m4a", 0, "talk_audio/x")
            talk_id = created["id"]
            fake = FakeClient()
            queue = self._queue(store, fake, Path(directory) / "cfg")
            blob = b"y" * (2 * TALK_AUDIO_CHUNK_BYTES + 10)
            total = queue.stage_blob(talk_id, blob, "m4a", "录音")
            self.assertEqual(total, 3)
            store.note_chunk_uploaded(talk_id, 0, total)
            store.bump_upload_attempts(talk_id)
            queue.submit(talk_id)
            # Wait for the background worker (tiny blob, no real network).
            for _ in range(200):
                item = store.get_talk(talk_id)
                if item and item["status"] == "transcribing":
                    break
                asyncio.run(asyncio.sleep(0.05))
            item = store.get_talk(talk_id)
            assert item is not None
            self.assertEqual(item["status"], "transcribing")
            self.assertEqual(len(fake.dispatched), 1)
            self.assertEqual(fake.dispatched[0][2]["talk_id"], talk_id)
            self.assertTrue(fake.dispatched[0][2]["request_id"])
            self.assertIn(f"talk_audio/{talk_id}/manifest.json", fake.files)
            self.assertEqual(queue.staged_blob(talk_id), blob)
            self.assertTrue(fake.timeouts)

    def test_retry_resumes_from_confirmed_chunks(self):
        import asyncio
        import tempfile

        from local_web.github_client import GitHubAPIError

        class FlakyClient:
            def __init__(self):
                self.files: dict[str, bytes] = {}
                self.calls: dict[str, int] = {}
                self.dispatched = 0

            def default_branch(self):
                return "main"

            def read_branch_file(self, branch, path):
                return getattr(self, "files", {}).get(path)

            def ensure_branch(self, branch, default_branch="main"):
                return "sha"

            def set_timeout(self, timeout):
                pass

            def reset_timeout(self):
                pass

            def write_branch_file(self, branch, path, data, message):
                self.calls[path] = self.calls.get(path, 0) + 1
                # First attempt always times out on chunk 2 only.
                if path.endswith("0002.part.enc") and self.calls[path] == 1:
                    raise GitHubAPIError(0, "The write operation timed out")
                self.files[path] = data

            def workflow_file_on_ref(self, workflow, ref="main"):
                return True

            def dispatch_workflow(self, workflow, ref="main", inputs=None):
                self.dispatched += 1

        with tempfile.TemporaryDirectory() as directory:
            store = TalkStore(Path(directory) / "db")
            created = store.create_audio_talk("录音", "m4a", 0, "talk_audio/x")
            talk_id = created["id"]
            flaky = FlakyClient()
            queue = self._queue(store, flaky, Path(directory) / "cfg")
            blob = b"z" * (TALK_AUDIO_CHUNK_BYTES + 5)
            total = queue.stage_blob(talk_id, blob, "m4a", "录音")
            self.assertEqual(total, 2)
            # Pretend chunk 1 was confirmed in a previous interrupted run.
            store.note_chunk_uploaded(talk_id, 1, total)
            queue.submit(talk_id)
            for _ in range(200):
                item = store.get_talk(talk_id)
                if item and item["status"] == "transcribing":
                    break
                asyncio.run(asyncio.sleep(0.05))
            item = store.get_talk(talk_id)
            assert item is not None
            self.assertEqual(item["status"], "transcribing")
            # Chunk 1 skipped locally; chunk 2 retried once then confirmed.
            chunk1 = f"talk_audio/{talk_id}/0001.part.enc"
            chunk2 = f"talk_audio/{talk_id}/0002.part.enc"
            self.assertNotIn(chunk1, flaky.calls)
            self.assertEqual(flaky.calls.get(chunk2), 2)
            self.assertEqual(flaky.dispatched, 1)


class TalkApiTest(unittest.TestCase):
    def _app(self, directory: Path):
        state = _state(Path(directory))
        state.settings = RepositorySettings("alice", "fork", "data")
        state.credentials = RuntimeCredentials("token", "student", "password")
        store = TalkStore(Path(directory) / "talks-lib")
        return create_app(state=state, talk_store=store)

    def _upload_route(self, app):
        for route in app.routes:
            if getattr(route, "path", "") == "/api/local/talks/upload":
                return route.endpoint
        raise AssertionError("upload route missing")

    def test_upload_route_registered(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            endpoint = self._upload_route(app)
            self.assertIn("upload_talk_audio", repr(endpoint.__qualname__))

    @staticmethod
    def _routes(app):
        routes: dict[tuple[str, str], object] = {}
        for route in app.routes:
            if not hasattr(route, "endpoint"):
                continue
            for method in getattr(route, "methods", ()) or ():
                routes[(method, route.path)] = route.endpoint
        return routes

    def test_crud_roundtrip(self):
        import asyncio

        from local_web.server import TalkCreateRequest, TalkRenameRequest

        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            routes = self._routes(app)
            create = routes[("POST", "/api/local/talks")]
            list_all = routes[("GET", "/api/local/talks")]
            get_one = routes[("GET", "/api/local/talks/{talk_id}")]
            delete_one = routes[("DELETE", "/api/local/talks/{talk_id}")]
            rename = routes[("PUT", "/api/local/talks/{talk_id}/title")]

            created = asyncio.run(
                create(TalkCreateRequest(title="讲座一", transcript="转写文本"))
            )
            talk_id = created["id"]

            items = asyncio.run(list_all())
            self.assertEqual(len(items), 1)

            fetched = asyncio.run(get_one(talk_id))
            self.assertEqual(fetched["transcript"], "转写文本")

            renamed = asyncio.run(
                rename(talk_id, TalkRenameRequest(name="新名"))
            )
            self.assertEqual(renamed["display_title"], "新名")

            cleared = asyncio.run(
                rename(talk_id, TalkRenameRequest(name=""))
            )
            self.assertEqual(cleared["display_title"], talk_id)

            deleted = asyncio.run(get_one(talk_id))
            self.assertIn("transcript", deleted)
            self.assertTrue(asyncio.run(delete_one(talk_id))["ok"])

    def test_summarize_validates_model_config(self):
        import asyncio

        from fastapi import HTTPException
        from pydantic import SecretStr

        from local_web.server import (
            TalkCreateRequest,
            TalkSummarizeRequest,
        )

        with tempfile.TemporaryDirectory() as directory:
            app = self._app(Path(directory))
            routes = self._routes(app)
            create = routes[("POST", "/api/local/talks")]
            summarize = routes[("POST", "/api/local/talks/{talk_id}/summarize")]
            created = asyncio.run(
                create(TalkCreateRequest(title="", transcript="转写文本"))
            )
            with self.assertRaises(HTTPException) as context:
                asyncio.run(
                    summarize(
                        created["id"],
                        TalkSummarizeRequest(
                            provider="nope", model="m",
                            api_key=SecretStr("k"),
                        ),
                    )
                )
            # No GitHub client in tests → falls back to defaults, where the
            # provider is unknown.
            self.assertIn(context.exception.status_code, (400, 502))


if __name__ == "__main__":
    unittest.main()
