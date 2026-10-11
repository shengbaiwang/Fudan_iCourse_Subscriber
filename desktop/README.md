# 丰饶之海 · 桌面应用

丰饶之海是基于现有 iCourse 功能的 macOS 私人知识库。界面参考 Glossa 的阅读节奏与排版，用独立的海蓝配色和现有海浪图标形成自己的视觉风格。

## 使用

打开 `dist/desktop/丰饶之海.app`，也可双击项目根目录的 `启动丰饶之海.command`。将应用复制到「应用程序」后即可独立运行。当前构建支持 Apple Silicon 和 macOS 13 及以上；安装包使用本机临时签名，跨机器分发尚未经过 Apple 公证。

手记可直接点击标题与正文编辑，自动保存。右上角菜单提供历史版本、Markdown 导出和移入回收站；删除后可立即撤销，也可在回收站恢复。阅读模式会排版 Markdown 和公式。

常用快捷键：`⌘N` 新建、`⌘S` 保存、`⌘E` 阅读/编辑、`⌘K` 搜索、`⌘O` 导入 Markdown、`⌘,` 设置。课程区域保留双击重命名、右键操作与拖拽整理。

在「设置 → 私人手记」导出备份，ZIP 内含独立 Markdown 文件，以及保留手记、收藏、回收站和历史版本的 JSON。恢复备份会合并资料；内容不同的同名标识会保留为新手记，避免覆盖现有内容。手记备份不包含课程和讲座，课程资料继续使用原有加密同步与 Obsidian 导出。

## 数据

为兼容现有配置，继续使用原来的目录：

```text
~/Library/Application Support/fudan-icourse-subscriber/
  library/notebook.sqlite3   私人手记与历史版本
  desktop-state.json        外观、阅读位置和待保存草稿
```

应用更新与删除安装包不会删除这份资料。个人手记保存在本机 SQLite，使用当前用户文件权限；课程镜像保持原来的加密机制。重要手记可定期导出备份并保存到自己的备份位置。账号凭据仍可选择保存到 macOS 钥匙串。

桌面服务只监听 `127.0.0.1` 的动态端口，并要求当前原生窗口持有的会话令牌。每次启动的端口不同，原生层会持久保存界面状态；退出前等待手记保存并关闭后台服务。

## 构建

### 日常开发（Dev）

双击项目根目录的 `启动丰饶之海 Dev.command`，或在项目目录执行：

```sh
.venv-course/bin/python scripts/dev_desktop.py
```

首次只编译原生窗口外壳，之后复用它；后端直接运行项目中的 Python，界面直接读取
`local_web/static/`，无需打包 Python、模型、ZIP，也无需复制到「应用程序」或重装。
窗口和应用名称为「丰饶之海 Dev」，应用标识与正式版分开，产物位于
`dist/desktop-dev/丰饶之海 Dev.app`，依赖当前项目目录和 `.venv-course` 环境。
启动脚本所在终端保留开发日志；用 `⌘Q` 退出 Dev 后脚本结束。

- 修改 HTML、CSS、JavaScript：在 Dev 窗口按 `⌘R`，或选择「显示 → 刷新开发界面」。刷新前会保存当前手记；保存失败时保留当前页面。
- 修改 Python：用 `⌘Q` 退出 Dev，然后再次打开启动脚本，即可运行最新源码。
- 修改 Swift：退出 Dev，再次运行启动脚本，会自动重新编译外壳。
- macOS 13.3 及以上可使用 Safari 的「开发」菜单检查 Dev 的 WebView。

Dev 默认沿用正式版的本机配置、手记和课程资料，因此编辑会保存到同一资料库。
自动测试使用下方的临时目录环境变量；日常开发默认使用 Dev，发布或验收正式安装包时再完整构建。

### 正式安装包

在 Apple Silicon Mac 上准备现有 `.venv-course` 环境和项目依赖，并安装桌面构建依赖：

```sh
uv pip install --python .venv-course/bin/python pyinstaller fastapi uvicorn PyNaCl python-multipart
.venv-course/bin/python scripts/build_desktop.py
```

需安装 Xcode Command Line Tools。构建会将 Python、ASR/OCR 依赖、SenseVoice 模型、VAD 模型、FFmpeg 和离线排版资源打包，不把本机账号、手记或课程数据库打入应用。

产物：

```text
dist/desktop/丰饶之海.app
dist/desktop/丰饶之海-macOS-arm64.zip
```

仅修改 Swift 外壳或前端资源时，可运行 `scripts/build_desktop.py --skip-backend` 复用已打包的后端。后端 Python 代码或依赖变更时需完整构建。

测试时使用 `SEA_DISABLE_KEYCHAIN=1`，并将 `ICOURSE_WEB_CONFIG_DIR` 与 `ICOURSE_WEB_CACHE_DIR` 指向临时目录，避免读取个人配置。
