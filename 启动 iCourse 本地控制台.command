#!/bin/zsh
# Double-click launcher for macOS Finder.  Keep this file beside the project.

set -eu

PROJECT_DIR="${0:A:h}"
PORT="8765"
URL="http://127.0.0.1:${PORT}"

if /usr/bin/curl --fail --silent --max-time 1 "${URL}/api/local/status" >/dev/null 2>&1; then
  /usr/bin/open "${URL}"
  exit 0
fi

cd "${PROJECT_DIR}"

if [[ ! -x ".venv-web/bin/python" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display alert "无法启动 iCourse" message "没有找到 Python 3。请先安装 Python 3.12 或更高版本。" as critical'
    exit 1
  fi
  echo "首次启动：正在准备本地控制台…"
  python3 -m venv .venv-web
  .venv-web/bin/python -m pip install --upgrade pip
  .venv-web/bin/python -m pip install -r requirements-web.txt
fi

echo "正在启动 iCourse 本地控制台…"
exec .venv-web/bin/python -m local_web --port "${PORT}"
