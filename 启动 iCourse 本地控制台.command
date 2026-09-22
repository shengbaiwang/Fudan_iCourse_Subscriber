#!/bin/zsh
# Double-click launcher for macOS Finder.  Keep this file beside the project.

set -eu

PROJECT_DIR="${0:A:h}"
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

PORT=""
for candidate in {8765..8785}; do
  URL="http://127.0.0.1:${candidate}"
  # An unrelated local server may answer on the port.  Only reuse iCourse.
  if /usr/bin/curl --fail --silent --max-time 1 "${URL}/api/local/status" 2>/dev/null | \
      /usr/bin/grep -q '"database_ready":'; then
    echo "iCourse 已在 ${URL} 运行，正在打开浏览器…"
    /usr/bin/open "${URL}"
    exit 0
  fi
  if .venv-web/bin/python -c 'import socket, sys; s = socket.socket(); s.settimeout(0.2); occupied = s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0; s.close(); sys.exit(0 if occupied else 1)' "${candidate}"; then
    continue
  fi
  if [[ -z "${PORT}" ]]; then
    PORT="${candidate}"
  fi
done

if [[ -z "${PORT}" ]]; then
  echo "无法启动：8765–8785 端口都已被占用。" >&2
  exit 1
fi

if [[ "${PORT}" != "8765" ]]; then
  echo "默认端口 8765 已被其他程序占用，改用 ${PORT}。"
fi
echo "正在启动 iCourse 本地控制台…"
exec .venv-web/bin/python -m local_web --port "${PORT}"
