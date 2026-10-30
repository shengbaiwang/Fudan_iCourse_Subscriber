from __future__ import annotations

import argparse
import atexit
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import threading
import webbrowser
import json
import os
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, StrictBool

from . import __version__
from .database import DatabaseManager
from .github_client import GitHubAPIError, GitHubClient
from .obsidian import ObsidianSyncError, ObsidianSyncService
from .provider_test import ProviderTestError, test_provider
from .provider_models import ModelDirectoryError, fetch_provider_models
from .talk_cloud import TalkCloudSync
from .runs import LocalRuns, LocalRunBusy
from .comparisons import ComparisonJobs
from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content
from src.ai.bucketer import assemble
from src.runtime.auto_check import PAUSE_VARIABLE, parse_auto_check_pauses
from .api_keychain import LocalAPIKeychain
from .talks import (
    MAX_AUDIO_BYTES,
    TALK_AUDIO_BRANCH,
    TALK_TRANSCRIBE_WORKFLOW,
    TalkBusyError,
    TalkJobs,
    TalkStore,
    TalkTranscribeError,
    TalkUploadQueue,
    decrypt_talk_blob,
    encrypt_talk_blob,
    talk_transcript_remote_path,
    validate_audio_upload,
    validate_talk_input,
)
from .state import (
    ObsidianSettings,
    RepositorySettings,
    RuntimeState,
)
from .notebook import Notebook, NoteConflict
from .note_library import NoteLibrary
from .shared_notes import SharedNotes
from src.runtime.config import DEFAULT_MODEL_PROVIDERS
from src.runtime.model_config import config_json, validate_model_config
from src.runtime.run_order import ordered_ids, ordered_lectures


ALLOWED_WORKFLOWS = {
    "check.yml",
    "single_run.yml",
    "export.yml",
    "delete_course.yml",
    "deploy-frontend.yml",
    "talk_transcribe.yml",
}


class ConfigureRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=200)
    branch: str = Field(default="data", min_length=1, max_length=200)
    token: SecretStr = SecretStr("")
    stuid: str = Field(min_length=1, max_length=100)
    uispsw: SecretStr = Field(min_length=1, max_length=500)
    remember: bool = False


class DispatchRequest(BaseModel):
    ref: str = "main"
    inputs: dict[str, str] = Field(default_factory=dict)


class ModelProviderRequest(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    models: list[str]
    enabled: bool = True
    generation: dict[str, Any] | None = None
    api_key: SecretStr | None = None


class ModelProvidersRequest(BaseModel):
    providers: list[ModelProviderRequest]


class ProviderModelsRequest(BaseModel):
    base_url: str
    api_key: SecretStr = Field(min_length=1)


class ProviderTestRequest(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    model: str
    api_key: SecretStr = Field(min_length=1)


class ObsidianPreviewRequest(BaseModel):
    vault_path: str = Field(min_length=1, max_length=4096)
    include_transcript: bool = False
    include_ocr: bool = False


class ObsidianSyncRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=128)


class SubscriptionRequest(BaseModel):
    course_ids: list[str] = Field(default_factory=list, max_length=500)


class AutoCheckRequest(BaseModel):
    course_id: str = Field(min_length=1, max_length=100)
    paused: bool


class CourseZoneRequest(BaseModel):
    course_id: str = Field(min_length=1, max_length=100)
    zone: str | None = Field(default=None, min_length=1, max_length=64)
    zones: list[str] | None = Field(default=None, max_length=100)
    operation: str = "replace"


class CourseSectionRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=40)
    parent_id: str | None = Field(default=None, min_length=1, max_length=64)


class CourseSectionsRequest(BaseModel):
    sections: list[CourseSectionRequest] = Field(max_length=100)
    revision: int = Field(ge=0)


class LectureNameRequest(BaseModel):
    sub_id: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=100)


class LectureHolidayRequest(BaseModel):
    sub_id: str = Field(min_length=1, max_length=100)
    holiday: StrictBool


class SummaryRerunRequest(BaseModel):
    provider: str = Field(default="", max_length=50)
    model: str = Field(default="", max_length=200)


class SummaryBatchRerunRequest(SummaryRerunRequest):
    sub_ids: list[str] = Field(default_factory=list)
    course_ids: list[str] = Field(default_factory=list, max_length=500)


class RunRequest(BaseModel):
    target: Literal["local", "github"] = "github"
    kind: Literal["process", "rerun", "titles"] = "process"
    course_ids: list[str] = Field(default_factory=list, max_length=500)
    sub_ids: list[str] = Field(default_factory=list)
    priority_course_ids: list[str] = Field(default_factory=list, max_length=500)
    lecture_order: Literal["oldest", "newest", "api"] = "newest"
    provider: str = Field(default="", max_length=50)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr = SecretStr("")
    use_official_transcript: bool = False


class RunPreferencesRequest(BaseModel):
    course_ids: list[str] = Field(max_length=500)
    lecture_order: Literal["oldest", "newest", "api"] = "newest"


class TalkCreateRequest(BaseModel):
    title: str = Field(default="", max_length=100)
    transcript: str = Field(min_length=1, max_length=200_000)


class TalkRenameRequest(BaseModel):
    name: str = Field(default="", max_length=100)


class TalkSummarizeRequest(BaseModel):
    provider: str = Field(default="", max_length=50)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr = SecretStr("")


class ComparisonModel(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)


class ComparisonRequest(BaseModel):
    models: list[ComparisonModel] = Field(min_length=1, max_length=20)
    api_key: SecretStr = SecretStr("")


class SourceNoteEditRequest(BaseModel):
    title: str = Field(max_length=200)
    body: str = Field(max_length=2_000_000)
    revision: str = Field(min_length=64, max_length=64)


class PersonalNoteRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=2_000_000)
    revision: int = Field(default=0, ge=0)
    starred: bool | None = None
    deleted: bool | None = None


class NoteFolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    parent_id: str | None = Field(default=None, max_length=32)


class NoteLocationRequest(BaseModel):
    folder_id: str = Field(default="", max_length=32)
    revision: int = Field(ge=0)
    candidates: list[str] = Field(default_factory=list, max_length=10000)


class NoteFolderMoveRequest(BaseModel):
    parent_id: str = Field(default="", max_length=32)
    expected_parent: str = Field(max_length=32)


class NoteFolderRestoreRequest(BaseModel):
    folders: list[dict[str, Any]] = Field(max_length=10000)
    locations: list[dict[str, Any]] = Field(max_length=100000)
    folder_candidates: list[dict[str, Any]] = Field(default_factory=list, max_length=100000)


class SharedNoteIdentity(BaseModel):
    note_id: str = Field(min_length=1, max_length=210)


class SharedNoteResolution(SharedNoteIdentity):
    title: str = Field(max_length=200)
    body: str = Field(max_length=2_000_000)
    raw_hash: str = Field(min_length=64, max_length=64)
    app_hash: str = Field(min_length=64, max_length=64)


class NoteFlagRequest(BaseModel):
    value: StrictBool


