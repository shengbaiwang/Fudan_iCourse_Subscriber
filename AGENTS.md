## 用户界面交互偏好

- 常用编辑操作优先融入内容本身和现有交互，不另开独立编辑框或集中编辑表单。
- 优先使用原位编辑、双击重命名、右键或长按操作、拖拽排序与移动；兼顾触屏和键盘操作。
- 打磨操作反馈、误操作恢复和撤销，让交互自然、轻量、连贯。

## 本机开发与验证

- 日常开发默认使用丰饶之海 Dev：运行 `.venv-course/bin/python scripts/dev_desktop.py`，或双击 `启动丰饶之海 Dev.command`。
- 前端源码在 `local_web/static/`；修改后在 Dev 窗口按 `⌘R` 刷新。Python 修改后退出并重新启动 Dev；Swift 修改后用同一入口自动重新编译原生外壳。
- 仅在用户要求发布、安装或验收正式包时运行 `scripts/build_desktop.py`；日常迭代无需重打包、复制到「应用程序」或重装。
- Dev 默认沿用现有本机资料。自动验证应设置 `SEA_DISABLE_KEYCHAIN=1`，并将 `ICOURSE_WEB_CONFIG_DIR`、`ICOURSE_WEB_CACHE_DIR` 指向临时目录。
