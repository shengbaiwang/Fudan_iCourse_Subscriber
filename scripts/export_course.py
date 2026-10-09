"""Export course notes to local HTML, Markdown or PDF files.

Usage:
    python scripts/export_course.py --course-id 30004 --output-dir exports
    python scripts/export_course.py --course-id 30004,30005 --pdf
    python scripts/export_course.py --course-id 30004 --sub-ids 1,2,5 --md

Each course becomes a separate file. GitHub Actions publishes these files
as downloadable artifacts; the script can also be run directly on a local DB.
"""

import argparse
import os
import sys
from html import escape
from pathlib import Path

# Allow importing from the project root when run as `python scripts/export_course.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.database import Database  # noqa: E402

# Override hardcoded pixel dimensions for PDF rendering.
# WeasyPrint maps CSS px to physical size at 96 DPI, which makes the
# pre-scaled latex images appear too small. This CSS lets the renderer
# size them naturally based on the image's intrinsic dimensions instead.
_PDF_LATEX_CSS = (
    "img { max-width: 100% !important; height: auto !important; }\n"
    'body { font-family: "Microsoft YaHei", sans-serif; }\n'
)


def _build_html(course_title: str, teacher: str, lectures: list[dict],
                pdf: bool = False) -> str:
    """Build a styled standalone document with embedded formula images."""
    from src.note_rendering import NOTE_CSS, PYGMENTS_CSS, markdown_to_html

    body_parts = [
        f"<h1>{escape(course_title)}</h1>",
        f"<p>任课教师：{escape(teacher)}</p>",
        "<hr>",
    ]
    for lec in lectures:
        body_parts.append(
            f"<h2>{escape(lec['sub_title'])} "
            f"<small>({escape(lec['date'])})</small></h2>"
        )
        body_parts.append(markdown_to_html(lec["summary"]))
        body_parts.append("<hr>")

    extra_css = f"\n{_PDF_LATEX_CSS}" if pdf else ""
    return (
        "<!DOCTYPE html>"
        "<html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{escape(course_title)}</title>"
        f"<style>{NOTE_CSS}\n{PYGMENTS_CSS}{extra_css}</style>"
        "</head><body>"
        + "\n".join(body_parts)
        + "</body></html>"
    )


def _build_plain(course_title: str, teacher: str, lectures: list[dict]) -> str:
    """Build a plain-text version of the summaries."""
    parts = [
        f"# 课程：{course_title}",
        f"任课教师：{teacher}",
    ]
    for lec in lectures:
        parts.append(f"## {lec['sub_title']} ({lec['date']})")
        parts.append(lec["summary"])
    return "\n".join(parts)


def _safe_filename(title: str) -> str:
    """Sanitise a course title for use as a filename."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip() or "course"


def _query_course(db: Database, course_id: str,
                  sub_ids: list[str] | None = None) -> tuple[str, str, list[dict]] | None:
    """Return ``(course_title, teacher, lectures)`` for *course_id*.

    Returns ``None`` if the course is missing or has no summaries.

    Args:
        sub_ids: When provided, restrict the result to lectures whose
                 ``sub_id`` is in the list.  String comparison — pass the
                 same form the database stores (the schema treats sub_id
                 as TEXT/INTEGER interchangeably).
    """
    course = db.conn.execute(
        "SELECT * FROM courses WHERE course_id = ?", (course_id,)
    ).fetchone()
    if not course:
        print(f"Course {course_id} not found in database – skipping.")
        return None

    course_title = course["title"]
    teacher = course["teacher"]

    rows = db.conn.execute(
        """SELECT sub_id, sub_title, date, summary
           FROM lectures
           WHERE course_id = ? AND summary IS NOT NULL
           ORDER BY CAST(sub_id AS INTEGER) ASC""",
        (course_id,),
    ).fetchall()
    lectures = [dict(row) for row in rows]

    if sub_ids:
        wanted = {str(s) for s in sub_ids}
        lectures = [lec for lec in lectures if str(lec["sub_id"]) in wanted]

    if not lectures:
        print(f"No summaries found for course {course_id} ({course_title}) – skipping.")
        return None

    print(f"Found {len(lectures)} summarized lecture(s) for {course_title}.")
    return course_title, teacher, lectures


def main():
    parser = argparse.ArgumentParser(description="Export course summaries to files.")
    parser.add_argument(
        "--course-id", required=True,
        help="Comma-separated course IDs to export (e.g. 30004 or 30004,30005)",
    )
    parser.add_argument(
        "--sub-ids", default="",
        help="Optional comma-separated sub_ids to export only selected lectures",
    )
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument("--pdf", action="store_true", help="Export PDF files")
    formats.add_argument("--md", action="store_true", help="Export Markdown files")
    parser.add_argument("--db", default="data/icourse.db", help="Database path")
    parser.add_argument(
        "--output-dir", default="exports", help="Output directory (default: exports)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        parser.error(f"Database not found: {args.db}")
    course_ids = list(dict.fromkeys(cid.strip() for cid in args.course_id.split(",") if cid.strip()))
    if not course_ids:
        parser.error("No valid course IDs provided.")
    sub_ids = [sid.strip() for sid in args.sub_ids.split(",") if sid.strip()] or None

    if args.pdf:
        try:
            import weasyprint
        except ImportError:
            parser.error("PDF export requires weasyprint: pip install weasyprint")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    db = Database(args.db)
    exported = 0
    try:
        for cid in course_ids:
            result = _query_course(db, cid, sub_ids=sub_ids)
            if result is None:
                continue
            course_title, teacher, lectures = result
            stem = f"{_safe_filename(course_title)}_{_safe_filename(cid)}_summaries"
            extension = "pdf" if args.pdf else "md" if args.md else "html"
            destination = output_dir / f"{stem}.{extension}"
            if args.md:
                destination.write_text(_build_plain(course_title, teacher, lectures), encoding="utf-8")
            else:
                html = _build_html(course_title, teacher, lectures, pdf=args.pdf)
                if args.pdf:
                    weasyprint.HTML(string=html).write_pdf(str(destination))
                else:
                    destination.write_text(html, encoding="utf-8")
            exported += 1
            print(f"[OK] Exported: {destination} ({destination.stat().st_size} bytes)")
    finally:
        db.conn.close()
    if not exported:
        parser.exit(1, "No courses with summaries found – nothing to export.\n")


if __name__ == "__main__":
    main()
