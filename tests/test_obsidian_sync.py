from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_web.obsidian import (
    EXPORT_FOLDER,
    MANIFEST_NAME,
    ObsidianSyncError,
    ObsidianSyncService,
)


def sample_note(**overrides):
    note = {
        "sub_id": "lecture-001",
        "course_id": "course-001",
        "course_title": "测试课程",
        "teacher": "测试教师",
        "sub_title": "第一讲：导论",
        "date": "2026-08-02",
        "processed_at": "2026-08-02T12:00:00Z",
        "summary_model": "example/model",
        "summary": "这是一份课程摘要。",
        "transcript": "这是完整转录。",
        "ocr_pages": [
            {"page_num": 1, "created_sec": 12, "text": "PPT 第一页"},
            {"page_num": 2, "created_sec": 71, "text": "PPT 第二页"},
        ],
    }
    note.update(overrides)
    return note


class ObsidianSyncServiceTest(unittest.TestCase):
    def make_vault(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        vault = Path(temporary.name) / "我的 Vault"
        (vault / ".obsidian").mkdir(parents=True)
        return vault

    @staticmethod
    def only_note(vault: Path) -> Path:
        notes = list((vault / EXPORT_FOLDER).rglob("*.md"))
        assert len(notes) == 1
        return notes[0]

    def test_preview_then_sync_creates_markdown_and_manifest(self):
        vault = self.make_vault()
        service = ObsidianSyncService()

        preview = service.preview(vault, [sample_note()])
        self.assertEqual(preview["counts"], {
            "create": 1,
            "update": 0,
            "unchanged": 0,
            "conflict": 0,
        })
        self.assertEqual(preview["vault_path"], str(vault.resolve()))
        self.assertEqual(len(preview["items"]), 1)
        self.assertNotIn("..", Path(preview["items"][0]["path"]).parts)

        result = service.sync(preview["plan_id"])
        self.assertEqual(result["counts"], {
            "created": 1,
            "updated": 0,
            "unchanged": 0,
            "conflict": 0,
        })
        target = self.only_note(vault)
        self.assertTrue(target.resolve().is_relative_to(vault.resolve()))
        markdown = target.read_text("utf-8")
        self.assertIn("icourse_generated: true", markdown)
        self.assertIn('summary_model: "example/model"', markdown)
        self.assertIn("这是一份课程摘要。", markdown)
        self.assertNotIn("## 转录", markdown)
        self.assertNotIn("## PPT OCR", markdown)

        manifest = json.loads((vault / EXPORT_FOLDER / MANIFEST_NAME).read_text("utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(manifest["notes"]["lecture-001"]["path"], preview["items"][0]["path"])
        self.assertNotIn("这是一份课程摘要。", json.dumps(manifest, ensure_ascii=False))
        self.assertNotIn("这是完整转录。", json.dumps(manifest, ensure_ascii=False))

        second_preview = service.preview(vault, [sample_note()])
        self.assertEqual(second_preview["counts"]["unchanged"], 1)
        second_result = service.sync(second_preview["plan_id"])
        self.assertEqual(second_result["counts"]["unchanged"], 1)

    def test_changed_generated_note_updates_but_manual_edit_becomes_conflict(self):
        vault = self.make_vault()
        service = ObsidianSyncService()
        first = service.preview(vault, [sample_note()])
        service.sync(first["plan_id"])
        target = self.only_note(vault)
        unrelated = vault / "我的手写笔记.md"
        unrelated.write_text("请不要删掉我", encoding="utf-8")

        changed = service.preview(vault, [sample_note(summary="新版本摘要。")])
        self.assertEqual(changed["counts"]["update"], 1)
        updated = service.sync(changed["plan_id"])
        self.assertEqual(updated["counts"]["updated"], 1)
        self.assertIn("新版本摘要。", target.read_text("utf-8"))

        target.write_text(target.read_text("utf-8") + "\n我的手写补充\n", encoding="utf-8")
        conflict = service.preview(vault, [sample_note(summary="又一个版本。")])
        self.assertEqual(conflict["counts"]["conflict"], 1)
        self.assertIn("未覆盖", conflict["items"][0]["reason"])
        blocked = service.sync(conflict["plan_id"])
        self.assertEqual(blocked["counts"]["conflict"], 1)
        self.assertIn("我的手写补充", target.read_text("utf-8"))
        self.assertEqual(unrelated.read_text("utf-8"), "请不要删掉我")

    def test_transcript_and_ocr_are_explicit_opt_ins(self):
        vault = self.make_vault()
        service = ObsidianSyncService()
        preview = service.preview(
            vault,
            [sample_note()],
            include_transcript=True,
            include_ocr=True,
        )
        service.sync(preview["plan_id"])
        markdown = self.only_note(vault).read_text("utf-8")
        self.assertIn("## 转录", markdown)
        self.assertIn("这是完整转录。", markdown)
        self.assertIn("## PPT OCR", markdown)
        self.assertIn("00:12 · 第 1 页", markdown)
        self.assertIn("01:11 · 第 2 页", markdown)

    def test_ai_title_drives_filename_heading_and_caption(self):
        vault = self.make_vault()
        service = ObsidianSyncService()
        note = sample_note(ai_title="梯度下降法", sub_title="2026-08-02第1-2节")
        preview = service.preview(vault, [note])
        self.assertEqual(preview["items"][0]["title"], "梯度下降法")
        self.assertIn("梯度下降法", preview["items"][0]["path"])
        service.sync(preview["plan_id"])
        markdown = self.only_note(vault).read_text("utf-8")
        self.assertIn("# 梯度下降法", markdown)
        self.assertIn("*2026-08-02第1-2节*", markdown)

        # Existing files keep their path even when the AI title later changes
        # (the manifest pins the original path; no orphan files are created).
        changed = service.preview(
            vault,
            [sample_note(ai_title="随机梯度下降", sub_title="2026-08-02第1-2节")],
        )
        self.assertEqual(changed["items"][0]["path"], preview["items"][0]["path"])

    def test_requires_absolute_initialized_vault_and_rejects_symlink_output(self):
        service = ObsidianSyncService()
        with self.assertRaisesRegex(ObsidianSyncError, "绝对路径"):
            service.preview("relative-vault", [sample_note()])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            not_vault = root / "not-a-vault"
            not_vault.mkdir()
            with self.assertRaisesRegex(ObsidianSyncError, "初始化"):
                service.preview(not_vault, [sample_note()])

            vault = root / "vault"
            (vault / ".obsidian").mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            try:
                (vault / EXPORT_FOLDER).symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"此环境无法创建符号链接：{exc}")
            with self.assertRaisesRegex(ObsidianSyncError, "符号链接"):
                service.preview(vault, [sample_note()])

    def test_unknown_same_path_and_consumed_plan_are_safe(self):
        vault = self.make_vault()
        service = ObsidianSyncService()
        first_preview = service.preview(vault, [sample_note()])
        relative = first_preview["items"][0]["path"]
        target = vault / EXPORT_FOLDER / relative
        target.parent.mkdir(parents=True)
        target.write_text("已有但不属于同步器的文件", encoding="utf-8")

        conflict = service.preview(vault, [sample_note()])
        self.assertEqual(conflict["counts"]["conflict"], 1)
        service.sync(conflict["plan_id"])
        self.assertEqual(target.read_text("utf-8"), "已有但不属于同步器的文件")

        fresh = service.preview(vault, [])
        service.sync(fresh["plan_id"])
        with self.assertRaisesRegex(ObsidianSyncError, "已失效"):
            service.sync(fresh["plan_id"])

    def test_unsafe_manifest_path_is_discarded_before_any_write(self):
        vault = self.make_vault()
        output = vault / EXPORT_FOLDER
        output.mkdir()
        (output / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "notes": {
                        "lecture-001": {
                            "path": "../../outside.md",
                            "content_hash": "not-a-real-hash",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        service = ObsidianSyncService()
        preview = service.preview(vault, [sample_note()])
        self.assertEqual(preview["counts"]["create"], 1)
        self.assertNotIn("..", Path(preview["items"][0]["path"]).parts)
        service.sync(preview["plan_id"])
        self.assertFalse((vault.parent / "outside.md").exists())


if __name__ == "__main__":
    unittest.main()
