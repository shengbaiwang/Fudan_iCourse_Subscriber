"""iCourse Subscriber — top-level orchestration.

The runtime is split across cooperating components — Scheduler (pools +
resource monitor), Reporter (centralised logging), PPTPipeline, LectureRunner,
AudioDownloader.  This file does only orchestration:

  1. Build all components.
  2. Login + enumerate.
  3. Drive LectureRunner across the queued lectures.
  4. Shutdown (each lecture persists its notes as it completes).

Anything more interesting belongs in one of ``src/*`` modules.
"""

import datetime
import json
import time
import traceback

from src.runtime import config
from src.runtime.auto_check import PAUSE_SCANS_META, parse_auto_check_pauses
from src.data.database import Database
from src.api.icourse import ICourseClient
from src.pipeline.lecture_runner import LectureRunner
from src.runtime.reporter import Reporter
from src.runtime.scheduler import Scheduler
from src.runtime.run_order import ordered_lectures
from src.runtime.progress import emit, note_saved
from src.ai.summarizer import Summarizer
from src.ai.transcriber import Transcriber
from src.api.webvpn import WebVPNSession


def login_with_retry(max_attempts: int = 10) -> WebVPNSession:
    """Login to WebVPN + iCourse CAS, retrying on transient failures.

    The iCourse CAS step (authenticate_icourse) has its own inner retry
    loop for transient redirect-chain hiccups; this outer loop only runs
    when those inner retries are exhausted, which generally means the
    WebVPN session itself needs a fresh login.  5 attempts handles the
    long tail of times when CAS rejects multiple fresh sessions in a
    row before letting one through.
    """
    for attempt in range(max_attempts):
        try:
            vpn = WebVPNSession()
            print(f"\n[Login] WebVPN (attempt {attempt + 1}/{max_attempts})...")
            vpn.login()
            print("[Login] iCourse CAS...")
            vpn.authenticate_icourse()
            return vpn
        except Exception as e:
            if attempt < max_attempts - 1:
                print(f"  Failed: {type(e).__name__}: {e}; retrying...")
                time.sleep(5)
            else:
                raise


def _check_session(client: ICourseClient) -> None:
    """Verify WebVPN session; re-login in place if expired.

    Mutates ``client`` so background workers holding the same instance
    automatically pick up refreshed cookies.
    """
    if client.check_alive():
        return
    print("[Session] WebVPN session expired, re-logging in...")
    client.vpn = login_with_retry()
    client._userinfo = None


