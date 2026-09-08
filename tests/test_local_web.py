from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from local_web.database import DatabaseManager, auto_lecture_title
from local_web.github_client import GitHubClient
from local_web.server import create_app
from local_web.state import (
    CourseZoneStore,
    LectureNameStore,
    ObsidianSettings,
    ObsidianSettingsStore,
    RepositorySettings,
    RuntimeCredentials,
    RuntimeState,
    SettingsStore,
    SubscriptionSettingsStore,
    normalize_course_zone,
)
from src.data.schema import SCHEMA_SQL


class SettingsStoreTest(unittest.TestCase):
    def test_persists_only_repository_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            expected = RepositorySettings("alice", "fork", "private-data")
            store.save(expected)
            self.assertEqual(store.load(), expected)
            text = store.path.read_text("utf-8")
            self.assertNotIn("token", text)
            self.assertNotIn("password", text)

    def test_persists_only_obsidian_export_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ObsidianSettingsStore(Path(directory))
            expected = ObsidianSettings(
                vault_path="/Users/example/Documents/学习 Vault",
                include_transcript=True,
                include_ocr=False,
            )
            store.save(expected)
            self.assertEqual(store.load(), expected)
            text = store.path.read_text("utf-8")
            self.assertNotIn("摘要", text)
            self.assertNotIn("token", text)

    def test_persists_subscription_ids_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SubscriptionSettingsStore(Path(directory))
            store.save(["1001", "1002", "1001", "bad,id"])
            self.assertEqual(store.load(), ["1001", "1002"])
            text = store.path.read_text("utf-8")
            self.assertNotIn("token", text)

    def test_persists_local_course_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CourseZoneStore(Path(directory))
            store.save({"1001": "study", "1002": "archive", "bad": "unknown"})
            self.assertEqual(store.load(), {"1001": "study", "1002": "archive"})

    def test_persists_local_lecture_names(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LectureNameStore(Path(directory))
            store.save({"10": "第一课 导论", " ": "无效", "11": "  "})
            self.assertEqual(store.load(), {"10": "第一课 导论"})
            text = store.path.read_text("utf-8")
            self.assertNotIn("无效", text)

    def test_migrates_localized_and_legacy_course_zone_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CourseZoneStore(Path(directory))
            store.path.write_text(
                '{"zones":{"1001":"学习区","1002":"archive","bad":"未知"}}',
                encoding="utf-8",
            )
            self.assertEqual(store.load(), {"1001": "study", "1002": "archive"})
            self.assertEqual(normalize_course_zone("归档区"), "archive")

    def test_runtime_state_writes_a_course_zone_for_the_next_launch(self):
        class EmptyKeychain:
            available = False

            def load(self, _settings):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = RuntimeState(
                store=SettingsStore(path),
                course_zone_store=CourseZoneStore(path),
                credential_store=EmptyKeychain(),
            )
            state.save_course_zone("1001", "查阅区")
            restored = CourseZoneStore(path).load()
            self.assertEqual(restored, {"1001": "reference"})

    def test_runtime_state_renames_and_resets_a_lecture_name(self):
        class EmptyKeychain:
            available = False

            def load(self, _settings):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = RuntimeState(
                store=SettingsStore(path),
                course_zone_store=CourseZoneStore(path),
                credential_store=EmptyKeychain(),
            )
            state.save_lecture_name("10", "自定义名称")
            self.assertEqual(LectureNameStore(path).load(), {"10": "自定义名称"})
            state.save_lecture_name("10", "")
            self.assertEqual(LectureNameStore(path).load(), {})

    def test_remembered_credentials_use_injected_keychain_store(self):
        class FakeKeychain:
            available = True

            def __init__(self):
                self.value = RuntimeCredentials("old-token", "old-id", "old-password")

            def load(self, _settings):
                return self.value

            def save(self, _settings, credentials):
                self.value = credentials

            def delete(self, _settings):
                self.value = None

        with tempfile.TemporaryDirectory() as directory:
            keychain = FakeKeychain()
            state = RuntimeState(
                store=SettingsStore(Path(directory)), credential_store=keychain
            )
            self.assertTrue(state.credentials_remembered)
            state.configure(
                RepositorySettings("alice", "fork", "data"),
                token="new-token",
                stuid="new-id",
                uispsw="new-password",
                remember=True,
            )
            self.assertEqual(keychain.value.token, "new-token")
            state.forget_remembered_credentials()
            self.assertIsNone(state.credentials)


class FakeGitHubClient(GitHubClient):
    def __init__(self):
        super().__init__("alice", "fork", "secret")

    def _json(self, path, **kwargs):
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "commit-1"}}
        if "/git/commits/" in path:
            return {"tree": {"sha": "tree-1"}}
        if "/git/trees/" in path:
            return {
                "tree": [
                    {"type": "blob", "path": "data/icourse-index.enc", "sha": "idx", "size": 12},
                    {"type": "blob", "path": "data/shards/meta-0000.db.gz.enc", "sha": "meta", "size": 34},
                    {"type": "blob", "path": "README.md", "sha": "readme", "size": 1},
                ]
            }
        raise AssertionError(path)


