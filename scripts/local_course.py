"""Local, resumable course processing; never dispatches Actions.

Default: inspect only. Use prepare for ASR/OCR, summarize for API notes,
and export for local Markdown. Credentials come from the existing Keychain.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
COURSE_ID = "4048"
TITLE = "当代中国经济与社会专题研究"
TEACHER = "林超超"
TERM = "2023-2024-2"
DEPT = "历史学系"
RUN_DIR = ROOT / "work" / "local-course-4048"


def configure_course(path: Path | None) -> None:
    """An explicit profile bounds the job to exactly one course."""
    global COURSE_ID, TITLE, TEACHER, TERM, DEPT, RUN_DIR
    if path is None:
        return
    profile = json.loads(path.read_text())
    keys = ("course_id", "title", "teacher", "term", "dept")
    if not all(isinstance(profile.get(key), str) and profile[key].strip() for key in keys):
        raise ValueError("课程配置需含 course_id、title、teacher、term、dept 五个非空字符串")
    if not re.fullmatch(r"[0-9]{1,12}", profile["course_id"]):
        raise ValueError("course_id 必须为数字，不能包含目录路径")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{4}-[12]", profile["term"]):
        raise ValueError("term 格式应为 2023-2024-2")
    COURSE_ID, TITLE, TEACHER, TERM, DEPT = (profile[key].strip() for key in keys)
    RUN_DIR = ROOT / "work" / f"local-course-{COURSE_ID}"


def safe_error(exc: BaseException) -> str:
    # Exceptions from HTTP/ffmpeg can contain signed URLs or cookies.
    value = re.sub(r"https?://[^\s\"']+", "[URL]", str(exc))
    value = re.sub(r"(?im)(Cookie|Authorization):[^\r\n]*", r"\1: [redacted]", value)
    return f"{type(exc).__name__}: {value[:400]}"


def write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".next")
    with open(temporary, "wb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def verify_duration(expected: float, actual: float) -> None:
    """Fail closed when duration is unknown or exceeds the allowed drift."""
    if not all(math.isfinite(n) and n > 0 for n in (expected, actual)):
        raise ValueError("无法确认完整录播时长，禁止保存转录")
    tolerance = max(3.0, expected * 0.005)
    if abs(expected - actual) > tolerance:
        raise ValueError(f"音频时长不符：预期 {expected:.2f}s，解码 {actual:.2f}s")


def lecture_list(detail: dict) -> list[dict]:
    if detail.get("title") != TITLE or detail.get("teacher") != TEACHER:
        raise ValueError("课程 ID、名称或教师不符，停止处理")
    # Preserve different recording IDs, even when the timetable label matches.
    unique = {str(item["sub_id"]): item for item in detail["lectures"]}
    return sorted(unique.values(), key=lambda item: (item["date"], str(item["sub_id"])))


def setup_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    import imageio_ffmpeg
    binary = Path(imageio_ffmpeg.get_ffmpeg_exe())
    directory = RUN_DIR / "bin"
    directory.mkdir(exist_ok=True)
    link = directory / "ffmpeg"
    if not link.exists():
        link.symlink_to(binary)
    os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")


def credentials():
    from local_web.state import SettingsStore, RuntimeCredentials
    from local_web.keychain import MacOSKeychainStore
    result = MacOSKeychainStore().load(SettingsStore().load())
    if result:
        return result
    stuid = os.environ.get("StuId") or input("学号（仅本机使用）：").strip()
    password = os.environ.get("UISPsw") or getpass.getpass("UIS 密码（不保存）：")
    if not stuid or not password:
        raise ValueError("学号及 UIS 密码不能为空")
    return RuntimeCredentials(token="", stuid=stuid, uispsw=password)


def login(creds):
    from src.api.icourse import ICourseClient
    from src.api.webvpn import WebVPNSession
    from scripts.local_http import BrowserHTTPSAdapter
    for attempt in range(2):
        vpn = WebVPNSession()
        vpn.session.mount("https://webvpn.fudan.edu.cn/", BrowserHTTPSAdapter())
        vpn.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        # Rejected credentials are never retried. Only a successful login
        # followed by a cold session / incomplete CAS redirect gets a retry.
        try:
            vpn.login(creds.stuid, creds.uispsw)
            try:
                vpn.authenticate_icourse(creds.stuid, creds.uispsw)
            except RuntimeError as exc:
                transient = ("WebVPN session cold" in str(exc)
                             or "Failed to extract lck from CAS redirect chain" in str(exc))
                if transient and attempt == 0:
                    print("WebVPN/CAS 会话未生效，5 秒后仅重试一次。", flush=True)
                    vpn.session.close()
                    time.sleep(5)
                    continue
                raise
            return ICourseClient(vpn)
        except BaseException:
            vpn.session.close()
            raise


@contextmanager
def running_job():
    """Hold a course lock through login, download, ASR and OCR."""
    with open(RUN_DIR / "job.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("这门课已有本机任务运行") from None
        marker = RUN_DIR / "active-job.json"
        write_private(marker, json.dumps({"pid": os.getpid(), "course_id": COURSE_ID,
                                         "stage": "prepare", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode())
        try:
            yield
        finally:
            marker.unlink(missing_ok=True)


@contextmanager
def local_database(creds):
    from src.data.crypto_box import decrypt, derive_new_password, encrypt, is_sqlite
    from src.data.database import Database
    password = derive_new_password(creds.stuid, creds.uispsw)
    snapshot = RUN_DIR / "icourse.db.enc"
    with open(RUN_DIR / "run.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("这门课已有本机任务运行") from None
        with tempfile.TemporaryDirectory(prefix=f"icourse-{COURSE_ID}-") as scratch:
            scratch = Path(scratch)
            os.chmod(scratch, 0o700)
            path = scratch / "icourse.db"
            if snapshot.exists():
                plaintext = decrypt(snapshot.read_bytes(), password)
                if not is_sqlite(plaintext):
                    raise ValueError("本地课程检查点无法解锁")
                path.write_bytes(plaintext)
            db = Database(str(path))
            os.chmod(path, 0o600)
            db.conn.execute("PRAGMA synchronous=FULL")

            def checkpoint():
                backup_path = scratch / "checkpoint.db"
                with db._lock:
                    with closing(sqlite3.connect(backup_path)) as backup:
                        db.conn.backup(backup)
                    # Async summaries share this checkpoint. Keep backup,
                    # encryption and atomic replacement under the same RLock.
                    write_private(snapshot, encrypt(backup_path.read_bytes(), password))
                    backup_path.unlink()

            try:
                yield db, checkpoint
            finally:
                db.conn.close()


@contextmanager
def read_snapshot(creds):
    """Read the last atomic checkpoint while preparation is running."""
    from src.data.crypto_box import decrypt, derive_new_password, is_sqlite
    from src.data.database import Database
    snapshot = RUN_DIR / "icourse.db.enc"
    plaintext = decrypt(snapshot.read_bytes(), derive_new_password(creds.stuid, creds.uispsw))
    if not is_sqlite(plaintext):
        raise ValueError("本地课程检查点无法解锁")
    with tempfile.TemporaryDirectory(prefix=f"icourse-{COURSE_ID}-read-") as scratch:
        path = Path(scratch) / "icourse.db"
        path.write_bytes(plaintext)
        os.chmod(path, 0o600)
        db = Database(str(path))
        try:
            yield db
        finally:
            db.conn.close()


def meta(db, sub_id: str, stage: str):
    value = db.read_meta(f"local:{sub_id}:{stage}")
    return json.loads(value) if value else None


def save_meta(db, sub_id: str, stage: str, value):
    db.write_meta(f"local:{sub_id}:{stage}", json.dumps(value, ensure_ascii=False))


def inspect(client):
    detail = client.get_course_detail(COURSE_ID)
    lectures = lecture_list(detail)
    manifest = {"course_id": COURSE_ID, "title": TITLE, "teacher": TEACHER,
                "term": TERM, "dept": DEPT, "lectures": lectures,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    write_private(RUN_DIR / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return manifest


def select_summary_provider(provider_name: str, model_name: str | None):
    from local_web.state import ModelConfigStore
    from src.runtime import config
    from src.runtime.model_config import runtime_providers, select_runtime_model
    document = ModelConfigStore().load()
    declared = document or {"version": 1, "providers": config.MODEL_PROVIDERS}
    if provider_name == "default":
        provider_name = runtime_providers(declared)[0]["name"]
    provider = next((p for p in declared["providers"] if p["name"] == provider_name), None)
    if not provider:
        raise ValueError("指定服务商不在本机模型配置中")
    providers = select_runtime_model(declared, provider_name, model_name or provider["models"][0])
    if len(providers) != 1:
        raise ValueError("摘要只使用指定的一个服务商")
    return providers[0]


def build_summarizer(provider_name: str, model_name: str | None):
    from src.runtime import config
    from src.ai.summarizer import Summarizer
    from local_web.api_keychain import LocalAPIKeychain
    from src.runtime.model_config import normalize_base_url
    provider = select_summary_provider(provider_name, model_name)
    # Never fall back to a different host sharing the same key variable.
    key_name = provider["api_key_env"]
    override = os.environ.get(provider.get("base_url_env", ""), "").strip()
    if override and normalize_base_url(override) != provider["default_base_url"]:
        raise ValueError("摘要地址与本机保存配置不符；请在模型配置中明确设置地址")
    if not os.environ.get(key_name):
        remembered = LocalAPIKeychain().load(provider)
        if remembered:
            os.environ[key_name] = remembered
            print(f"已从 macOS 钥匙串读取 {provider['name']} API Key。", flush=True)
    if not os.environ.get(key_name):
        if not sys.stdin.isatty():
            raise ValueError(f"本机缺少 {key_name}；请在终端运行摘要入口并隐藏输入 Key")
        os.environ[key_name] = getpass.getpass(f"{provider['name']} API Key（仅本次进程使用）：")
    if not os.environ.get(key_name):
        raise ValueError("API Key 为空；转录/OCR 仍可用 prepare 单独运行")
    config.MODEL_PROVIDERS = [provider]
    return Summarizer()


def summarize(db, checkpoint, lecture, summarizer):
    from src.ai import bucketer
    from src.ai.title import split_generated_title
    sub_id = str(lecture["sub_id"])
    row = db.get_lecture(sub_id)
    if row.get("summary"):
        return
    if not meta(db, sub_id, "audio") or not meta(db, sub_id, "ocr"):
        raise ValueError("需先完成并验证转录与 OCR")
    prompt, _ = bucketer.assemble(row["transcript"], meta(db, sub_id, "segments"), db.get_done_ppt_pages(sub_id))
    text, model = summarizer.summarize(TITLE, prompt)
    save_summary(db, checkpoint, sub_id, text, model)


def save_summary(db, checkpoint, sub_id: str, text: str, model: str):
    """Commit a complete API result only after its source stages passed."""
    from src.ai.title import split_generated_title
    if not meta(db, sub_id, "audio") or not meta(db, sub_id, "ocr"):
        raise ValueError("需先完成并验证转录与 OCR")
    title, body = split_generated_title(text)
    if not body.strip():
        raise ValueError("摘要正文为空")
    db.update_summary(sub_id, body, model)
    db.update_ai_title(sub_id, title)
    db.mark_processed(sub_id)
    db.clear_error(sub_id)
    checkpoint()
    print(f"[{sub_id}] 摘要已保存，模型 {model}", flush=True)


def import_summary(db, checkpoint, lecture, path: Path):
    from src.ai import bucketer
    sub_id = str(lecture["sub_id"])
    result = json.loads(path.read_text())
    row = db.get_lecture(sub_id)
    prompt, _ = bucketer.assemble(row["transcript"], meta(db, sub_id, "segments"), db.get_done_ppt_pages(sub_id))
    if (str(result.get("sub_id")) != sub_id
            or str(result.get("course_id", COURSE_ID)) != COURSE_ID
            or result.get("prompt_sha256") != hashlib.sha256(prompt.encode()).hexdigest()
            or result.get("finish_reason") != "stop"):
        raise ValueError("摘要的课次、输入指纹或完成状态不符")
    if not isinstance(result.get("text"), str) or not str(result.get("model", "")).strip():
        raise ValueError("摘要结果缺少正文或模型名")
    if row.get("summary"):
        return
    save_meta(db, sub_id, "summary", {"prompt_sha256": result["prompt_sha256"],
              "model": result["model"], "finish_reason": "stop"})
    save_summary(db, checkpoint, sub_id, result["text"], result["model"])


def summary_request(db, lecture):
    """Export source-bound messages for an already authorized API connector."""
    from src.ai import bucketer
    from src.ai.summary_prompt import SYSTEM_PROMPT, summary_user_content
    sub_id = str(lecture["sub_id"])
    if not meta(db, sub_id, "audio") or not meta(db, sub_id, "ocr"):
        raise ValueError("需先完成并验证转录与 OCR")
    row = db.get_lecture(sub_id)
    prompt, mode = bucketer.assemble(row["transcript"], meta(db, sub_id, "segments"), db.get_done_ppt_pages(sub_id))
    return {"sub_id": sub_id, "course_id": COURSE_ID,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "mode": mode,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": summary_user_content(TITLE, prompt)}]}


def print_status(db, manifest):
    rows = []
    for lecture in lecture_list(manifest):
        sub_id = str(lecture["sub_id"])
        row = db.get_lecture(sub_id) or {}
        media_path = RUN_DIR / "media" / f"{sub_id}.json"
        media = json.loads(media_path.read_text()) if media_path.exists() else {}
        rows.append({"sub_id": sub_id, "date": lecture["date"],
                     "has_playback": lecture.get("has_playback", False),
                     "audio_verified": bool(meta(db, sub_id, "audio")),
                     "ocr_verified": bool(meta(db, sub_id, "ocr")),
                     "summary_done": bool(row.get("summary")),
                     "downloaded_bytes": media.get("committed_bytes", 0),
                     "media_bytes": media.get("total_bytes")})
    with open(RUN_DIR / "job.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            running = False
        except BlockingIOError:
            running = True
    print(json.dumps({"course_id": COURSE_ID, "title": TITLE, "running": running,
                      "lectures": rows}, ensure_ascii=False, indent=2))


def write_summary_request(db, lecture):
    sub_id = str(lecture["sub_id"])
    output = RUN_DIR / "summary-requests"
    output.mkdir(exist_ok=True)
    write_private(output / f"{sub_id}.json", json.dumps(summary_request(db, lecture), ensure_ascii=False).encode())
    print(f"[{sub_id}] 已生成摘要连接器请求文件", flush=True)


def export_materials(db, lecture):
    """Make the verified local source reviewable without any API upload."""
    sub_id = str(lecture["sub_id"])
    if not (meta(db, sub_id, "audio") and meta(db, sub_id, "ocr")):
        raise ValueError("需先完成并验证转录与 OCR")
    row = db.get_lecture(sub_id)
    output = RUN_DIR / "materials" / sub_id
    output.mkdir(parents=True, exist_ok=True)
    header = f"# {TITLE} · {lecture['date']}\n\n教师：{TEACHER}\n\n"
    write_private(output / "transcript.md", (header + "以下为机器转录原文，尚未作术语和姓名校对。\n\n" + row["transcript"] + "\n").encode())
    pages = db.get_done_ppt_pages(sub_id)
    ocr = [header + "以下为课件 OCR 原文。\n"]
    for page in pages:
        ocr.append(f"\n## 第 {page['page_num']} 页\n\n{page.get('text') or ''}\n")
    write_private(output / "ocr.md", "\n".join(ocr).encode())
    write_private(output / "segments.json", json.dumps(meta(db, sub_id, "segments"), ensure_ascii=False).encode())
    write_private(output / "validation.json", json.dumps({"audio": meta(db, sub_id, "audio"), "ocr": meta(db, sub_id, "ocr")}, ensure_ascii=False, indent=2).encode())
    print(f"[{sub_id}] 本机材料：{output}", flush=True)


def export_index(db, manifest):
    output = RUN_DIR / "notes"
    output.mkdir(exist_ok=True)
    index = [f"# {TITLE}\n", f"教师：{TEACHER} · {DEPT} · {TERM}\n",
             "| 日期 | 课次 | 状态 | 笔记 |", "| --- | --- | --- | --- |"]
    combined = [f"# {TITLE}\n\n教师：{TEACHER} · {TERM}\n"]
    evidence = []
    for lecture in lecture_list(manifest):
        sub_id = str(lecture["sub_id"])
        row = db.get_lecture(sub_id) or {}
        audio, ocr = meta(db, sub_id, "audio"), meta(db, sub_id, "ocr")
        if not lecture.get("has_playback"):
            state, link = "平台无回放", "—"
        elif row.get("summary"):
            state, link = "已完成", f"[{row.get('ai_title') or '阅读'}]({sub_id}.md)"
            combined.append(f"\n## {lecture['date']} · {row.get('ai_title') or lecture['sub_title']}\n\n模型：{row.get('summary_model')}\n\n{row['summary']}\n")
        elif audio and ocr:
            state, link = "等待摘要", "—"
        else:
            state, link = "转录 / OCR 未完成", "—"
        index.append(f"| {lecture['date']} | {sub_id} | {state} | {link} |")
        evidence.append({"sub_id": sub_id, "date": lecture["date"], "status": state,
                         "audio": audio, "ocr": ocr, "transcript_chars": len(row.get("transcript") or ""),
                         "summary_chars": len(row.get("summary") or ""), "summary_model": row.get("summary_model"),
                         "summary": meta(db, sub_id, "summary")})
    write_private(output / "index.md", ("\n".join(index) + "\n").encode())
    write_private(output / "course.md", "\n".join(combined).encode())
    write_private(RUN_DIR / "validation.json", json.dumps(evidence, ensure_ascii=False, indent=2).encode())
    print(f"笔记入口：{output / 'index.md'}", flush=True)


def export_note(db, lecture):
    sub_id = str(lecture["sub_id"])
    row = db.get_lecture(sub_id) or {}
    if not row.get("summary"):
        return
    output = RUN_DIR / "notes"
    output.mkdir(exist_ok=True)
    content = f"# {row.get('ai_title') or lecture['sub_title']}\n\n课程：{TITLE}\n教师：{TEACHER}\n日期：{lecture['date']}\n模型：{row.get('summary_model')}\n\n{row['summary']}\n"
    write_private(output / f"{sub_id}.md", content.encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["inspect", "prepare", "summarize", "summary-request", "import-summary", "export-materials", "export", "status"], nargs="?", default="inspect")
    parser.add_argument("--course-config", type=Path, help="另一门课的 JSON 配置；默认课程 4048")
    parser.add_argument("--limit", type=int, default=1, help="最多处理几个未完成课次；默认 1")
    parser.add_argument("--sub-id", help="指定 manifest 中的课次 ID")
    parser.add_argument("--provider", help="摘要服务商名称；default 使用本机配置的首个已启用服务商")
    parser.add_argument("--model", help="摘要模型 ID，默认该服务商的第一个已保存模型")
    parser.add_argument("--summary-file", type=Path, help="已授权连接器生成的完整 JSON 摘要结果")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须至少为 1")
    if args.stage == "summarize" and not args.provider:
        parser.error("summarize 需要 --provider")
    if args.stage == "import-summary" and (not args.sub_id or not args.summary_file):
        parser.error("import-summary 需要 --sub-id 和 --summary-file")
    configure_course(args.course_config)
    os.umask(0o077)
    os.chdir(ROOT)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    os.environ["ASR_NUM_THREADS"] = "4"
    os.environ["IMAGE_WORKERS"] = "4"
    os.environ["OCR_MAX_WORKERS"] = "2"
    os.environ["OCR_MAX_TARGET"] = "2"
    if args.stage == "prepare":
        with running_job():
            return run(args)
    return run(args)


def run(args):
    creds = credentials()
    if args.stage in {"inspect", "prepare"}:
        client = login(creds)
        manifest = inspect(client)
    else:
        if not (RUN_DIR / "icourse.db.enc").is_file():
            raise ValueError("请先执行 prepare，建立课程加密检查点")
        manifest = json.loads((RUN_DIR / "manifest.json").read_text())
    lectures = lecture_list(manifest)
    if args.sub_id:
        lectures = [lecture for lecture in lectures if str(lecture["sub_id"]) == args.sub_id]
        if not lectures:
            raise ValueError(f"指定课次不属于课程 {COURSE_ID}")
    if args.stage == "inspect":
        return 0
    if args.stage in {"status", "summary-request", "export-materials"}:
        with read_snapshot(creds) as db:
            if args.stage == "status":
                print_status(db, manifest)
            else:
                processed = 0
                for lecture in lectures:
                    sub_id = str(lecture["sub_id"])
                    row = db.get_lecture(sub_id) or {}
                    ready = meta(db, sub_id, "audio") and meta(db, sub_id, "ocr")
                    if args.sub_id and not ready:
                        raise ValueError("指定课次尚未完成转录与 OCR")
                    if not ready or (args.stage == "summary-request" and row.get("summary")) or processed >= args.limit:
                        continue
                    if args.stage == "export-materials":
                        export_materials(db, lecture)
                    else:
                        write_summary_request(db, lecture)
                    processed += 1
                print(f"已导出 {processed} 个课次的{'本机材料' if args.stage == 'export-materials' else '待摘要请求'}", flush=True)
        return 0
    summarizer = build_summarizer(args.provider, args.model) if args.stage in {"summarize", "prepare"} and args.provider else None
    setup_ffmpeg() if args.stage == "prepare" else None
    scheduler = None
    pipeline = None
    summary_worker = None
    failures = []
    with local_database(creds) as (db, checkpoint):
        db.upsert_course(COURSE_ID, TITLE, TEACHER)
        db.upsert_all_courses_for_term(TERM, [{"course_id": COURSE_ID, "title": TITLE, "teacher": TEACHER, "dept": DEPT}])
        db.write_meta("local:manifest", json.dumps(manifest, ensure_ascii=False))
        for lecture in lectures:
            if lecture.get("has_playback"):
                db.insert_lecture(str(lecture["sub_id"]), COURSE_ID, lecture["sub_title"], lecture["date"])
        checkpoint()
        if args.stage == "prepare":
            from src.ai.transcriber import Transcriber
            from src.runtime.reporter import Reporter
            from src.runtime.scheduler import Scheduler
            from scripts.local_audio import LocalAudioDownloader
            from scripts.local_pipeline import LocalLectureRunner, StrictPPTClient, SummaryWorker
            # A crash between saving the proof and removing scratch media
            # must not leave a full recording consuming space forever.
            for lecture in lectures:
                sub_id = str(lecture["sub_id"])
                if meta(db, sub_id, "audio"):
                    for suffix in (".mp4.part", ".json", ".raw"):
                        (RUN_DIR / "media" / f"{sub_id}{suffix}").unlink(missing_ok=True)
            scheduler = Scheduler(Reporter())
            scheduler.audio_downloader.shutdown()
            scheduler.audio_downloader = LocalAudioDownloader(RUN_DIR / "media", write_private)
            transcriber = Transcriber(backend="sensevoice", num_threads=4)
            transcriber._init()
            client = StrictPPTClient(client, db, sys.modules[__name__])
            summary_worker = SummaryWorker(db, checkpoint, manifest, sys.modules[__name__], summarizer)
            pipeline = LocalLectureRunner(client, db, scheduler, transcriber, Reporter(),
                                          checkpoint=checkpoint, runner=sys.modules[__name__], summary_worker=summary_worker)
            targets = [lecture for lecture in lectures if lecture.get("has_playback")
                       and not (meta(db, str(lecture["sub_id"]), "audio") and meta(db, str(lecture["sub_id"]), "ocr"))][:args.limit]
            next_lectures = {str(lecture["sub_id"]): (COURSE_ID, str(targets[i + 1]["sub_id"])) if i + 1 < len(targets) else None
                             for i, lecture in enumerate(targets)}
            for lecture in lectures:
                sub_id = str(lecture["sub_id"])
                if meta(db, sub_id, "audio") and meta(db, sub_id, "ocr") and not (db.get_lecture(sub_id) or {}).get("summary"):
                    write_summary_request(db, lecture)
                    summary_worker.schedule(lecture)
            if targets:
                pipeline.prefetch_first(COURSE_ID, str(targets[0]["sub_id"]))
        processed = 0
        try:
            for lecture in lectures:
                sub_id = str(lecture["sub_id"])
                if not lecture.get("has_playback"):
                    continue
                db.insert_lecture(sub_id, COURSE_ID, lecture["sub_title"], lecture["date"])
                row = db.get_lecture(sub_id)
                if args.stage == "prepare" and meta(db, sub_id, "audio") and meta(db, sub_id, "ocr"):
                    continue
                if args.stage in {"summarize", "summary-request"} and row.get("summary"):
                    continue
                if args.stage != "export" and processed >= args.limit:
                    break
                processed += 1
                print(f"[{sub_id}] {lecture['sub_title']} — {args.stage}", flush=True)
                try:
                    if args.stage == "prepare":
                        # Shared Actions phases; cached media survives retries.
                        for attempt in range(3):
                            try:
                                pipeline.run(COURSE_ID, TITLE, lecture, next_info=next_lectures[sub_id])
                                break
                            except Exception:
                                if attempt == 2:
                                    raise
                                scheduler.image_cache.discard(sub_id)
                                client.retry(sub_id)
                                time.sleep(5 * (attempt + 1))
                                if not client.check_alive():
                                    client.client = login(creds)
                    elif args.stage == "summarize":
                        summarize(db, checkpoint, lecture, summarizer)
                    elif args.stage == "import-summary":
                        import_summary(db, checkpoint, lecture, args.summary_file)
                    elif row.get("summary"):
                        export_note(db, lecture)
                except Exception as exc:
                    error = safe_error(exc)
                    db.update_error(sub_id, args.stage, error)
                    checkpoint()
                    failures.append({"sub_id": sub_id, "error": error})
                    print(error, flush=True)
        finally:
            if pipeline:
                for sub_id in list(pipeline._ppt._prefetch_threads):
                    pipeline._ppt._join_prefetch(sub_id)
            if scheduler:
                scheduler.shutdown()
            if summary_worker:
                summary_worker.shutdown()
        if args.stage == "export":
            export_index(db, manifest)
        write_private(RUN_DIR / "last-run.json", json.dumps({"stage": args.stage, "attempted": processed, "failures": failures}, ensure_ascii=False, indent=2).encode())
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已停止；完成阶段的加密检查点已保留。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(safe_error(exc), file=sys.stderr)
        raise SystemExit(1)
