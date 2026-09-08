# 统一控制台

`local_web/static/` 是唯一界面源码。本地服务直接提供该目录，GitHub Pages 也从这里打包，
保留深色主题、课程卡片上的分区下拉和相同的导航与操作。

| 能力 | 本地 | GitHub Pages |
| --- | --- | --- |
| 课程、课次、置顶、四个分区 | 支持 | 支持 |
| 摘要、转录、OCR、模型版本对比 | 支持 | 支持 |
| 按课程及内容类型筛选、分页搜索 | 支持 | 支持 |
| 订阅、单次运行、导出、删除、运行状态 | 支持 | 支持 |
| 模型配置、API Key 保存、单个和批量重跑 | 支持 | 支持 |
| 模型连接测试、Obsidian Vault 同步 | 支持 | 使用本地入口 |
| 凭据 | Python 会话；可选 macOS 钥匙串 | 当前标签页内存，刷新后重新登录 |
| 分区偏好 | 本机配置文件 | 浏览器仓库专属存储 |

## 开发与部署

```bash
# 本地服务
python -m local_web

# 从共享源码生成 Pages 静态站点
python3 scripts/build_frontend.py
python3 -m http.server 8000 --directory dist
# 打开 http://localhost:8000/frontend/
```

Pages 设置选择 GitHub Actions，运行 Deploy Frontend。工作流监测 `local_web/static/`、
构建脚本和部署配置的变化，发布 `dist/frontend/`。HTML、CSS、app.js 和 bootstrap.js
与本地源码完全一致，只将 runtime-config.js 的运行模式设为 `pages`。
相对资源路径兼容 GitHub Pages 的仓库子路径。

`frontend/index.html` 仅用于在仓库静态预览时跳转到共享源码，不再包含独立界面。
不要直接上传这个跳转文件；部署必须使用构建产物。

## 数据与凭据

本地的 `api()` 调用 Python 服务；Pages 的同一调用由 `browser/transport.js` 适配至
WebCrypto、sql.js 和 GitHub API。支持加密分片，以及旧单文件数据库；旧密钥格式的额外
DASHSCOPE/SMTP 字段在连接表单“高级设置”中。所有分片成功读取后才接受新版本，失败时
恢复当前会话上次可用资料库。

Pages 不保存新的 Token、UIS 密码或明文数据库到浏览器持久存储。旧查看器的仓库设置与
置顶继续可用；同仓库的旧订阅快照会迁移。成功连接后移除旧 `ics_creds` 凭据记录。分区和订阅快照按仓库分别保存。
这些偏好不会自动与本地配置文件同步。旧查看器历史 IndexedDB 缓存不会被新版本读取。

GitHub Token 需要 Contents Read、Actions Read and write；修改订阅/API Key 还需要
Secrets Read and write，模型配置需要 Variables Read and write。模型 API Key 在 Pages
浏览器中通过 sealed-box 加密后提交到 GitHub。Secret 明文无法读回，空白 Key 表示保留。

Pages 初次加载需要联网取得 sql.js。两个入口的 Markdown/公式增强组件均按需从 CDN
加载，失败时保留内置安全 Markdown 排版；本地资料库仍可离线打开。

## 验证

```bash
.venv-web/bin/python -m unittest discover -s tests -v
node --check local_web/static/app.js
node --check local_web/static/browser/transport.js
```

`tests/test_unified_console.py` 检查共享打包、默认模型配置及搜索数据契约。
`tests/browser_console.cjs` 用合成加密 SQLite 数据验证两种入口，不连接真实账户或触发真实工作流。

浏览器集成测试需要安装 Playwright（可用 `BROWSER_CHANNEL` 选择 Chrome 等通道），并提供
sql.js 1.12.0 的 `sql-wasm.js` 与 `sql-wasm.wasm`：

```bash
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
SQLJS_DIR=/path/to/sql.js-1.12.0 \
node tests/browser_console.cjs
```
