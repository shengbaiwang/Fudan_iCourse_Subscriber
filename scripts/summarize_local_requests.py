"""Summarize verified local materials while the preparation job keeps running.

The API key comes from Keychain or hidden terminal input/environment; it is
never written to project files or logs.
The preparation job imports source-bound results through its existing inbox.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import local_course as runner


from src.ai.model_policy import IncompleteSummaryError

def complete_result(value, request):
    return (isinstance(value, dict)
            and str(value.get("sub_id")) == request["sub_id"]
            and str(value.get("course_id")) == request["course_id"]
            and value.get("prompt_sha256") == request["prompt_sha256"]
            and value.get("finish_reason") == "stop"
            and isinstance(value.get("text"), str) and bool(value["text"].strip())
            and isinstance(value.get("model"), str) and bool(value["model"].strip()))


def generate_result(summarizer, request):
    response, provider, requested_model, options = summarizer.complete_messages(request["messages"])
    usage = getattr(response, "usage", None)
    text = response.choices[0].message.content
    actual_model = response.model
    if not isinstance(text, str) or not text.strip() or not isinstance(actual_model, str) or not actual_model.strip():
        raise ValueError("摘要缺少完整正文或实际模型名")
    receipt = {"course_id": request["course_id"], "sub_id": request["sub_id"],
            "prompt_sha256": request["prompt_sha256"], "text": text,
            "model": f"{provider['name']}/{actual_model}", "requested_model": requested_model,
            "provider": provider["name"], "base_url": provider["base_url"],
            "finish_reason": "stop"}
    if usage is not None:
        details = getattr(usage, "completion_tokens_details", None)
        receipt["usage"] = {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                            "completion_tokens": getattr(usage, "completion_tokens", None),
                            "reasoning_tokens": getattr(details, "reasoning_tokens", None)}
    if "max_completion_tokens" in options:
        receipt["max_completion_tokens"] = options["max_completion_tokens"]
        receipt["thinking"] = "disabled"
    return receipt


def write_status(state, **details):
    runner.write_private(runner.RUN_DIR / "summary-status.json", json.dumps({
        "state": state, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **details,
    }, ensure_ascii=False, indent=2).encode())


def generate_with_retry(summarizer, request):
    from openai import APIConnectionError, APIStatusError
    for attempt in range(3):
        try:
            return generate_result(summarizer, request)
        except (APIConnectionError, APIStatusError, RuntimeError) as exc:
            failures = getattr(exc, "failures", [exc])
            def transient(error):
                status = getattr(error, "status_code", None)
                return (isinstance(error, APIConnectionError) or isinstance(error, APIStatusError)
                        and (status == 429 or status is not None and status >= 500))
            if not failures or not all(transient(error) for error in failures) or attempt == 2:
                raise
            delay = 10 * (attempt + 1)
            print(f"[{request['sub_id']}] 已配置模型均暂时不可用；"
                  f"{delay} 秒后按保存顺序重试。", flush=True)
            time.sleep(delay)


def preparation_running():
    with open(runner.RUN_DIR / "job.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def import_if_idle(creds, manifest):
    # The preparation process owns the writable database while active.
    # Preserve inbox files if it starts between the lock check and import.
    if preparation_running():
        return
    try:
        with runner.local_database(creds) as (db, checkpoint):
            for lecture in runner.lecture_list(manifest):
                sub_id = str(lecture["sub_id"])
                path = runner.RUN_DIR / "summary-results" / f"{sub_id}.json"
                if not path.exists() or (db.get_lecture(sub_id) or {}).get("summary"):
                    continue
                if not (runner.meta(db, sub_id, "audio") and runner.meta(db, sub_id, "ocr")):
                    continue
                runner.import_summary(db, checkpoint, lecture, path)
                runner.export_note(db, lecture)
            runner.export_index(db, manifest)
    except RuntimeError as exc:
        if "已有本机任务" not in str(exc):
            raise


def process_requests(creds, manifest, lectures, summarizer, watch):
    output = runner.RUN_DIR / "summary-results"
    output.mkdir(exist_ok=True)
    os.chmod(output, 0o700)
    blocked = {}
    chain = [{"name": p["name"], "base_url": p["base_url"], "models": p["models"]} for p in summarizer.providers]
    for path in (runner.RUN_DIR / "summary-failures").glob("*.json"):
        try:
            failure = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (failure.get("course_id") == runner.COURSE_ID
                and failure.get("model_chain") == chain
                and failure.get("finish_reason") == "content_filter"):
            blocked[str(failure["sub_id"])] = {
                "prompt_sha256": failure["prompt_sha256"], "finish_reason": "content_filter",
            }
    while True:
        pending = []
        with runner.read_snapshot(creds) as db:
            for lecture in lectures:
                sub_id = str(lecture["sub_id"])
                if (db.get_lecture(sub_id) or {}).get("summary"):
                    blocked.pop(sub_id, None)
                    continue
                if not (runner.meta(db, sub_id, "audio") and runner.meta(db, sub_id, "ocr")):
                    pending.append(sub_id)
                    continue
                # Rebuild from the encrypted, verified checkpoint. Never
                # upload an unchecked or edited request file from disk.
                request = runner.summary_request(db, lecture)
                if blocked.get(sub_id, {}).get("prompt_sha256") == request["prompt_sha256"]:
                    continue
                path = output / f"{sub_id}.json"
                try:
                    existing = json.loads(path.read_text())
                except (OSError, ValueError):
                    existing = None
                if complete_result(existing, request):
                    continue
                print(f"[{sub_id}] 正在生成课堂笔记…", flush=True)
                write_status("generating", sub_id=sub_id)
                try:
                    result = generate_with_retry(summarizer, request)
                except IncompleteSummaryError as exc:
                    blocked[sub_id] = {"prompt_sha256": request["prompt_sha256"],
                                       "finish_reason": exc.finish_reason}
                    failures = runner.RUN_DIR / "summary-failures"
                    failures.mkdir(exist_ok=True)
                    runner.write_private(failures / f"{sub_id}.json", json.dumps({
                        "course_id": runner.COURSE_ID, "sub_id": sub_id,
                        "model_chain": chain,
                        **blocked[sub_id],
                    }, ensure_ascii=False, indent=2).encode())
                    print(f"[{sub_id}] {exc}；保留失败记录，继续其他课次。", flush=True)
                    continue
                runner.write_private(path, json.dumps(result, ensure_ascii=False).encode())
                write_status("saved", sub_id=sub_id, model=result["model"])
                print(f"[{sub_id}] 完整笔记已交给本机任务导入：{result['model']}", flush=True)
        import_if_idle(creds, manifest)
        if blocked:
            with runner.read_snapshot(creds) as db:
                blocked = {sub_id: failure for sub_id, failure in blocked.items()
                           if not (db.get_lecture(sub_id) or {}).get("summary")}
        if not pending:
            if blocked:
                write_status("partial_failed", failures=blocked)
                print(f"其他课次已处理；仍未完成的摘要：{', '.join(blocked)}", flush=True)
                return 1
            write_status("complete")
            print("所选课次的完整摘要结果已全部保存。", flush=True)
            return 0
        if not watch:
            write_status("waiting_materials", pending=pending)
            print(f"尚未验收的课次：{', '.join(pending)}；可加 --watch 等待。", flush=True)
            return 0
        if not preparation_running():
            write_status("preparation_stopped", pending=pending)
            print(f"准备进程已退出，以下课次仍未验收：{', '.join(pending)}；请续跑准备任务。", flush=True)
            return 1
        write_status("waiting_materials", pending=pending, failures=blocked)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course-config", type=Path)
    parser.add_argument("--provider", default="default")
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--watch", action="store_true", help="保持终端运行，等待后续本机材料验收")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须至少为 1")
    runner.configure_course(args.course_config)
    os.umask(0o077)
    manifest = json.loads((runner.RUN_DIR / "manifest.json").read_text())
    lectures = [lecture for lecture in runner.lecture_list(manifest) if lecture.get("has_playback")][:args.limit]
    with open(runner.RUN_DIR / "summary-api.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("这门课已有终端摘要任务运行") from None
        print(f"按已保存模型顺序生成课程 {runner.COURSE_ID} 的课堂笔记。", flush=True)
        creds = runner.credentials()
        write_status("waiting_key", model_policy="saved-order")
        summarizer = runner.build_summarizer(args.provider, args.model)
        return process_requests(creds, manifest, lectures, summarizer, args.watch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        write_status("stopped")
        print("摘要等待已停止；本机转录任务和完整摘要结果均保留。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        # API error bodies may contain sensitive data. Persist only the
        # error class/code, or our locally constructed validation message.
        details = {"error_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None)}
        if isinstance(exc, ValueError):
            details["message"] = runner.safe_error(exc)
        write_status("failed", **details)
        message = details.get("message") or f"{type(exc).__name__}（HTTP {details['http_status']}）"
        print(message, file=sys.stderr)
        raise SystemExit(1)
