from __future__ import annotations

import argparse
import atexit
from datetime import datetime, timezone
import ipaddress
import threading
import webbrowser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from . import __version__
from .database import DatabaseManager
from .github_client import GitHubAPIError, GitHubClient
from .obsidian import ObsidianSyncError, ObsidianSyncService
from .provider_test import ProviderTestError, test_provider
from .state import (
    ObsidianSettings,
    RepositorySettings,
    RuntimeState,
    normalize_course_zone,
)
from src.runtime.config import DEFAULT_MODEL_PROVIDERS
from src.runtime.model_config import config_json, validate_model_config


ALLOWED_WORKFLOWS = {
    "check.yml",
    "single_run.yml",
    "export.yml",
    "delete_course.yml",
    "deploy-frontend.yml",
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
    api_key: SecretStr | None = None


class ModelProvidersRequest(BaseModel):
    providers: list[ModelProviderRequest]


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


class CourseZoneRequest(BaseModel):
    course_id: str = Field(min_length=1, max_length=100)
    zone: str = Field(min_length=1, max_length=32)


class LectureNameRequest(BaseModel):
    sub_id: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=100)


class SummaryRerunRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)


class SummaryBatchRerunRequest(SummaryRerunRequest):
    sub_ids: list[str] = Field(default_factory=list, max_length=20)
    course_ids: list[str] = Field(default_factory=list, max_length=20)


def create_app(
    state: RuntimeState | None = None,
    database: DatabaseManager | None = None,
) -> FastAPI:
    runtime = state or RuntimeState()
    db = database or DatabaseManager()
    static_dir = Path(__file__).with_name("static")
    app = FastAPI(
        title="iCourse Subscriber Local Console",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime
    app.state.database = db
    app.state.obsidian = ObsidianSyncService()
    atexit.register(db.close)
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
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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
            return await run_in_threadpool(
                test_provider,
                provider["default_base_url"],
                provider["models"][0],
                api_key,
            )
        except (ValueError, ProviderTestError) as exc:
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
    async def course_zones() -> dict[str, dict[str, str]]:
        return {"zones": dict(runtime.course_zones)}

    @app.put("/api/local/course-zones")
    async def save_course_zone(payload: CourseZoneRequest) -> dict[str, dict[str, str]]:
        if normalize_course_zone(payload.zone) is None:
            raise HTTPException(status_code=400, detail="未知课程分区")
        try:
            await run_in_threadpool(
                runtime.save_course_zone, payload.course_id, payload.zone
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"zones": dict(runtime.course_zones)}

    @app.get("/api/local/lecture-names")
    async def lecture_names() -> dict[str, dict[str, str]]:
        return {"names": dict(runtime.lecture_names)}

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
        source = "local-save" if course_ids else "data-snapshot"
        if not course_ids and db.db_path.is_file():
            course_ids = await run_in_threadpool(db.subscription_ids)
        courses = (
            await run_in_threadpool(db.subscription_courses, course_ids)
            if db.db_path.is_file()
            else []
        )
        return {"course_ids": course_ids, "courses": courses, "source": source}

    @app.get("/api/local/subscription-catalog")
    async def subscription_catalog(q: str = "", term: str = "", limit: int = 100):
        local_db = require_db()
        return {
            "terms": await run_in_threadpool(local_db.subscription_terms),
            "courses": await run_in_threadpool(
                local_db.subscription_catalog, q, term, limit
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
        courses = (
            await run_in_threadpool(db.subscription_courses, course_ids)
            if db.db_path.is_file()
            else []
        )
        return {"course_ids": course_ids, "courses": courses, "source": "local-save"}

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
        """Validate selected lectures and dispatch one bounded rerun workflow."""
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
        if len(sub_ids) > 20:
            raise HTTPException(
                status_code=400,
                detail=f"已选择 {len(sub_ids)} 个可重跑课次；一次最多 20 个，请分批提交",
            )

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
            provider_name = provider_value.strip()
            model_name = model_value.strip()
            provider = next(
                (row for row in document["providers"] if row["name"] == provider_name),
                None,
            )
            if provider is None or not provider["enabled"]:
                raise HTTPException(status_code=400, detail="所选模型供应商不存在或未启用")
            if model_name not in provider["models"]:
                raise HTTPException(status_code=400, detail="所选模型不在当前配置中")
            secret_names = await run_in_threadpool(gh.repository_secret_names)
            if provider["api_key_env"] not in secret_names:
                raise HTTPException(
                    status_code=400,
                    detail="所选模型尚未配置 API Key，请先在模型管理中保存",
                )
            await run_in_threadpool(
                gh.dispatch_workflow,
                "single_run.yml",
                ref="main",
                inputs={
                    "course_ids": "",
                    "resummarize_sub_ids": ",".join(sub_ids),
                    "summary_provider": provider_name,
                    "summary_model": model_name,
                    "use_official_transcript": "false",
                },
            )
            return {
                "ok": True,
                "provider": provider_name,
                "model": model_name,
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

    @app.get("/api/local/search")
    async def search(
        q: str,
        course_id: str = "",
        domains: str = "",
        page: int = 1,
        page_size: int = 50,
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
        try:
            await run_in_threadpool(
                client().dispatch_workflow,
                workflow,
                ref=payload.ref,
                inputs=payload.inputs,
            )
        except GitHubAPIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

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

    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"iCourse 本地控制台：{url}")
    print("已启用本地加密资料库；明文数据库仅在当前进程临时解锁。")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