def _enumerate_lectures(client: ICourseClient, db: Database,
                        reporter: Reporter, *, force_video_recheck: bool = False) -> list[tuple[str, str, dict]]:
    """Sync, fast: list every (course_id, course_title, lecture) we'll
    process this run.  Done up-front so the prefetch loop can see across
    course boundaries when picking the "next" lecture."""
    out: list[tuple[str, str, dict]] = []
    pause_scans = parse_auto_check_pauses(db.read_meta(PAUSE_SCANS_META))
    pause_scans = {cid: token for cid, token in pause_scans.items() if cid in config.AUTO_CHECK_PAUSES}
    for course_id in config.COURSE_IDS:
        try:
            pause_token = config.AUTO_CHECK_PAUSES.get(course_id)
            if pause_token and pause_scans.get(course_id) == pause_token:
                course = db.get_course(course_id) or {}
                title = course.get("title") or course_id
                pending = ordered_lectures(db.get_unprocessed_lectures(course_id, include_deferred=force_video_recheck), config.LECTURE_ORDER)
                blocked = db.get_exhausted_sub_ids(course_id)
                reporter.info(f"[Auto check] {title}: 已暂停新课次检查，待补齐 {len(pending)} 节"
                              + (f"，{len(blocked)} 节达到重试上限" if blocked else ""))
                out.extend((course_id, title, lecture) for lecture in pending)
                continue
            _check_session(client)
            detail = client.get_course_detail(course_id)
            course_title = detail["title"]
            teacher = detail["teacher"]
            lectures = detail["lectures"]
            playback_count = sum(1 for l in lectures if l.get("has_playback"))
            reporter.course_header(
                course_id, course_title, teacher,
                total=len(lectures), playback=playback_count,
            )
            db.upsert_course(course_id, course_title, teacher)

            # School system sometimes lists duplicate lectures; dedup the
            # raw list so the same logic produces the same outcome each run.
            # When a sub_title appears more than once, keep the first one
            # that has playback; if none have playback, keep the first.
            seen_sub_titles: dict[str, dict] = {}
            deduped = []
            for lec in lectures:
                title = lec.get("sub_title", "")
                if not title:
                    deduped.append(lec)
                    continue
                existing = seen_sub_titles.get(title)
                if existing is None:
                    seen_sub_titles[title] = lec
                    deduped.append(lec)
                elif not existing.get("has_playback") and lec.get("has_playback"):
                    # replace the earlier no-playback entry in place so the
                    # processing order stays chronological
                    deduped[deduped.index(existing)] = lec
                    seen_sub_titles[title] = lec
                    reporter.course_dedup_skip(title, existing["sub_id"])
                else:
                    reporter.course_dedup_skip(title, lec["sub_id"])
            lectures = deduped

            known_processed = db.get_processed_sub_ids(course_id)
            exhausted = db.get_exhausted_sub_ids(course_id)
            deferred = set() if force_video_recheck else db.get_deferred_sub_ids(course_id)
            new_lectures = [
                lec for lec in lectures
                if lec.get("has_playback")
                and str(lec["sub_id"]) not in known_processed
                and str(lec["sub_id"]) not in exhausted
                and str(lec["sub_id"]) not in deferred
            ]
            unprocessed = db.get_unprocessed_lectures(course_id, include_deferred=force_video_recheck)
            new_ids = {str(lec["sub_id"]) for lec in new_lectures}
            retry_only = [
                {"sub_id": u["sub_id"], "sub_title": u["sub_title"],
                 "date": u["date"]}
                for u in unprocessed if u["sub_id"] not in new_ids
            ]
            new_lectures.extend(retry_only)
            new_lectures = ordered_lectures(new_lectures, config.LECTURE_ORDER)
            reporter.course_new_count(len(new_lectures))
            for lecture in new_lectures:
                sub_id = str(lecture["sub_id"])
                db.insert_lecture(
                    sub_id, course_id,
                    lecture.get("sub_title", ""),
                    lecture.get("date", ""),
                )
                out.append((course_id, course_title, lecture))
            awaiting_playback = [lec for lec in lectures
                                 if not lec.get("has_playback")
                                 and str(lec["sub_id"]) not in known_processed | exhausted]
            if pause_token and not awaiting_playback:
                # Persist only after all discovered playback rows are saved;
                # a failed discovery remains eligible for the final scan.
                pause_scans[course_id] = pause_token
                db.write_meta(PAUSE_SCANS_META, json.dumps(pause_scans))
            elif pause_token:
                reporter.info(f"[Auto check] {course_title}: {len(awaiting_playback)} 节录播尚未发布，继续核对直到可补齐。")
        except Exception:
            emit("course_failed", course_id=course_id)
            reporter.course_enumeration_error(course_id)
            traceback.print_exc()
    return out


def _drive_lectures(client: ICourseClient, db: Database,
                    scheduler: Scheduler, transcriber: Transcriber,
                    summarizer: Summarizer, reporter: Reporter,
                    all_lectures: list[tuple[str, str, dict]], *, runner=None) -> None:
    """Phase 2: run each lecture through LectureRunner.

    Pre-schedules the first lecture's prefetch (audio + images) before
    entering the loop; subsequent prefetches are kicked off from inside
    each LectureRunner.run via ``next_info``.
    """
    if not all_lectures:
        return

    runner = runner or LectureRunner(
        client, db, scheduler, transcriber, summarizer, reporter,
    )
    emit("queue", total=len(all_lectures))

    first_course, _, first_lec = all_lectures[0]
    runner.prefetch_first(first_course, str(first_lec["sub_id"]))

    for i, (course_id, course_title, lecture) in enumerate(all_lectures):
        sub_id = str(lecture["sub_id"])
        next_info: tuple[str, str] | None = None
        if i + 1 < len(all_lectures):
            next_course, _, next_lec = all_lectures[i + 1]
            next_info = (next_course, str(next_lec["sub_id"]))

        _check_session(client)
        emit("lecture", sub_id=sub_id, course_id=course_id, index=i + 1,
             total=len(all_lectures), title=lecture.get("sub_title", ""))
        try:
            runner.run(
                course_id, course_title, lecture, next_info=next_info,
            )
            saved = db.get_lecture(sub_id) or {}
            if saved.get("summary"):
                note_saved(db, sub_id)
            else:
                emit("failed", sub_id=sub_id)
        except Exception:
            emit("failed", sub_id=sub_id)
            reporter.lecture_error(sub_id)
            traceback.print_exc()
        finally:
            # Belt-and-braces: drop any lingering prefetch entry for this
            # lecture so we don't leak bytes if the runner crashed before
            # PPTPipeline.submit released the cache.
            scheduler.image_cache.discard(sub_id)
            scheduler.audio_downloader.release(sub_id)


