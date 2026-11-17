# CSSCI WeChat Crawler Team

这是一个面向 CSSCI 来源刊公众号的授权会话采集项目，支持公众号历史目录、文章正文、图片索引和阅读/点赞/在看/收藏/分享/评论等指标采集，并提供 SQLite 存储、断点续跑、会话预检、CSV/JSON/Markdown 导出和 Windows 桌面客户端辅助路线。

## 项目定位

- 适用场景：经过授权的学术公众号历史文章整理与研究数据生产。
- 服务端路线：使用同一授权会话的 Requests/Browser 访问链，单进程、单 SQLite 写者、顺序账号处理。
- 桌面路线：在 Windows 上配合已安装的微信 PC 客户端和 Fiddler Classic，捕获经过授权的公开请求，再由项目解析和落盘。
- 数据质量：按 `(biz, mid, idx)` 去重，保留 checkpoint，区分会话降级、失败和成功，默认不在日志中保存完整 URL 或凭据。

## 目录重点

| 路径 | 作用 |
| --- | --- |
| `main.py` | CLI 子命令入口 |
| `crawl/` | 目录遍历、文章拉取、会话预检与桌面捕获 |
| `capture/` | Fiddler Classic、MSAA/UIA 和抓包清洗辅助 |
| `metrics/` | 互动指标 provider 与补采队列 |
| `parsers/` | 文章 HTML 与 URL 解析 |
| `storage/` | SQLAlchemy 模型和 Repository |
| `export/` | CSV、JSON、JSONL、Markdown 导出 |
| `scripts/` | Windows PowerShell 运维脚本 |
| `deploy/` | Linux systemd 示例 |
| `data/sources/` | CSSCI 目录与公众号主页链接等公开输入 |
| `config/*.private.example.txt` | 会话输入模板，不含真实凭据 |
| `tests/` | pytest 测试与安全回归 |

## Fiddler 与低版本工具

- **Fiddler Classic**：可选的 Windows 抓包工具，用于在本机微信客户端路线中观察经过授权的请求。仓库只保留脚本接口、规则和清洗逻辑；Fiddler 安装目录、可执行文件、DLL、PDB 和安装包不随仓库发布。请从官方或机构批准的来源自行安装，并按项目文档配置。
- **低版本工具**：同级目录中的旧版微信辅助工具属于本地兼容性材料，用于特定客户端版本的登录/兼容测试。它包含可执行文件与逆向分析产物，仓库不打包、不分发，也不把它当作运行时依赖。使用前应确认软件来源、授权和终端安全策略。
- 两类工具都不能用于绕过验证码、权限、登录保护或平台风控；采集应限定在已获授权的数据范围内。

## 快速开始

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall main.py config crawl capture metrics storage export sources parsers discovery scheduler utils
```

使用服务端路线时，先复制 `config/wechat-sessions.private.example.txt` 为私有会话文件，并完成 `validate-sessions`；使用桌面路线时，先安装并登录已授权的微信客户端，再按 `README.md` 第 8 章配置 Fiddler。先从单账号、小样本开始，确认 checkpoint 和导出结果后再扩大范围。

## 数据与合规

真实会话请求、Cookie、token、数据库、日志、输出和媒体文件均属于本地私有数据，不应提交到 Git 或同步到公开存储。仅对有权访问和研究使用的数据运行，并遵守微信、所在机构和适用法律的条款。