def create_app(
    state: RuntimeState | None = None,
    database: DatabaseManager | None = None,
    talk_store: TalkStore | None = None,
    talk_jobs: TalkJobs | None = None,
    talk_uploads: TalkUploadQueue | None = None,
    notebook: Notebook | None = None,
) -> FastAPI:
    runtime = state or RuntimeState()
    db = database or DatabaseManager()
    talks = talk_store or TalkStore()
    jobs = talk_jobs or TalkJobs(talks)
    local_runs = LocalRuns(db)
    comparisons = ComparisonJobs()
    personal_notes = notebook or Notebook(runtime.store.directory)
    note_library = NoteLibrary(personal_notes, db, talks, lambda: dict(runtime.lecture_names))
    shared_notes = SharedNotes(note_library)

    def _upload_client() -> GitHubClient:
        return client()

    uploads = talk_uploads or TalkUploadQueue(talks, _upload_client)
    cloud = TalkCloudSync(talks, uploads, _upload_client, lambda: runtime.credentials)

    @asynccontextmanager
    async def lifespan(_app):
        cloud.start()
        shared_notes.start()
        try:
            yield
        finally:
            shared_notes.close()
            comparisons.close()
            local_runs.close()
            cloud.close()

    static_dir = Path(__file__).with_name("static")
    app = FastAPI(
        title="丰饶之海",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.database = db
    app.state.talks = talks
    app.state.talk_jobs = jobs
    app.state.comparisons = comparisons
    app.state.talk_uploads = uploads
    app.state.talk_cloud = cloud
    app.state.local_runs = local_runs
    app.state.obsidian = ObsidianSyncService()
    app.state.shared_notes = shared_notes
    atexit.register(db.close)
    atexit.register(local_runs.close)
    if runtime.credentials:
        try:
            db.unlock_persistent(runtime.credentials)
            runtime.database_path = db.db_path if db.db_path.is_file() else None
            runtime.commit_sha = db.commit_sha
        except (OSError, ValueError) as exc:
            runtime.last_sync_error = str(exc)

    auto_update_lock = threading.Lock()
    auto_update_running = False

    @app.middleware("http")
    async def security_headers(request, call_next):
        desktop_token = os.environ.get("SEA_DESKTOP_TOKEN", "")
        if desktop_token and request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("X-Sea-Session", ""), desktop_token):
                return JSONResponse({"detail": "请从丰饶之海应用打开资料库"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def library_call(function, *args, **kwargs):
        try:
            return await run_in_threadpool(shared_notes.call, function, *args, **kwargs)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="笔记或文件夹已不存在，请刷新列表") from exc
        except NoteConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/local/note-library")
    async def library_notes(q: str = "", kind: str = "all", folder: str | None = None, starred: bool = False, trash: bool = False):
        try:
            result = await library_call(note_library.list, q, kind, folder, starred, trash)
        except HTTPException as exc:
            if not shared_notes.enabled or exc.status_code != 400:
                raise
            result = await run_in_threadpool(note_library.list, q, kind, folder, starred, trash)
        return {**result, 'shared': shared_notes.status()}

    @app.get("/api/local/note-library/{kind}/{source_id}")
    async def library_note(kind: str, source_id: str):
        return shared_notes.detail(await library_call(note_library.get, kind, source_id))

    @app.put("/api/local/note-library/{kind}/{source_id}/star")
    async def library_star(kind: str, source_id: str, payload: NoteFlagRequest):
        return await library_call(note_library.set_star, kind, source_id, payload.value)

    @app.put("/api/local/note-library/{kind}/{source_id}/content")
    async def library_save_edit(kind: str, source_id: str, payload: SourceNoteEditRequest):
        return await library_call(note_library.save_edit, kind, source_id, payload.title, payload.body, payload.revision)

    @app.get("/api/local/note-library/{kind}/{source_id}/versions")
    async def library_edit_versions(kind: str, source_id: str):
        return await library_call(note_library.edit_versions, kind, source_id)

    @app.put("/api/local/note-library/{kind}/{source_id}/folder")
    async def library_location(kind: str, source_id: str, payload: NoteLocationRequest):
        return await library_call(note_library.move_note, kind, source_id, payload.folder_id, payload.revision, payload.candidates)

    @app.post("/api/local/note-folders")
    async def create_note_folder(payload: NoteFolderRequest):
        return await library_call(note_library.save_folder, payload.name, None, payload.parent_id)

    @app.post("/api/local/note-folders/restore")
    async def restore_note_folder(payload: NoteFolderRestoreRequest):
        return await library_call(note_library.restore_folder, payload.model_dump())

    @app.put("/api/local/note-folders/{folder_id}")
    async def rename_note_folder(folder_id: str, payload: NoteFolderRequest):
        return await library_call(note_library.save_folder, payload.name, folder_id)

    @app.put("/api/local/note-folders/{folder_id}/move")
    async def move_note_folder(folder_id: str, payload: NoteFolderMoveRequest):
        return await library_call(note_library.move_folder, folder_id, payload.parent_id, payload.expected_parent)

    @app.delete("/api/local/note-folders/{folder_id}")
    async def delete_note_folder(folder_id: str):
        return await library_call(note_library.delete_folder, folder_id)

    @app.get("/api/local/notebook")
    async def list_notes(q: str = "", trash: bool = False):
        return await library_call(personal_notes.list, q, trash)

    @app.post("/api/local/notebook")
    async def create_note(payload: PersonalNoteRequest):
        return await library_call(shared_notes.save_personal, None, payload.title, payload.body)

    @app.get("/api/local/notebook/backup")
    async def backup_notes():
        return Response(await library_call(personal_notes.backup), media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=sea-notebook.zip"})

    @app.post("/api/local/notebook/restore")
    async def restore_notes(file: UploadFile = File(...)):
        from zipfile import BadZipFile
        data = await file.read(20_000_001)
        try:
            count = await library_call(personal_notes.restore, data)
        except (ValueError, BadZipFile, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"imported": count}

    @app.get("/api/local/notebook/{note_id}")
    async def read_note(note_id: str):
        try:
            return await library_call(personal_notes.get, note_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="笔记不存在") from exc

    @app.get("/api/local/notebook/{note_id}/versions")
    async def note_versions(note_id: str):
        try:
            return await library_call(personal_notes.versions, note_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="笔记不存在") from exc

    @app.put("/api/local/notebook/{note_id}")
    async def save_note(note_id: str, payload: PersonalNoteRequest):
        try:
            flags = {name: getattr(payload, name) for name in ("starred", "deleted") if getattr(payload, name) is not None}
            return await library_call(shared_notes.save_personal, note_id, payload.title, payload.body, payload.revision, **flags)
        except NoteConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="笔记不存在") from exc

    @app.get("/api/local/shared-notes/status")
    async def shared_status():
        try:
            await library_call(lambda: None)
        except HTTPException:
            pass
        return shared_notes.status()

    @app.get("/api/local/shared-notes/conflict")
    async def shared_conflict(note_id: str):
        return await library_call(shared_notes.conflict, note_id)

    @app.post("/api/local/shared-notes/resolve")
    async def resolve_shared_conflict(payload: SharedNoteResolution):
        if not shared_notes.enabled:
            raise HTTPException(status_code=400, detail="尚未连接共享笔记库")
        return await library_call(shared_notes.resolve, payload.note_id, payload.title, payload.body, payload.raw_hash, payload.app_hash)

    @app.post("/api/local/shared-notes/restore")
    async def restore_shared_note(payload: SharedNoteIdentity):
        if not shared_notes.enabled:
            raise HTTPException(status_code=400, detail="尚未连接共享笔记库")
        await library_call(shared_notes.restore, payload.note_id)
        return {"restored": True}

    @app.post("/api/local/shared-notes/trash")
    async def trash_shared_note(payload: SharedNoteIdentity):
        if not shared_notes.enabled:
            raise HTTPException(status_code=400, detail="尚未连接共享笔记库")
        return await library_call(shared_notes.delete, payload.note_id)

    @app.get("/api/local/shared-notes/asset")
    async def shared_asset(note_id: str, target: str):
        if not shared_notes.enabled:
            raise HTTPException(status_code=404, detail="尚未连接共享笔记库")
        path = await library_call(shared_notes.asset, note_id, target)
        import base64
        import mimetypes
        if path.stat().st_size > 20_000_000:
            raise HTTPException(status_code=400, detail="图片超过 20 MB")
        data = await run_in_threadpool(path.read_bytes)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return {"data_url": "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")}

    @app.get("/api/local/shared-notes/link")
    async def shared_link(note_id: str, target: str):
        if not shared_notes.enabled:
            raise HTTPException(status_code=404, detail="尚未连接共享笔记库")
        return await library_call(shared_notes.link, note_id, target)

    def client() -> GitHubClient:
        credentials = runtime.credentials
        if not credentials:
            raise HTTPException(status_code=409, detail="请先配置本地控制台")
        settings = runtime.settings
        return GitHubClient(settings.owner, settings.repo, credentials.token)

    def require_db() -> DatabaseManager:
        if not db.db_path.is_file():
            raise HTTPException(status_code=409, detail="本地资料库尚未准备好，请等待自动更新完成")
        return db

    def schedule_auto_update() -> None:
        """Start one non-blocking check; existing local data stays readable."""
        nonlocal auto_update_running
        credentials = runtime.credentials
        if not credentials:
            return
        with auto_update_lock:
            if auto_update_running:
                return
            auto_update_running = True
            runtime.auto_update_state = "checking"
        settings = runtime.settings

        def worker() -> None:
            nonlocal auto_update_running
            try:
                github = GitHubClient(settings.owner, settings.repo, credentials.token)
                result = db.sync(github, settings.branch, credentials)
                runtime.database_path = db.db_path
                runtime.commit_sha = db.commit_sha
                runtime.last_sync_error = None
                runtime.auto_update_state = "current" if result.get("unchanged") else "updated"
            except (GitHubAPIError, ValueError, OSError) as exc:
                runtime.last_sync_error = str(exc)
                runtime.auto_update_state = "failed"
            finally:
                runtime.last_auto_update_at = datetime.now(timezone.utc).isoformat()
                with auto_update_lock:
                    auto_update_running = False

        threading.Thread(target=worker, name="icourse-auto-update", daemon=True).start()

    @app.get("/api/local/status")
    async def status() -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": __version__,
            "configured": runtime.credentials is not None,
            "database_ready": db.db_path.is_file(),
            "repository": runtime.settings.as_dict(),
            "last_sync_error": runtime.last_sync_error,
            "keychain_available": runtime.keychain_available,
            "credentials_remembered": runtime.credentials_remembered,
            "update": {
                "state": runtime.auto_update_state,
                "last_checked_at": runtime.last_auto_update_at,
            },
        }
        if db.db_path.is_file():
            result["database"] = await run_in_threadpool(db.stats)
        return result

    @app.post("/api/local/configure")
    async def configure(payload: ConfigureRequest) -> dict[str, Any]:
        if any(job["status"] in {"queued", "in_progress"} for job in local_runs.list()):
            raise HTTPException(status_code=409, detail="请先结束本地任务，再切换连接")
        settings = RepositorySettings(
            owner=payload.owner.strip(),
            repo=payload.repo.strip(),
            branch=payload.branch.strip(),
        )
        try:
            token_source = await run_in_threadpool(
                runtime.configure,
                settings,
                token=payload.token.get_secret_value(),
                stuid=payload.stuid,
                uispsw=payload.uispsw.get_secret_value(),
                remember=payload.remember,
            )
            user = await run_in_threadpool(client().authenticated_user)
        except (ValueError, GitHubAPIError, OSError) as exc:
            runtime.credentials = None
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            if not db.db_path.is_file() and runtime.credentials:
                await run_in_threadpool(db.unlock_persistent, runtime.credentials)
                runtime.database_path = db.db_path if db.db_path.is_file() else None
                runtime.commit_sha = db.commit_sha
        except (OSError, ValueError) as exc:
            # A stale local library must never block a fresh online sync.
            runtime.last_sync_error = str(exc)
        schedule_auto_update()
        return {
            "ok": True,
            "token_source": token_source,
            "github_user": user,
            "credentials_remembered": runtime.credentials_remembered,
        }

    @app.post("/api/local/credentials/forget")
    async def forget_credentials() -> dict[str, bool]:
        if any(job["status"] in {"queued", "in_progress"} for job in local_runs.list()):
            raise HTTPException(status_code=409, detail="请先结束本地任务，再退出连接")
        try:
            await run_in_threadpool(runtime.forget_remembered_credentials)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    def _provider_response(
        document: dict, secret_names: set[str], source: str
    ) -> dict[str, Any]:
        providers = []
        for item in document["providers"]:
            providers.append(
                {
                    "name": item["name"],
                    "base_url": item["default_base_url"],
                    "api_key_env": item["api_key_env"],
                    "models": item["models"],
                    "enabled": item["enabled"],
                    **({"generation": item["generation"]} if "generation" in item else {}),
                    "api_key_configured": item["api_key_env"] in secret_names,
                }
            )
        return {"version": document["version"], "source": source, "providers": providers}

    @app.get("/api/local/model-providers")
    async def get_model_providers():
        gh = client()
        try:
            raw = await run_in_threadpool(
                gh.repository_variable, "MODEL_PROVIDERS_JSON"
            )
            if raw:
                document = validate_model_config(raw)
                runtime.model_store.save(document)
                source = "github-variable"
            else:
                document = runtime.model_store.load()
                if document:
                    source = "local-cache"
                else:
                    document = validate_model_config(DEFAULT_MODEL_PROVIDERS)
                    source = "defaults"
            secret_names = await run_in_threadpool(gh.repository_secret_names)
            return _provider_response(document, secret_names, source)
        except (GitHubAPIError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.put("/api/local/model-providers")
    async def save_model_providers(payload: ModelProvidersRequest):
        raw_providers = []
        secret_values: dict[str, str] = {}
        for provider in payload.providers:
            raw_providers.append(
                {
                    "name": provider.name,
                    "base_url": provider.base_url,
                    "api_key_env": provider.api_key_env,
                    "models": provider.models,
                    "enabled": provider.enabled,
                    **({"generation": provider.generation} if provider.generation is not None else {}),
                }
            )
            if provider.api_key is not None:
                value = provider.api_key.get_secret_value().strip()
                if value:
                    env_name = provider.api_key_env.strip().upper()
                    existing = secret_values.get(env_name)
                    if existing is not None and existing != value:
                        raise HTTPException(
                            status_code=400,
                            detail=f"同一个 Secret {env_name} 收到了两个不同的值",
                        )
                    secret_values[env_name] = value
        try:
            document = validate_model_config(
                {"version": 1, "providers": raw_providers}
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        gh = client()
        try:
            existing_names = await run_in_threadpool(gh.repository_secret_names)
            missing = [
                item["api_key_env"]
                for item in document["providers"]
                if item["enabled"]
                and item["api_key_env"] not in existing_names
                and item["api_key_env"] not in secret_values
            ]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail="这些已启用供应商尚未配置 API Key：" + ", ".join(missing),
                )
            # Secrets first: a failed key update must never publish a config
            # that points at a missing credential.
            for name, value in secret_values.items():
                await run_in_threadpool(gh.upsert_repository_secret, name, value)
            serialized = config_json(document)
            await run_in_threadpool(
                gh.upsert_repository_variable,
                "MODEL_PROVIDERS_JSON",
                serialized,
            )
            runtime.model_store.save(document)
            final_names = existing_names | set(secret_values)
            return _provider_response(document, final_names, "github-variable")
        except HTTPException:
            raise
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/local/model-providers/models")
    async def get_provider_models(payload: ProviderModelsRequest):
        try:
            return await run_in_threadpool(
                fetch_provider_models, payload.base_url, payload.api_key.get_secret_value()
            )
        except (ValueError, ModelDirectoryError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/model-providers/test")
    async def test_model_provider(payload: ProviderTestRequest):
        # Reuse the same strict validation as saved configurations.  The API
        # key is intentionally required because GitHub never reveals an
        # existing Secret value.
        try:
            api_key = payload.api_key.get_secret_value().strip()
            if not api_key:
                raise ValueError("请重新输入用于测试的 API Key")
            document = validate_model_config(
                {
                    "version": 1,
                    "providers": [
                        {
                            "name": payload.name,
                            "base_url": payload.base_url,
                            "api_key_env": payload.api_key_env,
                            "models": [payload.model],
                        }
                    ],
                }
            )
            provider = document["providers"][0]
            from src.ai.model_policy import is_openrouter
            if is_openrouter(provider):
                raw = await run_in_threadpool(client().repository_variable, "MODEL_PROVIDERS_JSON")
                saved = validate_model_config(raw or runtime.model_store.load() or DEFAULT_MODEL_PROVIDERS)
                if not any(row["name"] == provider["name"]
                           and row["default_base_url"] == provider["default_base_url"]
                           and payload.model in row["models"] for row in saved["providers"]):
                    raise ValueError("OpenRouter 只能测试已保存的模型，请先保存模型设置")
            return await run_in_threadpool(
                test_provider,
                provider["default_base_url"],
                provider["models"][0],
                api_key,
            )
        except (ValueError, ProviderTestError, GitHubAPIError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/sync")
    async def sync_database() -> dict[str, Any]:
        credentials = runtime.credentials
        if not credentials:
            raise HTTPException(status_code=409, detail="请先配置本地控制台")
        try:
            result = await run_in_threadpool(
                db.sync,
                client(),
                runtime.settings.branch,
                credentials,
            )
            runtime.database_path = db.db_path
            runtime.commit_sha = db.commit_sha
            runtime.last_sync_error = None
            runtime.auto_update_state = "current" if result.get("unchanged") else "updated"
            runtime.last_auto_update_at = datetime.now(timezone.utc).isoformat()
            return result
        except (GitHubAPIError, ValueError, OSError) as exc:
            runtime.last_sync_error = str(exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/local/database")
    async def download_database():
        require_db()
        return FileResponse(
            db.db_path,
            media_type="application/vnd.sqlite3",
            filename="icourse.db",
        )

    @app.get("/api/local/obsidian/settings")
    async def obsidian_settings() -> dict[str, str | bool]:
        """Expose only local, non-secret export preferences to the UI."""
        return runtime.obsidian_settings.as_dict()

    @app.post("/api/local/obsidian/preview")
    async def preview_obsidian_sync(
        payload: ObsidianPreviewRequest,
    ) -> dict[str, Any]:
        if shared_notes.enabled:
            requested = Path(payload.vault_path).expanduser().resolve()
            connected = shared_notes.root.resolve()
            if requested == connected or connected in requested.parents:
                raise HTTPException(status_code=409, detail='该目录已是共享笔记库，修改会自动更新，请从「笔记」打开，无需再次导出')
        local_db = require_db()
        service: ObsidianSyncService = app.state.obsidian
        try:
            notes = await run_in_threadpool(
                lambda: local_db.obsidian_notes(
                    include_transcript=payload.include_transcript,
                    include_ocr=payload.include_ocr,
                )
            )
            plan = await run_in_threadpool(
                lambda: service.preview(
                    payload.vault_path,
                    notes,
                    include_transcript=payload.include_transcript,
                    include_ocr=payload.include_ocr,
                )
            )
            try:
                await run_in_threadpool(
                    runtime.save_obsidian_settings,
                    ObsidianSettings(
                        vault_path=plan["vault_path"],
                        include_transcript=payload.include_transcript,
                        include_ocr=payload.include_ocr,
                    ),
                )
            except OSError:
                # The preview/sync itself is valid even when the optional
                # local convenience preference cannot be cached.
                pass
            return plan
        except ObsidianSyncError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/obsidian/sync")
    async def sync_obsidian_plan(payload: ObsidianSyncRequest) -> dict[str, Any]:
        service: ObsidianSyncService = app.state.obsidian
        try:
            return await run_in_threadpool(service.sync, payload.plan_id)
        except ObsidianSyncError as exc:
            # This commonly means that the preview was consumed/expired or a
            # file changed after preview; neither case should overwrite it.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/local/courses")
    async def courses():
        return await run_in_threadpool(require_db().courses)

    @app.get("/api/local/course-zones")
    async def course_zones() -> dict:
        return runtime.course_organization()

    @app.put("/api/local/course-zones")
    async def save_course_zone(payload: CourseZoneRequest) -> dict:
        if (payload.zone is None) == (payload.zones is None):
            raise HTTPException(status_code=400, detail="未知课程分区")
        try:
            await run_in_threadpool(
                runtime.save_course_memberships, payload.course_id,
                payload.zones if payload.zones is not None else [payload.zone], payload.operation
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.course_organization()

    @app.put("/api/local/course-sections")
    async def save_course_sections(payload: CourseSectionsRequest) -> dict:
        try:
            await run_in_threadpool(runtime.save_course_sections,
                                    [item.model_dump(exclude_none=True) for item in payload.sections], payload.revision)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.course_organization()

    @app.get("/api/local/lecture-names")
    async def lecture_names() -> dict[str, dict[str, str]]:
        return {"names": dict(runtime.lecture_names)}

    @app.get("/api/local/lecture-holidays")
    async def lecture_holidays() -> dict:
        return {"sub_ids": sorted(runtime.lecture_holidays)}

    @app.put("/api/local/lecture-holidays")
    async def save_lecture_holiday(payload: LectureHolidayRequest) -> dict:
        sub_id = payload.sub_id.strip()
        if await run_in_threadpool(require_db().lecture, sub_id) is None:
            raise HTTPException(status_code=404, detail="课次不存在")
        try:
            await run_in_threadpool(runtime.save_lecture_holiday, sub_id, payload.holiday)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"sub_ids": sorted(runtime.lecture_holidays)}

    @app.put("/api/local/lecture-names")
    async def save_lecture_name(payload: LectureNameRequest) -> dict[str, dict[str, str]]:
        try:
            await run_in_threadpool(
                runtime.save_lecture_name, payload.sub_id, payload.name
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"names": dict(runtime.lecture_names)}

    def normalized_subscription_ids(values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            course_id = raw.strip()
            if not course_id or len(course_id) > 100 or "," in course_id:
                raise HTTPException(status_code=400, detail="课程 ID 格式不正确")
            if course_id not in seen:
                seen.add(course_id)
                result.append(course_id)
        return result

    @app.get("/api/local/subscriptions")
    async def subscriptions() -> dict[str, Any]:
        # Secrets cannot be read back from GitHub.  Prefer the explicit local
        # save, otherwise use the non-secret snapshot embedded in the data DB.
        course_ids = list(runtime.subscription_ids)
        saved_selection = runtime.subscriptions_saved or bool(course_ids)
        source = "local-save" if saved_selection else "data-snapshot"
        if not saved_selection and db.db_path.is_file():
            course_ids = await run_in_threadpool(db.subscription_ids)
        pauses = runtime.auto_check_pauses
        try:
            pauses = parse_auto_check_pauses(await run_in_threadpool(client().repository_variable, PAUSE_VARIABLE))
            await run_in_threadpool(runtime.save_auto_check_pauses, pauses)
        except GitHubAPIError:
            # Keep offline reading usable; writes below require a fresh read.
            if pauses is None:
                pauses = await run_in_threadpool(db.auto_check_pauses) if db.db_path.is_file() else {}
        courses = (
            await run_in_threadpool(db.subscription_courses, course_ids)
            if db.db_path.is_file()
            else []
        )
        progress = await run_in_threadpool(db.subscription_check_status, pauses) if db.db_path.is_file() else {}
        for course in courses:
            cid = str(course["course_id"])
            course.update(progress.get(cid, {}))
            course["auto_check_paused"] = cid in pauses
        return {"course_ids": course_ids, "courses": courses, "source": source,
                "paused_course_ids": [cid for cid in course_ids if cid in pauses]}

    @app.put("/api/local/subscriptions/auto-check")
    async def save_auto_check(payload: AutoCheckRequest) -> dict[str, Any]:
        cid = normalized_subscription_ids([payload.course_id])[0]
        current = await subscriptions()
        if cid not in current["course_ids"]:
            raise HTTPException(status_code=409, detail="课程已不在订阅中，请刷新后重试")
        try:
            gh = client()
            pauses = parse_auto_check_pauses(await run_in_threadpool(gh.repository_variable, PAUSE_VARIABLE))
            if payload.paused:
                pauses.setdefault(cid, uuid.uuid4().hex)
            else:
                pauses.pop(cid, None)
            await run_in_threadpool(gh.upsert_repository_variable, PAUSE_VARIABLE, json.dumps(pauses))
            await run_in_threadpool(runtime.save_auto_check_pauses, pauses)
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return await subscriptions()

    @app.get("/api/local/subscription-catalog")
    async def subscription_catalog(q: str = "", term: str = "", limit: int = 20, page: int = 1):
        local_db = require_db()
        return {
            "terms": await run_in_threadpool(local_db.subscription_terms),
            **await run_in_threadpool(
                local_db.subscription_catalog, q, term, limit, page
            ),
        }

    @app.put("/api/local/subscriptions")
    async def save_subscriptions(payload: SubscriptionRequest) -> dict[str, Any]:
        course_ids = normalized_subscription_ids(payload.course_ids)
        try:
            await run_in_threadpool(
                client().upsert_repository_secret,
                "COURSE_IDS",
                ",".join(course_ids),
            )
            await run_in_threadpool(runtime.save_subscription_ids, course_ids)
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return await subscriptions()

    @app.get("/api/local/courses/{course_id}/lectures")
    async def lectures(course_id: str):
        return await run_in_threadpool(require_db().lectures, course_id)

    @app.get("/api/local/lectures/{sub_id}")
    async def lecture(sub_id: str):
        item = await run_in_threadpool(require_db().lecture, sub_id)
        if item is None:
            raise HTTPException(status_code=404, detail="课次不存在")
        return item

    @app.post("/api/local/lectures/{sub_id}/rerun-summary")
    async def rerun_lecture_summary(sub_id: str, payload: SummaryRerunRequest):
        return await dispatch_summary_rerun(
            [sub_id], [], payload.provider, payload.model
        )

    async def dispatch_summary_rerun(
        requested_sub_ids: list[str],
        requested_course_ids: list[str],
        provider_value: str,
        model_value: str,
    ) -> dict[str, Any]:
        """Validate the complete selection; the worker processes it in batches."""
        local_db = require_db()
        sub_ids: list[str] = []
        seen: set[str] = set()

        def include(sub_id: str) -> None:
            normalized = str(sub_id).strip()
            if len(normalized) > 100 or "," in normalized:
                raise HTTPException(status_code=400, detail="课次 ID 格式不正确")
            if normalized and normalized not in seen:
                seen.add(normalized)
                sub_ids.append(normalized)

        for sub_id in requested_sub_ids:
            include(sub_id)
        for course_id in requested_course_ids:
            normalized_course_id = str(course_id).strip()
            if not normalized_course_id or len(normalized_course_id) > 100:
                raise HTTPException(status_code=400, detail="课程 ID 格式不正确")
            course_sub_ids = await run_in_threadpool(
                local_db.rerunnable_lecture_ids, normalized_course_id
            )
            for sub_id in course_sub_ids:
                include(sub_id)

        if not sub_ids:
            raise HTTPException(status_code=400, detail="请选择至少一个有转录的课次或课程")

        for sub_id in sub_ids:
            item = await run_in_threadpool(local_db.lecture, sub_id)
            if item is None:
                raise HTTPException(status_code=404, detail=f"课次不存在：{sub_id}")
            if not str(item.get("transcript") or "").strip():
                raise HTTPException(
                    status_code=409,
                    detail=f"课次没有可用转录，暂时不能只重新生成笔记：{sub_id}",
                )
        try:
            gh = client()
            raw = await run_in_threadpool(
                gh.repository_variable, "MODEL_PROVIDERS_JSON"
            )
            document = validate_model_config(raw or DEFAULT_MODEL_PROVIDERS)
            secret_names = await run_in_threadpool(gh.repository_secret_names)
            if not any(row["enabled"] and row["api_key_env"] in secret_names for row in document["providers"]):
                raise HTTPException(status_code=400, detail="请先在模型与 API 中保存已启用模型的 API Key")
            await run_in_threadpool(
                gh.dispatch_workflow,
                "single_run.yml",
                ref="main",
                inputs={
                    "course_ids": "",
                    "resummarize_sub_ids": ",".join(sub_ids),
                    "use_official_transcript": "false",
                },
            )
            return {
                "ok": True,
                "model_policy": "saved-order",
                "sub_ids": sub_ids,
                "sub_id": sub_ids[0] if len(sub_ids) == 1 else None,
                "count": len(sub_ids),
            }
        except HTTPException:
            raise
        except (GitHubAPIError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/local/summary-reruns")
    async def rerun_summary_batch(payload: SummaryBatchRerunRequest):
        """Regenerate selected lectures, or all eligible lectures in courses."""
        return await dispatch_summary_rerun(
            payload.sub_ids, payload.course_ids, payload.provider, payload.model
        )

    @app.get("/api/local/run-capabilities")
    async def run_capabilities():
        result = await run_in_threadpool(local_runs.capabilities)
        return {**result, "github": True}

    def local_generation_keys(document, payload):
        import os
        enabled = [p for p in document["providers"] if p["enabled"]]
        entered = payload.api_key.get_secret_value().strip()
        binding = next((p for p in enabled if p["name"] == payload.provider), None) if payload.provider else enabled[0]
        if entered and (binding is None or payload.model and payload.model not in binding["models"]):
            raise ValueError("API Key 对应的供应商或模型未添加")
        keys = {}
        for provider in enabled:
            key = entered if entered and provider is binding else LocalAPIKeychain().load(provider)
            key = key or os.environ.get(provider["api_key_env"], "")
            if key:
                env = provider["api_key_env"]
                if env in keys and keys[env] != key:
                    raise ValueError(f"{env} 对应不同供应商的本机 Key，请在模型设置中使用不同的 Secret 名称")
                keys[env] = key
        if not keys:
            raise ValueError("请在本机钥匙串保存 API Key，或输入首个供应商的 Key（仅本次运行使用）")
        return keys

    @app.get("/api/local/runs")
    async def run_jobs():
        return {"jobs": local_runs.list()}

    @app.get("/api/local/comparisons/{source}/{source_id}")
    async def comparison_status(source: Literal["lecture", "talk"], source_id: str):
        return {"jobs": comparisons.list(source, source_id)}

    @app.post("/api/local/comparisons/{source}/{source_id}")
    async def generate_comparisons(source: Literal["lecture", "talk"], source_id: str,
                                   payload: ComparisonRequest):
        credentials = _require_talk_credentials()
        if source == "talk":
            item = _talk_or_404(source_id)
            if jobs.is_running(source_id) or item["status"] in {"uploading", "dispatching", "transcribing", "summarizing"}:
                raise HTTPException(status_code=409, detail="请等待讲座处理完成后再生成对照笔记")
        else:
            item = await run_in_threadpool(require_db().lecture, source_id)
            if not item:
                raise HTTPException(status_code=404, detail="课次不存在")
        transcript = str(item.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=409, detail="没有可用转写文本")
        try:
            raw = await run_in_threadpool(client().repository_variable, "MODEL_PROVIDERS_JSON")
            document = validate_model_config(raw or DEFAULT_MODEL_PROVIDERS)
            enabled = {p["name"]: p for p in document["providers"] if p["enabled"]}
            providers, seen = [], set()
            for selection in payload.models:
                identity = (selection.provider, selection.model)
                if identity in seen:
                    continue
                seen.add(identity)
                provider = enabled.get(selection.provider)
                if not provider or selection.model not in provider["models"]:
                    raise ValueError(f"模型未启用或未保存：{selection.provider}/{selection.model}")
                key = payload.api_key.get_secret_value().strip() if selection.provider == payload.models[0].provider else ""
                key = key or os.environ.get(provider["api_key_env"], "") or LocalAPIKeychain().load(provider)
                if not key:
                    raise ValueError(f"请先保存 {selection.provider} 的本机 API Key，或在本轮输入该供应商的 Key")
                providers.append({"name": provider["name"], "base_url": provider["default_base_url"],
                                  "models": [selection.model], "api_key": key,
                                  **({"generation": provider["generation"]} if "generation" in provider else {})})
            pages = [p for p in item.get("ppt_pages", []) if p.get("ocr_status", "done") == "done"] if source == "lecture" else []
            material, _mode = assemble(transcript, None, pages)
            title = str(item.get("course_title") or item.get("custom_title") or item.get("title") or "未命名讲座")
            def save(body, ai_title, model):
                if runtime.credentials is not credentials:
                    raise ValueError("登录资料已变化，请重新生成")
                if source == "talk":
                    if talks.get_talk(source_id) is None:
                        raise ValueError("讲座已删除")
                    talks.save_summary(source_id, body, ai_title, model)
                else:
                    db.save_comparison(source_id, body, ai_title, model, credentials)
            launched = comparisons.submit(source, source_id, providers, SYSTEM_PROMPT,
                                          summary_user_content(title, material), save)
            return {"jobs": launched, "model_policy": "selected-models"}
        except (ValueError, OSError, GitHubAPIError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/local/runs/{job_id}/cancel")
    async def cancel_local_run(job_id: str):
        client()
        try:
            return await run_in_threadpool(local_runs.cancel, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/local/run-preferences")
    async def run_preferences():
        state = await subscriptions()
        try:
            order = await run_in_threadpool(client().repository_variable, "LECTURE_ORDER")
        except GitHubAPIError:
            order = "api"
        return {"course_ids": state["course_ids"], "lecture_order": order or "api"}

    @app.put("/api/local/run-preferences")
    async def save_run_preferences(payload: RunPreferencesRequest):
        # Reordering must never silently unsubscribe a course.
        current = await subscriptions()
        try:
            values = ordered_ids(payload.course_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if set(values) != set(current["course_ids"]):
            raise HTTPException(status_code=409, detail="订阅已变化，请重新打开运行页面后再保存顺序")
        await save_subscriptions(SubscriptionRequest(course_ids=values))
        try:
            await run_in_threadpool(client().upsert_repository_variable, "LECTURE_ORDER", payload.lecture_order)
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=f"课程顺序已保存，课次顺序未保存：{exc}") from exc
        return {"course_ids": values, "lecture_order": payload.lecture_order}

    @app.post("/api/local/runs")
    async def start_run(payload: RunRequest):
        gh = client()
        try:
            courses = ordered_ids(payload.course_ids)
            selected = ordered_ids(payload.sub_ids)
            priority = ordered_ids(payload.priority_course_ids)
            rank = {cid: i for i, cid in enumerate(priority)}
            courses.sort(key=lambda cid: rank.get(cid, len(rank)))
            if payload.kind == "rerun":
                local_db = require_db()
                for cid in courses:
                    selected.extend(await run_in_threadpool(local_db.rerunnable_lecture_ids, cid))
                selected = list(dict.fromkeys(selected))
                rows = []
                for sid in selected:
                    row = await run_in_threadpool(local_db.lecture, sid)
                    if not row or not str(row.get("transcript") or "").strip():
                        raise ValueError(f"课次没有可用转录：{sid}")
                    rows.append(row)
                rows = ordered_lectures(rows, payload.lecture_order)
                rows.sort(key=lambda row: rank.get(str(row["course_id"]), len(rank)))
                selected = [str(row["sub_id"]) for row in rows]
                if not selected:
                    raise ValueError("重新生成笔记需选择至少一个有转录的课次")
            elif not courses:
                raise ValueError("请选择课程，或输入要运行的课程 ID")
            try:
                raw = await run_in_threadpool(gh.repository_variable, "MODEL_PROVIDERS_JSON")
            except GitHubAPIError:
                if payload.target != "local":
                    raise
                raw = runtime.model_store.load()
            document = validate_model_config(raw or DEFAULT_MODEL_PROVIDERS)
            if payload.target == "github":
                secrets = await run_in_threadpool(gh.repository_secret_names)
                if not any(p["enabled"] and p["api_key_env"] in secrets for p in document["providers"]):
                    raise ValueError("请先在模型与 API 中保存已启用模型的 GitHub Key")
                inputs = {"course_ids": ",".join(courses), "lecture_order": payload.lecture_order,
                          "use_official_transcript": str(payload.use_official_transcript).lower()}
                if payload.kind == "rerun":
                    inputs["resummarize_sub_ids"] = ",".join(selected)
                if payload.kind == "titles":
                    inputs["backfill_titles"] = "true"
                await run_in_threadpool(gh.dispatch_workflow, "single_run.yml", ref="main", inputs=inputs)
                return {"ok": True, "target": "github", "count": len(selected) if payload.kind == "rerun" else len(courses)}
            capabilities = await run_in_threadpool(local_runs.capabilities)
            if not capabilities["summary_ready"] or payload.kind == "process" and not capabilities["process_ready"]:
                missing = capabilities["missing"] + (["ASR 权重"] if payload.kind == "process" and not capabilities.get("models_ready") else [])
                raise ValueError("本地运行环境未就绪，请安装 requirements-course.txt 及 ASR 权重；缺少：" + ", ".join(missing))
            api_keys = local_generation_keys(document, payload)
            request = {"kind": payload.kind, "course_ids": courses, "sub_ids": selected,
                       "lecture_order": payload.lecture_order, "use_official_transcript": payload.use_official_transcript}
            job = local_runs.start(request, runtime.credentials, document, api_keys)
            return {"ok": True, "target": "local", "job": job}
        except LocalRunBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Talks: local-only uploaded/pasted lectures ──────────────────────
    # Independent from the course library (which mirrors the data branch);
    # talk audio/transcripts never leave this machine except as encrypted
    # blobs on the talk-audio branch, or as transcript text inside the
    # summary request itself.

    def _talk_or_404(talk_id: str) -> dict[str, Any]:
        item = talks.get_talk(talk_id.strip())
        if item is None:
            raise HTTPException(status_code=404, detail="讲座不存在")
        return item

    def _require_talk_credentials() -> Any:
        credentials = runtime.credentials
        if not credentials:
            raise HTTPException(status_code=409, detail="请先配置本地控制台")
        if not credentials.stuid or not credentials.uispsw:
            raise HTTPException(status_code=409, detail="学号和 UIS 密码不能为空")
        return credentials

    @app.get("/api/local/talks")
    async def list_talks():
        rows = await run_in_threadpool(talks.list_talks)
        return [{**row, **uploads.describe(row["id"])} for row in rows]

    @app.post("/api/local/talks")
    async def create_talk(payload: TalkCreateRequest):
        try:
            title, transcript = validate_talk_input(
                payload.title, payload.transcript
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await run_in_threadpool(
            talks.create_talk, title, transcript, "paste"
        )

    @app.post("/api/local/talks/upload")
    async def upload_talk_audio(
        file: UploadFile = File(...),
        title: str = Form(default=""),
        upload_token: str = Form(default="", max_length=100),
    ):
        """Stage a recording and return immediately; chunks go up in back.

        Long-term fix for ``GitHub API 0: The write operation timed out``:
        the old synchronous handler held the HTTP request open across every
        chunk PUT, so any slow uplink killed the whole upload with no way
        forward.  Now the request only validates + encrypts + stages the
        ciphertext locally (<1 s for typical sizes) and a background queue
        pushes ≤4 MiB chunks with per-chunk retry; the browser polls
        progress and the workflow is dispatched only after the manifest
        lands, so the runner never sees a half upload.
        """
        credentials = _require_talk_credentials()
        try:
            raw = await file.read(MAX_AUDIO_BYTES + 1)
        finally:
            await file.close()
        try:
            ext, _ = validate_audio_upload(file.filename or "", raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        clean_title = (str(title or "").strip() or Path(file.filename or "").stem)[:100]
        audio_bytes = len(raw)
        try:
            encrypted = await run_in_threadpool(
                encrypt_talk_blob, raw, credentials.stuid, credentials.uispsw
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"音频加密失败：{exc}"
            ) from exc
        # Release the plaintext before any disk/network I/O.
        del raw
        try:
            return await run_in_threadpool(
                uploads.enqueue_blob, encrypted, ext, clean_title, audio_bytes, upload_token,
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail="本地保存录音失败，请检查磁盘空间") from exc

    @app.get("/api/local/talks/{talk_id}/upload-progress")
    async def talk_upload_progress(talk_id: str):
        """Progress snapshot for the upload poll loop (no secrets)."""
        item = _talk_or_404(talk_id)
        progress = await run_in_threadpool(talks.upload_progress, item["id"])
        return {
            "talk_id": item["id"],
            "status": item.get("status"),
            "error": item.get("error") or "",
            "error_stage": item.get("error_stage") or "",
            **progress,
        }

    @app.post("/api/local/talks/{talk_id}/retry-upload")
    async def retry_talk_upload(talk_id: str):
        """One-click resume: re-queue the staged blob, skipping done chunks."""
        _require_talk_credentials()
        item = _talk_or_404(talk_id)
        if item.get("source") != "upload":
            raise HTTPException(status_code=409, detail="该讲座不是录音上传")
        if item["status"] == "ready":
            raise HTTPException(status_code=409, detail="笔记已生成，无需恢复上传")
        if uploads.is_active(item["id"]):
            if item["status"] == "paused":
                raise HTTPException(status_code=409, detail="正在暂停当前分片，请稍后继续")
            return item
        if item["status"] in {"transcribing", "summarizing"}:
            raise HTTPException(status_code=409, detail="云端正在处理，请等待处理结果")
        # Missing local staging is allowed when the cloud still has the manifest.
        # The worker checks both sources and can resume at ASR or summary.
        await run_in_threadpool(talks.bump_upload_attempts, item["id"])
        uploads.submit(item["id"])
        refreshed = _talk_or_404(talk_id)
        progress = await run_in_threadpool(
            talks.upload_progress, item["id"]
        )
        return {**refreshed, "upload_progress": progress}

    @app.post("/api/local/talks/{talk_id}/pause-upload")
    async def pause_talk_upload(talk_id: str):
        _talk_or_404(talk_id)
        if not uploads.pause(talk_id):
            raise HTTPException(status_code=409, detail="只能暂停排队或上传中的录音")
        return _talk_or_404(talk_id)

    @app.post("/api/local/talks/{talk_id}/sync-transcript")
    async def sync_talk_transcript(talk_id: str):
        """Pull the cloud transcript (+ auto summary) into the local library.

        Mirrors the iCourse flow: ASR and summary both run in Actions, this
        endpoint only pulls the finished blob.  A transcript-only blob parks
        at ``transcribed``; a full blob lands at ``ready`` with a version
        row, so auto notes and manual notes share one display path.
        """
        _require_talk_credentials()
        _talk_or_404(talk_id)
        try:
            item = await run_in_threadpool(cloud.sync, talk_id)
        except (GitHubAPIError, TalkTranscribeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": item["status"] == "ready", **item,
                "detail": "转写与笔记已同步" if item["status"] == "ready" else "已刷新处理进度"}

    @app.get("/api/local/talks/{talk_id}")
    async def get_talk(talk_id: str):
        return {**_talk_or_404(talk_id), **uploads.describe(talk_id)}

    @app.delete("/api/local/talks/{talk_id}")
    async def delete_talk(talk_id: str):
        item = _talk_or_404(talk_id)
        if uploads.is_active(item["id"]) or jobs.is_running(item["id"]) or comparisons.is_running("talk", item["id"]) or item["status"] in {"transcribing", "summarizing", "dispatching"}:
            raise HTTPException(status_code=409, detail="讲座正在处理，请等待结束后再删除")
        deleted = await run_in_threadpool(
            talks.delete_talk, talk_id.strip()
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="讲座不存在")
        # Drop staged ciphertext (if any) + remote shard dir; best effort.
        await run_in_threadpool(uploads.drop_staging, item["id"])
        remote_dir = str(item.get("remote_audio_path") or "").strip()
        if remote_dir:
            try:
                await run_in_threadpool(
                    client().delete_branch_dir,
                    TALK_AUDIO_BRANCH, remote_dir,
                    f"talk audio {item['id']} deleted",
                )
                await run_in_threadpool(
                    client().delete_branch_file,
                    TALK_AUDIO_BRANCH, talk_transcript_remote_path(item["id"]),
                    f"talk checkpoint {item['id']} deleted",
                )
            except GitHubAPIError:
                pass
        return {"ok": True}

    @app.put("/api/local/talks/{talk_id}/title")
    async def rename_talk(talk_id: str, payload: TalkRenameRequest):
        item = await run_in_threadpool(
            talks.rename_talk, talk_id.strip(), payload.name
        )
        if item is None:
            raise HTTPException(status_code=404, detail="讲座不存在")
        return item

    @app.post("/api/local/talks/{talk_id}/summarize")
    async def summarize_talk(talk_id: str, payload: TalkSummarizeRequest):
        """Generate using the complete saved chain and locally available keys."""
        item = _talk_or_404(talk_id)
        if jobs.is_running(item["id"]) or comparisons.is_running("talk", item["id"]) or item["status"] in {"transcribing", "dispatching", "summarizing", "uploading"}:
            raise HTTPException(
                status_code=409, detail="该讲座正在生成中，请稍后刷新查看"
            )
        transcript = str(item.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(
                status_code=409, detail="该讲座没有可用转写文本"
            )
        try:
            gh = client()
            raw = await run_in_threadpool(
                gh.repository_variable, "MODEL_PROVIDERS_JSON"
            )
            document = validate_model_config(raw or DEFAULT_MODEL_PROVIDERS)
            keys = local_generation_keys(document, payload)
            providers = [{"name": p["name"], "base_url": p["default_base_url"],
                          "models": p["models"], "api_key": keys[p["api_key_env"]],
                          **({"generation": p["generation"]} if "generation" in p else {})}
                         for p in document["providers"] if p["enabled"] and p["api_key_env"] in keys]
        except HTTPException:
            raise
        except (GitHubAPIError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        try:
            launched = await run_in_threadpool(
                jobs.submit,
                item["id"],
                str(item.get("custom_title") or ""),
                transcript,
                providers,
            )
        except TalkBusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if not launched:
            raise HTTPException(
                status_code=409, detail="该讲座正在生成中，请稍后刷新查看"
            )
        return {
            "ok": True,
            "talk_id": item["id"],
            "model_policy": "saved-order",
        }

    @app.get("/api/local/search")
    async def search(
        q: str,
        course_id: str = "",
        domains: str = "",
        page: int = 1,
        page_size: int = 20,
    ):
        domain_list = [d.strip() for d in domains.split(",") if d.strip()] or None
        return await run_in_threadpool(
            require_db().search,
            q,
            course_id=course_id,
            domains=domain_list,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/local/workflows")
    async def workflows():
        try:
            return await run_in_threadpool(client().workflow_runs)
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/local/workflows/{workflow}/dispatch")
    async def dispatch(workflow: str, payload: DispatchRequest):
        if workflow not in ALLOWED_WORKFLOWS:
            raise HTTPException(status_code=400, detail="不允许触发该 workflow")
        course_ids, sub_ids = [], []
        if workflow == "delete_course.yml":
            course_ids = normalized_subscription_ids(payload.inputs.get("course_ids", "").split(","))
            raw_sub_ids = payload.inputs.get("sub_ids", "").strip()
            sub_ids = normalized_subscription_ids(raw_sub_ids.split(",")) if raw_sub_ids else []
            if any(job["status"] in {"queued", "in_progress"} and
                   set(job["course_ids"]).intersection(course_ids)
                   for job in app.state.local_runs.list()):
                raise HTTPException(status_code=409, detail="课程正在本机处理，请先停止任务再删除")
        try:
            await run_in_threadpool(
                client().dispatch_workflow,
                workflow,
                ref=payload.ref,
                inputs=payload.inputs,
            )
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        local_deleted = workflow == "delete_course.yml" and db.db_path.is_file()
        if local_deleted:
            try:
                await run_in_threadpool(db.delete_course_data, course_ids, sub_ids, runtime.credentials)
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise HTTPException(status_code=500, detail="远端删除已提交，本机资料清理失败；请稍后重试") from exc
        return {"ok": True, "local_deleted": local_deleted}

    # ``create_app`` is called once by the local console process.  Start its
    # one background freshness check here, rather than from the status route
    # that the browser polls while the check is running.
    schedule_auto_update()
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="local-static")
    return app


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def main() -> None:
    parser = argparse.ArgumentParser(description="启动按需本地 Web 管理界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非回环地址（会暴露敏感管理接口）",
    )
    args = parser.parse_args()
    if not _loopback(args.host) and not args.allow_remote:
        parser.error("出于安全考虑，非本机地址必须显式使用 --allow-remote")

    import uvicorn

    if os.environ.get("SEA_DESKTOP_TOKEN"):
        import socket
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        application = create_app()
        print("SEA_READY " + json.dumps({"url": f"http://127.0.0.1:{port}"}), flush=True)
        uvicorn.Server(uvicorn.Config(application, log_level="warning")).run(sockets=[listener])
        listener.close()
        return

    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"iCourse 本地控制台：{url}")
    print("已启用本地加密资料库；明文数据库仅在当前进程临时解锁。")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
