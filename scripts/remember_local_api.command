#!/bin/bash
set -euo pipefail
COURSE_PROJECT_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$COURSE_PROJECT_DIR"
exec .venv-course/bin/python -u scripts/remember_local_api.py "$@"
