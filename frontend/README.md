# 统一控制台入口

界面源码已合并到 `local_web/static/`。不要在这里添加第二套 HTML、CSS 或交互逻辑。

- 本地：`python -m local_web`
- Pages：`python3 scripts/build_frontend.py`，部署 `dist/frontend/`
- 仓库静态预览：在仓库根目录运行 `python3 -m http.server 8000`，访问 `/frontend/`，会跳转到共享页面的 Pages 模式。

原有 WebCrypto、GitHub 和 sql.js 模块已移到 `local_web/static/browser/`。
