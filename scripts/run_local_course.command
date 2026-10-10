#!/bin/bash
set -euo pipefail
COURSE_PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$COURSE_PROJECT_DIR"
if [[ ! -x .venv-course/bin/python ]]; then
  echo "缺少 .venv-course；请按 docs/local-course-4048-plan.md 安装本机依赖。"
  exit 1
fi
if [[ $# -eq 0 ]]; then
  set -- prepare --limit 10000
fi
exec caffeinate -i .venv-course/bin/python -u scripts/local_course.py "$@"
