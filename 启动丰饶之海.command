#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
APP_PATH="${PROJECT_DIR}/dist/desktop/丰饶之海.app"
if [[ -d "${APP_PATH}" ]]; then
  exec /usr/bin/open "${APP_PATH}"
fi
cd "${PROJECT_DIR}"
if [[ ! -x .venv-course/bin/python ]]; then
  /usr/bin/osascript -e 'display alert "请先构建丰饶之海" message "在项目中执行 .venv-course/bin/python scripts/build_desktop.py，然后再次打开。"'
  exit 1
fi
.venv-course/bin/python scripts/build_desktop.py
exec /usr/bin/open "${APP_PATH}"