def _crawl_semester_catalog(client: ICourseClient, db: Database,
                            reporter: Reporter) -> None:
    """Auto-discover every available semester and refresh ``all_courses``.

    Walks every page of get-course-list for each discovered term and
    replaces the term's catalog in one transaction.  No longer requires
    the ``CRAWL_TERM`` secret — the API tells us what terms exist.
    """
    reporter.info("Discovering available semesters from API...")
    try:
        _check_session(client)
        terms = client.discover_terms()
    except Exception as e:
        reporter.crawl_courses_failed("discovery", e)
        return

    if not terms:
        reporter.info("No semesters found via API discovery.")
        return

    reporter.info(f"Found {len(terms)} semester(s): "
                  f"{', '.join(t['name'] for t in terms)}")

    for term_info in terms:
        code = term_info["code"]
        name = term_info["name"]
        expected = term_info["count"]
        reporter.crawl_courses_start(name)
        t0 = time.time()
        try:
            _check_session(client)
            rows = client.list_semester_courses(code)
            if not rows:
                reporter.info(f"  Term {name}: API returned 0 courses, skipping.")
                continue
            # Pass the human-readable term name (not the API code) to the
            # DB so the frontend displays "2025-20262" instead of "25".
            deleted, upserted = db.upsert_all_courses_for_term(name, rows)
            reporter.crawl_courses_done(
                name, len(rows), deleted, upserted, time.time() - t0,
            )
        except Exception as e:
            reporter.crawl_courses_failed(name, e)
        reporter.info(f"  ({code}) → {expected} API courses, "
                      f"{len(rows)} fetched")

    reporter.info("Semester catalog crawl complete.")


def run():
    """Single execution of the full pipeline."""
    reporter = Reporter()
    reporter.run_header()

    if not config.COURSE_IDS and not config.CRAWL_TERM:
        reporter.info(
            "No COURSE_IDS configured. Set COURSE_IDS to process lectures "
            "or leave empty for crawl-only mode."
        )
        # Fall through — crawl-only mode is valid.

    db = Database()
    corrected = db.sync_dates_from_sub()
    if corrected:
        print(f"  [Date] Synced {corrected} lecture date(s) from sub_title", flush=True)
    # Refresh the semester catalog: run on the 5th and 25th of each month,
    # or immediately if the database has no catalog data yet.
    has_catalog = db.has_all_courses()
    today = datetime.datetime.now().day
    needs_catalog = not has_catalog or today in (5, 25)
    scans = parse_auto_check_pauses(db.read_meta(PAUSE_SCANS_META))
    if config.COURSE_IDS and not needs_catalog and all(
        cid in config.AUTO_CHECK_PAUSES and scans.get(cid) == config.AUTO_CHECK_PAUSES[cid]
        and not db.get_unprocessed_lectures(cid, include_deferred=config.VIDEO_RECHECK_NOW) for cid in config.COURSE_IDS
    ):
        reporter.info("订阅课程均已暂停且没有可自动补齐的课次，跳过平台登录与检查。")
        reporter.run_footer()
        return

    vpn = login_with_retry()
    client = ICourseClient(vpn)
    if needs_catalog:
        _crawl_semester_catalog(client, db, reporter)
    else:
        reporter.info("Skipping catalog crawl (has data, not the 5th or 25th).")

    if not config.COURSE_IDS:
        # Crawl-only mode: nothing to process, just persist + exit.
        reporter.info("\n[Crawl-only mode] No COURSE_IDS — skipping lectures.")
        reporter.run_footer()
        return

    all_lectures = _enumerate_lectures(client, db, reporter, force_video_recheck=config.VIDEO_RECHECK_NOW)
    if not all_lectures:
        reporter.info("没有待处理课次，本次检查完成。")
        reporter.run_footer()
        return
    transcriber = Transcriber()
    summarizer = Summarizer()
    scheduler = Scheduler(reporter=reporter)
    try:
        _drive_lectures(
            client, db, scheduler, transcriber, summarizer, reporter,
            all_lectures,
        )

    finally:
        scheduler.shutdown()

    reporter.run_footer()


if __name__ == "__main__":
    run()
