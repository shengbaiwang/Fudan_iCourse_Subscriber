"""Safe, on-demand Markdown synchronization into an Obsidian vault.

The service deliberately never talks to a cloud service and never deletes
files.  It writes only generated Markdown below ``iCourse 笔记`` after the
user has previewed a plan.  A small manifest records hashes of our previous
output so hand-edited notes are reported as conflicts instead of overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


EXPORT_FOLDER = "iCourse 笔记"
MANIFEST_NAME = ".icourse-sync.json"
MANIFEST_VERSION = 1
MAX_PLANS = 12


class ObsidianSyncError(ValueError):
    """A safe-to-show explanation for a local sync failure."""


@dataclass(frozen=True)
class SyncEntry:
    sub_id: str
    title: str
    relative_path: str
    content: str
    desired_hash: str
    status: str
    reason: str = ""
    current_hash: str = ""

    def public(self) -> dict[str, str]:
        result = {
            "status": self.status,
            "path": self.relative_path,
            "title": self.title,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class SyncPlan:
    plan_id: str
    vault: Path
    output_root: Path
    entries: tuple[SyncEntry, ...]
    manifest: dict[str, Any]


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_component(value: Any, fallback: str, limit: int = 72) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:limit].rstrip(" .") or fallback


def _display_text(value: Any, fallback: str = "") -> str:
    text = " ".join(str(value or "").replace("\r", " ").splitlines()).strip()
    return text or fallback


def _yaml_string(value: Any) -> str:
    """JSON strings are valid YAML scalars and avoid frontmatter injection."""
    return json.dumps(_display_text(value), ensure_ascii=False)


def _valid_relative_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


class ObsidianSyncService:
    """Build preview plans and apply them only after explicit confirmation."""

    def __init__(self) -> None:
        self._plans: dict[str, SyncPlan] = {}
        self._lock = threading.RLock()

    def preview(
        self,
        vault_path: str | Path,
        notes: list[dict[str, Any]],
        *,
        include_transcript: bool = False,
        include_ocr: bool = False,
    ) -> dict[str, Any]:
        vault = self._validate_vault(vault_path)
        output_root = vault / EXPORT_FOLDER
        self._ensure_safe_target(vault, output_root)
        if output_root.exists() and not output_root.is_dir():
            raise ObsidianSyncError("Vault 内的 iCourse 笔记同名路径不是文件夹")
        manifest = self._load_manifest(vault, output_root)
        entries = tuple(
            self._entry_for_note(
                vault,
                output_root,
                manifest,
                note,
                include_transcript=include_transcript,
                include_ocr=include_ocr,
            )
            for note in notes
        )
        plan = SyncPlan(
            plan_id=uuid.uuid4().hex,
            vault=vault,
            output_root=output_root,
            entries=entries,
            manifest=manifest,
        )
        with self._lock:
            self._plans[plan.plan_id] = plan
            while len(self._plans) > MAX_PLANS:
                self._plans.pop(next(iter(self._plans)))
        return self._public_plan(plan)

    def sync(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            plan = self._plans.pop(plan_id, None)
        if plan is None:
            raise ObsidianSyncError("同步预览已失效，请先重新预览")

        self._ensure_safe_target(plan.vault, plan.output_root)
        if plan.output_root.exists() and not plan.output_root.is_dir():
            raise ObsidianSyncError("Vault 内的 iCourse 笔记同名路径不是文件夹")
        try:
            plan.output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ObsidianSyncError("无法创建 Vault 中的 iCourse 笔记目录") from exc
        self._ensure_safe_target(plan.vault, plan.output_root)
        manifest = self._load_manifest(plan.vault, plan.output_root)
        existing_notes = dict(manifest["notes"])
        counts = _empty_sync_counts()

        for planned in plan.entries:
            entry = self._recheck_entry(plan.vault, plan.output_root, manifest, planned)
            count_key = {"create": "created", "update": "updated"}.get(
                entry.status, entry.status
            )
            counts[count_key] += 1
            if entry.status not in {"create", "update", "unchanged"}:
                continue
            target = self._target(plan.vault, plan.output_root, entry.relative_path)
            if entry.status in {"create", "update"}:
                self._atomic_write(plan.vault, target, entry.content)
            existing_notes[entry.sub_id] = {
                "path": entry.relative_path,
                "content_hash": entry.desired_hash,
            }

        self._write_manifest(
            plan.vault,
            plan.output_root,
            {"version": MANIFEST_VERSION, "notes": existing_notes},
        )
        return {
            "counts": counts,
            "output_folder": str(plan.output_root),
        }

    def _validate_vault(self, value: str | Path) -> Path:
        raw = Path(str(value).strip()).expanduser()
        if not raw.is_absolute():
            raise ObsidianSyncError("Obsidian Vault 必须填写绝对路径")
        try:
            vault = raw.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ObsidianSyncError("找不到指定的 Obsidian Vault") from exc
        if not vault.is_dir():
            raise ObsidianSyncError("指定路径不是文件夹")
        if not (vault / ".obsidian").is_dir():
            raise ObsidianSyncError(
                "该文件夹不是已初始化的 Obsidian Vault（缺少 .obsidian）"
            )
        return vault

    def _ensure_inside(self, vault: Path, target: Path) -> None:
        if not _inside(vault, target):
            raise ObsidianSyncError("同步目录不能通过符号链接离开所选 Vault")

    def _ensure_safe_target(self, vault: Path, target: Path) -> None:
        """Reject every symlink component, including ones that stay in the Vault."""
        try:
            parts = target.relative_to(vault).parts
        except ValueError as exc:
            raise ObsidianSyncError("同步目标不在所选 Vault 内") from exc
        current = vault
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ObsidianSyncError("同步目录包含符号链接，已拒绝写入")
        self._ensure_inside(vault, target)

    def _target(self, vault: Path, output_root: Path, relative_path: str) -> Path:
        valid = _valid_relative_path(relative_path)
        if valid is None:
            raise ObsidianSyncError("同步清单包含不安全的文件路径")
        target = output_root.joinpath(*PurePosixPath(valid).parts)
        self._ensure_safe_target(vault, target)
        if target.is_symlink():
            raise ObsidianSyncError("目标笔记是符号链接，已拒绝写入")
        return target

    def _load_manifest(self, vault: Path, output_root: Path) -> dict[str, Any]:
        path = self._target(vault, output_root, MANIFEST_NAME)
        if not path.exists():
            return {"version": MANIFEST_VERSION, "notes": {}}
        if path.is_symlink():
            raise ObsidianSyncError("同步清单是符号链接，已拒绝读取")
        try:
            document = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ObsidianSyncError("同步清单无法读取；为保护手写笔记已停止同步") from exc
        if (
            not isinstance(document, dict)
            or document.get("version") != MANIFEST_VERSION
            or not isinstance(document.get("notes"), dict)
        ):
            raise ObsidianSyncError("同步清单格式不兼容；为保护手写笔记已停止同步")
        notes: dict[str, dict[str, str]] = {}
        for sub_id, item in document["notes"].items():
            if not isinstance(item, dict):
                continue
            relative = _valid_relative_path(item.get("path"))
            content_hash = item.get("content_hash")
            if relative and isinstance(content_hash, str):
                notes[str(sub_id)] = {
                    "path": relative,
                    "content_hash": content_hash,
                }
        return {"version": MANIFEST_VERSION, "notes": notes}

    def _entry_for_note(
        self,
        vault: Path,
        output_root: Path,
        manifest: dict[str, Any],
        note: dict[str, Any],
        *,
        include_transcript: bool,
        include_ocr: bool,
    ) -> SyncEntry:
        sub_id = _display_text(note.get("sub_id"))
        if not sub_id:
            raise ObsidianSyncError("数据库中存在没有课次 ID 的笔记，无法安全同步")
        title = _display_text(note.get("sub_title"), sub_id)
        previous = manifest["notes"].get(sub_id)
        relative_path = (
            previous["path"]
            if previous and _valid_relative_path(previous.get("path"))
            else self._new_relative_path(note)
        )
        content = self._render_note(
            note,
            include_transcript=include_transcript,
            include_ocr=include_ocr,
        )
        desired_hash = _hash_text(content)
        return self._classify(
            vault,
            output_root,
            sub_id,
            title,
            relative_path,
            content,
            desired_hash,
            previous,
        )

    def _new_relative_path(self, note: dict[str, Any]) -> str:
        course_id = _safe_component(note.get("course_id"), "course")
        course = _safe_component(note.get("course_title"), "未命名课程")
        course_folder = f"{course} [{course_id}]"
        date = _safe_component(note.get("date"), "未标日期", limit=20)
        lecture = _safe_component(note.get("sub_title"), "未命名课次")
        sub_id = _display_text(note.get("sub_id"), "lecture")
        suffix = hashlib.sha256(sub_id.encode("utf-8")).hexdigest()[:8]
        filename = f"{date} {lecture} [{suffix}].md"
        return (PurePosixPath(course_folder) / filename).as_posix()

    def _classify(
        self,
        vault: Path,
        output_root: Path,
        sub_id: str,
        title: str,
        relative_path: str,
        content: str,
        desired_hash: str,
        previous: dict[str, str] | None,
    ) -> SyncEntry:
        target = self._target(vault, output_root, relative_path)
        if not target.exists():
            return SyncEntry(
                sub_id, title, relative_path, content, desired_hash, "create"
            )
        if not target.is_file():
            return SyncEntry(
                sub_id,
                title,
                relative_path,
                content,
                desired_hash,
                "conflict",
                "目标路径不是普通 Markdown 文件",
            )
        try:
            current = target.read_text("utf-8")
        except (OSError, UnicodeError):
            return SyncEntry(
                sub_id,
                title,
                relative_path,
                content,
                desired_hash,
                "conflict",
                "现有文件无法按 UTF-8 读取",
            )
        current_hash = _hash_text(current)
        if current_hash == desired_hash:
            status = "unchanged"
            reason = ""
        elif (
            previous
            and previous.get("path") == relative_path
            and previous.get("content_hash") == current_hash
        ):
            status = "update"
            reason = ""
        else:
            status = "conflict"
            reason = "检测到手写修改或未知同名文件，未覆盖"
        return SyncEntry(
            sub_id,
            title,
            relative_path,
            content,
            desired_hash,
            status,
            reason,
            current_hash,
        )

    def _recheck_entry(
        self,
        vault: Path,
        output_root: Path,
        manifest: dict[str, Any],
        planned: SyncEntry,
    ) -> SyncEntry:
        return self._classify(
            vault,
            output_root,
            planned.sub_id,
            planned.title,
            planned.relative_path,
            planned.content,
            planned.desired_hash,
            manifest["notes"].get(planned.sub_id),
        )

    def _render_note(
        self,
        note: dict[str, Any],
        *,
        include_transcript: bool,
        include_ocr: bool,
    ) -> str:
        title = _display_text(note.get("sub_title"), note.get("sub_id"))
        course_title = _display_text(note.get("course_title"))
        frontmatter = [
            "---",
            "icourse_generated: true",
            f"icourse_sub_id: {_yaml_string(note.get('sub_id'))}",
            f"course_id: {_yaml_string(note.get('course_id'))}",
            f"course: {_yaml_string(course_title)}",
            f"teacher: {_yaml_string(note.get('teacher'))}",
            f"lecture: {_yaml_string(title)}",
            f"date: {_yaml_string(note.get('date'))}",
            f"processed_at: {_yaml_string(note.get('processed_at'))}",
            f"summary_model: {_yaml_string(note.get('summary_model'))}",
            'tags: ["iCourse", "课程笔记"]',
            "---",
            "",
        ]
        body = [f"# {title}", "", _normal_text(note.get("summary"))]
        if include_transcript and _normal_text(note.get("transcript")):
            body.extend(["", "## 转录", "", _normal_text(note.get("transcript"))])
        if include_ocr:
            pages = note.get("ocr_pages") or []
            if pages:
                body.extend(["", "## PPT OCR"])
                for page in pages:
                    try:
                        seconds = int(page.get("created_sec") or 0)
                    except (TypeError, ValueError):
                        seconds = 0
                    minute, second = divmod(seconds, 60)
                    heading = f"### {minute:02d}:{second:02d} · 第 {page.get('page_num', '?')} 页"
                    body.extend(["", heading, "", _normal_text(page.get("text"))])
        return "\n".join(frontmatter + body).rstrip() + "\n"

    def _atomic_write(self, vault: Path, target: Path, content: str) -> None:
        self._ensure_safe_target(vault, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_target(vault, target)
        if target.is_symlink():
            raise ObsidianSyncError("目标笔记是符号链接，已拒绝写入")
        # The temporary file stays in target.parent, so os.replace is atomic.
        descriptor, temporary = tempfile.mkstemp(
            prefix=".icourse-", suffix=".tmp", dir=target.parent
        )
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, target)
        except OSError as exc:
            raise ObsidianSyncError(f"无法写入 {target.name}: {exc}") from exc
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_manifest(
        self,
        vault: Path,
        output_root: Path,
        manifest: dict[str, Any],
    ) -> None:
        path = self._target(vault, output_root, MANIFEST_NAME)
        self._atomic_write(
            vault,
            path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

    def _public_plan(self, plan: SyncPlan) -> dict[str, Any]:
        counts = _empty_counts()
        for entry in plan.entries:
            counts[entry.status] += 1
        return {
            "plan_id": plan.plan_id,
            "vault_path": str(plan.vault),
            "vault_name": plan.vault.name,
            "output_folder": str(plan.output_root),
            "counts": counts,
            "items": [entry.public() for entry in plan.entries],
        }


def _normal_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _empty_counts() -> dict[str, int]:
    return {"create": 0, "update": 0, "unchanged": 0, "conflict": 0}


def _empty_sync_counts() -> dict[str, int]:
    return {"created": 0, "updated": 0, "unchanged": 0, "conflict": 0}
