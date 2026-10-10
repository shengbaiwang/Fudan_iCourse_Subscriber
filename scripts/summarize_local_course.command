#!/bin/bash
set -euo pipefail
umask 077
COURSE_PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$COURSE_PROJECT_DIR"
COURSE_SUMMARY_LOG="$COURSE_PROJECT_DIR/work/local-course-4048/summary.log"
touch "$COURSE_SUMMARY_LOG"
chmod 600 "$COURSE_SUMMARY_LOG"
# getpass reads/writes /dev/tty directly; the key never enters this log.
exec caffeinate -i .venv-course/bin/python -u scripts/summarize_local_requests.py --watch --limit 8 "$@" \
  > >(tee -a "$COURSE_SUMMARY_LOG") 2>&1
