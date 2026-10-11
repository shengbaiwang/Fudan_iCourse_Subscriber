#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
cd "${PROJECT_DIR}"
if [[ ! -x .venv-course/bin/python ]]; then
  print -u2 '缺少 .venv-course/bin/python，请先按 desktop/README.md 准备开发环境。'
  exit 1
fi
exec .venv-course/bin/python scripts/dev_desktop.py
