from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .keychain import CredentialStore, MacOSKeychainStore
from src.runtime.model_config import validate_model_config


APP_DIR_NAME = "fudan-icourse-subscriber"
COURSE_ZONES = frozenset({"organize", "study", "reference", "archive"})
COURSE_ZONE_ALIASES = {
    "organize": "organize",
    "study": "study",
    "reference": "reference",
    "archive": "archive",
    "整理区": "organize",
    "学习区": "study",
    "查阅区": "reference",
    "归档区": "archive",
}


def normalize_course_zone(value: object) -> str | None:
    """Normalize both stable IDs and labels saved by early local builds."""
    return COURSE_ZONE_ALIASES.get(str(value or "").strip())


def default_config_dir() -> Path:
    override = os.environ.get("ICOURSE_WEB_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys_platform() == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME


def default_cache_dir() -> Path:
    override = os.environ.get("ICOURSE_WEB_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys_platform() == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / APP_DIR_NAME


def sys_platform() -> str:
    import sys

    return sys.platform


@dataclass(frozen=True)
class RepositorySettings:
    owner: str = "shengbaiwang"
    repo: str = "Fudan_iCourse_Subscriber"
    branch: str = "data"

    @classmethod
    def from_dict(cls, value: dict) -> "RepositorySettings":
        return cls(
            owner=str(value.get("owner") or cls.owner).strip(),
            repo=str(value.get("repo") or cls.repo).strip(),
            branch=str(value.get("branch") or cls.branch).strip(),
        )

    def as_dict(self) -> dict[str, str]:
        return {"owner": self.owner, "repo": self.repo, "branch": self.branch}


@dataclass(frozen=True)
class RuntimeCredentials:
    token: str
    stuid: str
    uispsw: str


@dataclass(frozen=True)
class ObsidianSettings:
    """Non-secret local preferences for the on-demand Vault export."""

    vault_path: str = ""
    include_transcript: bool = False
    include_ocr: bool = False

    @classmethod
    def from_dict(cls, value: dict) -> "ObsidianSettings":
        return cls(
            vault_path=str(value.get("vault_path") or "").strip(),
            include_transcript=value.get("include_transcript") is True,
            include_ocr=value.get("include_ocr") is True,
        )

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "vault_path": self.vault_path,
            "include_transcript": self.include_transcript,
            "include_ocr": self.include_ocr,
        }


class SettingsStore:
    """Persist non-secret repository settings only."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "settings.json"

    def load(self) -> RepositorySettings:
        try:
            return RepositorySettings.from_dict(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return RepositorySettings()

    def save(self, settings: RepositorySettings) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(settings.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


class ModelConfigStore:
    """Cache the non-secret provider/model document for offline startup."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "model-providers.json"

    def load(self) -> dict | None:
        try:
            return validate_model_config(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, document: dict) -> None:
        normalized = validate_model_config(document)
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


class ObsidianSettingsStore:
    """Persist only the selected Vault path and content switches, never notes."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "obsidian.json"

    def load(self) -> ObsidianSettings:
        try:
            return ObsidianSettings.from_dict(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            return ObsidianSettings()

    def save(self, settings: ObsidianSettings) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(settings.as_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


class SubscriptionSettingsStore:
    """Persist the last locally saved COURSE_IDS selection, never credentials."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "subscriptions.json"

    def load(self) -> list[str]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            values = raw.get("course_ids") if isinstance(raw, dict) else []
            return _normalize_course_ids(values if isinstance(values, list) else [])
        except (OSError, ValueError, TypeError):
            return []

    def save(self, course_ids: list[str]) -> None:
        values = _normalize_course_ids(course_ids)
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"course_ids": values}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


class CourseZoneStore:
    """Persist local-only course organization without touching subscriptions."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "course-zones.json"

    def load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("zones"), dict):
            # Briefly-used pre-release format: {"zones": {course_id: zone}}.
            raw = raw["zones"]
        if not isinstance(raw, dict):
            return {}
        zones: dict[str, str] = {}
        for raw_id, raw_zone in raw.items():
            course_id = str(raw_id).strip()
            zone = normalize_course_zone(raw_zone)
            if course_id and len(course_id) <= 100 and zone:
                zones[course_id] = zone
        return zones

    def save(self, zones: dict[str, str]) -> None:
        clean = {
            str(course_id).strip(): normalized_zone
            for course_id, zone in zones.items()
            for normalized_zone in [normalize_course_zone(zone)]
            if str(course_id).strip()
            and len(str(course_id).strip()) <= 100
            and normalized_zone is not None
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


class LectureNameStore:
    """Persist local-only custom note names (empty name = back to auto naming)."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or default_config_dir()
        self.path = self.directory / "lecture-names.json"

    def load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        names: dict[str, str] = {}
        for raw_id, raw_name in raw.items():
            sub_id = str(raw_id).strip()
            name = str(raw_name).strip()
            if sub_id and len(sub_id) <= 100 and name and len(name) <= 100:
                names[sub_id] = name
        return names

    def save(self, names: dict[str, str]) -> None:
        clean = {
            str(sub_id).strip(): str(name).strip()
            for sub_id, name in names.items()
            if str(sub_id).strip()
            and len(str(sub_id).strip()) <= 100
            and str(name).strip()
            and len(str(name).strip()) <= 100
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)