class GitHubClientTest(unittest.TestCase):
    def test_builds_sharded_manifest(self):
        manifest = FakeGitHubClient().data_manifest("data")
        self.assertEqual(manifest.commit_sha, "commit-1")
        self.assertEqual(manifest.index.sha, "idx")
        self.assertEqual([item.name for item in manifest.shards], ["meta-0000.db.gz.enc"])


class DatabaseQueryTest(unittest.TestCase):
    def test_queries_local_read_only_database(self):
        with tempfile.TemporaryDirectory() as cache:
            manager = DatabaseManager(Path(cache))
            try:
                with closing(sqlite3.connect(manager.db_path)) as db:
                    db.executescript(SCHEMA_SQL)
                    db.execute("INSERT INTO courses VALUES ('1', '测试课程', '教师')")
                    db.execute(
                        """INSERT INTO all_courses
                           (course_id, term, title, teacher, dept)
                           VALUES ('1', '2026-秋', '测试课程', '教师', '历史系'),
                                  ('2', '2026-秋', '目录课程', '老师', '社会学院')"""
                    )
                    db.execute(
                        "INSERT INTO meta VALUES ('subscribed_course_ids', '1,2')"
                    )
                    db.execute(
                        """INSERT INTO lectures
                           (sub_id, course_id, sub_title, date, transcript, summary,
                            processed_at, summary_model)
                           VALUES ('10', '1', '第一讲', '2026-01-01', '转录关键词',
                                   '摘要关键词', '2026-01-01', 'test/model')"""
                    )
                    db.execute(
                        """INSERT INTO ppt_pages
                           (sub_id, page_num, created_sec, text, ocr_status)
                           VALUES ('10', 1, 15, 'OCR 关键词', 'done')"""
                    )
                    db.commit()
                manager.commit_sha = "fixture"
                self.assertEqual(manager.stats()["ready"], 1)
                self.assertEqual(manager.courses()[0]["title"], "测试课程")
                self.assertEqual(manager.lectures("1")[0]["sub_id"], "10")
                self.assertEqual(manager.lecture("10")["summary"], "摘要关键词")
                self.assertEqual(
                    manager.search("关键词")["results"][0]["hit_field"], "summary"
                )
                self.assertEqual(manager.subscription_ids(), ["1", "2"])
                self.assertEqual(manager.subscription_terms(), ["2026-秋"])
                self.assertEqual(
                    manager.subscription_catalog("目录")[0]["course_id"], "2"
                )
                self.assertEqual(
                    [item["course_id"] for item in manager.subscription_courses(["2", "1"])],
                    ["2", "1"],
                )
                default_notes = manager.obsidian_notes()
                self.assertEqual(default_notes[0]["summary_model"], "test/model")
                self.assertNotIn("transcript", default_notes[0])
                self.assertNotIn("ocr_pages", default_notes[0])
                detailed_notes = manager.obsidian_notes(
                    include_transcript=True, include_ocr=True
                )
                self.assertEqual(detailed_notes[0]["transcript"], "转录关键词")
                self.assertEqual(detailed_notes[0]["ocr_pages"][0]["text"], "OCR 关键词")
            finally:
                manager.close()

    def _search_fixture(self, cache: Path) -> DatabaseManager:
        manager = DatabaseManager(cache)
        with closing(sqlite3.connect(manager.db_path)) as db:
            db.executescript(SCHEMA_SQL)
            db.execute("INSERT INTO courses VALUES ('1', '搜索课程', '教师')")
            db.execute("INSERT INTO courses VALUES ('2', '另一门课', '老师')")
            db.executemany(
                """INSERT INTO lectures
                   (sub_id, course_id, sub_title, date, transcript, summary,
                    processed_at, summary_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    # 标题命中
                    ("t1", "1", "贝叶斯定理", "2026-01-01", "", "无关键词",
                     "2026-01-03", "m"),
                    # 摘要命中：关键词藏在长文本中部且带 markdown 标记，
                    # 验证片段居中且去除语法符号
                    ("t2", "1", "第二讲", "2026-01-02",
                     "", "开头" + "长" * 200 + "**贝叶斯**" + "尾" * 200,
                     "2026-01-02", "m"),
                    # 仅转录命中
                    ("t3", "1", "第三讲", "2026-01-03", "转录里的贝叶斯", "",
                     "2026-01-01", "m"),
                    # 另一门课，仅摘要命中
                    ("t4", "2", "第四讲", "2026-01-04", "", "贝叶斯主义", 
                     "2026-01-04", "m"),
                ],
            )
            db.execute(
                """INSERT INTO ppt_pages (sub_id, page_num, created_sec, text, ocr_status)
                   VALUES ('t3', 1, 10, 'PPT 上的贝叶斯公式', 'done')"""
            )
            db.commit()
        manager.commit_sha = "fixture"
        return manager

    def test_search_title_snippet_keywords_and_ranking(self):
        with tempfile.TemporaryDirectory() as cache:
            manager = self._search_fixture(Path(cache))
            try:
                result = manager.search("贝叶斯")
                self.assertEqual(result["total"], 4)
                # 排序：标题 > 摘要 > 转录（同域按处理时间倒序：t4 在 t2 前）
                self.assertEqual(
                    [item["sub_id"] for item in result["results"]],
                    ["t1", "t4", "t2", "t3"],
                )
                self.assertEqual(
                    [item["hit_field"] for item in result["results"]],
                    ["title", "summary", "summary", "transcript"],
                )
                # 片段居中：长摘要的片段应包含关键词而非只有开头；
                # markdown 语法符号应被剥除
                long_hit = next(
                    r for r in result["results"] if r["sub_id"] == "t2"
                )
                self.assertIn("贝叶斯", long_hit["snippet"])
                self.assertNotIn("*", long_hit["snippet"])
                self.assertTrue(long_hit["snippet"].startswith("…"))
                # 标题使用 AI/派生标题而非时间节次
                self.assertTrue(long_hit["auto_title"].startswith("开头"))
                t4_hit = next(
                    r for r in result["results"] if r["sub_id"] == "t4"
                )
                self.assertEqual(t4_hit["auto_title"], "贝叶斯主义")
                # 多关键词 AND
                self.assertEqual(manager.search("贝叶斯 主义")["total"], 1)
                self.assertEqual(manager.search("贝叶斯 不存在词")["total"], 0)
                # 域过滤
                titles = manager.search("贝叶斯", domains=["title"])
                self.assertEqual([r["sub_id"] for r in titles["results"]], ["t1"])
                ocr = manager.search("贝叶斯", domains=["ocr"])
                self.assertEqual([r["sub_id"] for r in ocr["results"]], ["t3"])
                self.assertIn("贝叶斯", ocr["results"][0]["snippet"])
                # 课程过滤
                scoped = manager.search("贝叶斯", course_id="2")
                self.assertEqual([r["sub_id"] for r in scoped["results"]], ["t4"])
                # 分页
                first = manager.search("贝叶斯", page=1, page_size=2)
                self.assertTrue(first["has_more"])
                second = manager.search("贝叶斯", page=2, page_size=2)
                self.assertFalse(second["has_more"])
                self.assertEqual(first["total"], second["total"])
                self.assertEqual(
                    [r["sub_id"] for r in first["results"] + second["results"]],
                    ["t1", "t4", "t2", "t3"],
                )
            finally:
                manager.close()

    def test_restores_encrypted_persistent_database(self):
        credentials = RuntimeCredentials("token", "student", "password")
        with tempfile.TemporaryDirectory() as cache:
            manager = DatabaseManager(Path(cache))
            try:
                with closing(sqlite3.connect(manager.db_path)) as db:
                    db.executescript(SCHEMA_SQL)
                    db.execute("INSERT INTO courses VALUES ('1', '本地课程', '教师')")
                    db.commit()
                manager.commit_sha = "persisted-commit"
                manager._persist_encrypted(credentials)
            finally:
                manager.close()

            restored = DatabaseManager(Path(cache))
            try:
                self.assertTrue(restored.unlock_persistent(credentials))
                self.assertEqual(restored.stats()["courses"], 1)
                self.assertEqual(restored.commit_sha, "persisted-commit")
                self.assertFalse(restored.persistent_db_path.read_bytes().startswith(b"SQLite"))
            finally:
                restored.close()


class AutoUpdateLifecycleTest(unittest.TestCase):
    def test_reading_status_does_not_start_a_second_auto_update(self):
        class RecordingDatabase:
            def __init__(self, directory: Path):
                self.db_path = directory / "not-yet-synced.db"
                self.commit_sha = None
                self.sync_calls = 0
                self.synced = threading.Event()

            def unlock_persistent(self, _credentials):
                return False

            def sync(self, _github, _branch, _credentials):
                self.sync_calls += 1
                self.synced.set()
                return {"unchanged": True}

            def close(self):
                pass

        class EmptyKeychain:
            available = False

            def load(self, _settings):
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = RuntimeState(
                store=SettingsStore(path), credential_store=EmptyKeychain()
            )
            state.settings = RepositorySettings("alice", "fork", "data")
            state.credentials = RuntimeCredentials("token", "student", "password")
            database = RecordingDatabase(path)
            app = create_app(state=state, database=database)
            status_endpoint = next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/local/status"
            )

            self.assertTrue(database.synced.wait(timeout=2))
            self.assertEqual(database.sync_calls, 1)
            asyncio.run(status_endpoint())
            asyncio.run(status_endpoint())
            self.assertEqual(database.sync_calls, 1)


class AutoLectureTitleTest(unittest.TestCase):
    def test_prefers_the_first_summary_heading(self):
        self.assertEqual(
            auto_lecture_title("## 一、导论与背景\n正文段落", "2026-03-05第6-8节"),
            "一、导论与背景",
        )

    def test_falls_back_to_first_line_and_strips_markdown(self):
        self.assertEqual(
            auto_lecture_title("**加粗**的第一行 [链接](https://x)\n第二行", "t"),
            "加粗的第一行 链接",
        )

    def test_skips_math_and_falls_back_to_sub_title(self):
        self.assertEqual(
            auto_lecture_title("$E=mc^2$", "2026-03-05第6-8节"),
            "2026-03-05第6-8节",
        )

    def test_falls_back_when_no_summary(self):
        self.assertEqual(auto_lecture_title("", "2026-03-05第6-8节"), "2026-03-05第6-8节")
        self.assertEqual(auto_lecture_title(None, None), "")

    def test_truncates_long_names(self):
        title = auto_lecture_title("一" * 60, "t", max_length=40)
        self.assertEqual(len(title), 40)
        self.assertTrue(title.endswith("…"))


if __name__ == "__main__":
    unittest.main()