def _normalize_course_ids(values: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values[:500]:
        course_id = str(raw).strip()
        if not course_id or len(course_id) > 100 or "," in course_id:
            continue
        if course_id not in seen:
            seen.add(course_id)
            normalized.append(course_id)
    return normalized


def token_from_gh_cli() -> str:
    """Read an existing GitHub CLI token without ever logging it."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


class RuntimeState:
    def __init__(
        self,
        store: SettingsStore | None = None,
        model_store: ModelConfigStore | None = None,
        obsidian_store: ObsidianSettingsStore | None = None,
        subscription_store: SubscriptionSettingsStore | None = None,
        course_zone_store: CourseZoneStore | None = None,
        lecture_name_store: LectureNameStore | None = None,
        credential_store: CredentialStore | None = None,
    ):
        self.store = store or SettingsStore()
        self.model_store = model_store or ModelConfigStore(self.store.directory)
        self.obsidian_store = obsidian_store or ObsidianSettingsStore(
            self.store.directory
        )
        self.subscription_store = subscription_store or SubscriptionSettingsStore(
            self.store.directory
        )
        self.course_zone_store = course_zone_store or CourseZoneStore(self.store.directory)
        self.lecture_name_store = lecture_name_store or LectureNameStore(
            self.store.directory
        )
        self.credential_store = credential_store or MacOSKeychainStore()
        self.settings = self.store.load()
        self.obsidian_settings = self.obsidian_store.load()
        self.subscription_ids = self.subscription_store.load()
        self.course_zones = self.course_zone_store.load()
        self.lecture_names = self.lecture_name_store.load()
        self.credentials = self.credential_store.load(self.settings)
        self.credentials_remembered = self.credentials is not None
        self.database_path: Path | None = None
        self.commit_sha: str | None = None
        self.last_sync_error: str | None = None
        self.auto_update_state = "idle"
        self.last_auto_update_at: str | None = None
        self.lock = threading.RLock()

    def configure(
        self,
        settings: RepositorySettings,
        *,
        token: str,
        stuid: str,
        uispsw: str,
        remember: bool = False,
    ) -> str:
        resolved_token = token.strip() or token_from_gh_cli()
        if not resolved_token:
            raise ValueError("请提供 GitHub Token，或先运行 gh auth login")
        if not stuid.strip() or not uispsw:
            raise ValueError("学号和 UIS 密码不能为空")
        with self.lock:
            self.settings = settings
            credentials = RuntimeCredentials(
                token=resolved_token,
                stuid=stuid.strip(),
                uispsw=uispsw,
            )
            self.credentials = credentials
            self.credentials_remembered = False
            self.store.save(settings)
            if remember:
                self.credential_store.save(settings, credentials)
                self.credentials_remembered = True
        return "gh-cli" if not token.strip() else "form"

    @property
    def keychain_available(self) -> bool:
        return self.credential_store.available

    def forget_remembered_credentials(self) -> None:
        with self.lock:
            self.credential_store.delete(self.settings)
            self.credentials = None
            self.credentials_remembered = False

    def save_obsidian_settings(self, settings: ObsidianSettings) -> None:
        with self.lock:
            self.obsidian_settings = settings
            self.obsidian_store.save(settings)

    def save_subscription_ids(self, course_ids: list[str]) -> None:
        values = _normalize_course_ids(course_ids)
        with self.lock:
            self.subscription_ids = values
            self.subscription_store.save(values)

    def save_course_zone(self, course_id: str, zone: str) -> None:
        normalized_id = course_id.strip()
        normalized_zone = normalize_course_zone(zone)
        if not normalized_id or len(normalized_id) > 100 or normalized_zone is None:
            raise ValueError("课程分区无效")
        with self.lock:
            self.course_zones = {**self.course_zones, normalized_id: normalized_zone}
            self.course_zone_store.save(self.course_zones)

    def save_lecture_name(self, sub_id: str, name: str) -> None:
        """Set or clear a custom note name; an empty name restores auto naming."""
        normalized_id = sub_id.strip()
        normalized_name = name.strip()
        if not normalized_id or len(normalized_id) > 100 or len(normalized_name) > 100:
            raise ValueError("笔记名称无效")
        with self.lock:
            names = {**self.lecture_names}
            if normalized_name:
                names[normalized_id] = normalized_name
            else:
                names.pop(normalized_id, None)
            self.lecture_names = names
            self.lecture_name_store.save(names)
