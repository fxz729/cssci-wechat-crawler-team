# CSSCI 微信公众号历史采集器

本仓库的范围、公开发布边界，以及 Fiddler Classic 与低版本工具说明见 [`REPOSITORY_OVERVIEW.md`](REPOSITORY_OVERVIEW.md)。

本项目在**合法授权范围内**，按 `data/sources/CSSCI来源刊公众号主页链接.md` 中的固定名单，采集微信公众号可访问的历史文章的目录、正文、正文图片和六项互动指标：阅读、点赞、在看、收藏、分享、评论总数。

它**不会**自动登录、扫码、绕过验证码、绕过权限或风控；**不会**使用代理池、IP 轮换或并发会话轰击。请只对你有权限访问、且已获研究授权使用的数据运行。

> 本项目为团队内部研究与交付使用，不是通用爬虫框架。默认单进程、单 SQLite 写者、顺序账号处理。

---

## 目录

- [1. 采集原理](#1-采集原理)
- [2. 目录结构](#2-目录结构)
- [3. 环境要求](#3-环境要求)
- [4. 快速开始](#4-快速开始)
- [5. 安装](#5-安装)
- [6. 配置](#6-配置)
- [7. 运行：服务端授权会话路线](#7-运行服务端授权会话路线)
- [8. 运行：桌面客户端 + Fiddler 路线](#8-运行桌面客户端--fiddler-路线-windows)
- [9. 部署](#9-部署)
- [10. 测试与自检](#10-测试与自检)
- [11. 常见问题](#11-常见问题)
- [12. 安全边界与团队分发](#12-安全边界与团队分发)
- [13. 许可](#13-许可)

---

## 1. 采集原理

### 1.1 采集链路

```text
目标主页链接 -> Account.biz/fakeid -> appmsgpublish 发布批次
-> 官方文章页 -> 刷新文章级 appmsg_token
-> getappmsgext + appmsg_comment -> SQLite -> CSV/JSON
```

`appmsgpublish` 的每页是**发布批次**；一个批次可展开多篇文章。程序以 `(biz, mid, idx)` 去重，分页游标按发布批次数推进，不会因附文 `idx=2` 跳页。

### 1.2 互动字段映射

| 输出字段 | 微信响应字段 |
| --- | --- |
| 阅读 | `read_num` |
| 点赞 | `old_like_num` |
| 在看/推荐 | `like_num` |
| 收藏 | `collect_num`，缺失时才回退 `star_num` |
| 分享 | `share_num` |
| 评论 | `appmsg_comment.total_count` |

`0` 是有效数值；空值表示接口没有返回该字段。HTTP 200 或 `base_resp.ret=0` 但缺少 `appmsgstat` 会被视为**会话降级**，不会写入空指标快照。

---

## 2. 目录结构

```text
cssci-wechat-crawler-team/
├── main.py                  # 全部 CLI 子命令入口
├── requirements.txt         # Python 依赖
├── pyproject.toml           # 项目元数据 + pytest 配置
│
├── config/                  # 配置与私有凭据加载
│   ├── env_loader.py               # 读取 .env.local
│   ├── session_credentials.py      # 解析 wechat-sessions.private.txt
│   ├── journal_request_input.py    # 解析单刊会话请求
│   └── *.private.example.txt       # 凭据模板（复制后使用）
│
├── crawl/                   # 采集主体：目录遍历、文章拉取、会话预检与降级
├── capture/                 # Fiddler 抓包规则与控制面（Classic + MSAA 路线）
├── discovery/               # 搜狗候选发现（旧路线）
├── metrics/                 # 指标 provider、headers 解析、补采队列
├── parsers/                 # 文章 HTML 解析
├── export/                  # CSV / JSON / Markdown / Excel 导出
├── sources/                 # 固定名单与期刊目录解析
├── storage/                 # SQLAlchemy 模型与 Repository
├── scheduler/               # 后台 daemon 调度
├── utils/                   # 日志、进度、运行锁、请求节流
│
├── scripts/                 # 运维脚本（PowerShell 为主）
├── deploy/                  # systemd unit / timer 示例
├── docs/                    # 设计文档与操作手册
├── data/
│   └── sources/             # CSSCI 期刊目录与公众号主页链接（输入数据）
└── tests/                   # pytest 测试
```

---

## 3. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11（桌面客户端路线必须）；Linux 可用于纯 Requests 服务端路线 |
| Python | 3.10 及以上（推荐 3.12） |
| 核心依赖 | `requests`、`sqlalchemy`、`beautifulsoup4`、`lxml`、`loguru`、`pydantic`、`openpyxl` |
| 浏览器回退 | `playwright` + `playwright-stealth`，需额外安装 Chromium |
| 桌面客户端路线 | 微信 PC 客户端（旧版）、Fiddler Classic、已放行的本机抓包规则 |
| 磁盘 | 按采集规模预留；`output/` 会持续增长 |

两条路线可以独立使用：

- **服务端授权会话路线**：只需 Python，可在 Linux 无人值守运行（第 7 章）。
- **桌面客户端 + Fiddler 路线**：需要 Windows + 微信客户端 + Fiddler，用于抓取客户端内的历史目录与正文（第 8 章）。

---

## 4. 快速开始

```text
1. 解压代码，创建虚拟环境            → 验证：python -V 输出 3.10+
2. 安装依赖（+ Chromium 若用回退）   → 验证：python -c "import requests, sqlalchemy, bs4, openpyxl"
3. 复制凭据模板并填入自己的会话       → 验证：文件存在于 config/，且未提交到 Git
4. 单账号会话预检                    → 验证：validate-sessions 输出 usable
5. 单账号试跑（--max-accounts 1）    → 验证：crawl-status 文章数 > 0
6. 逐步扩大账号范围                  → 验证：每刊完成后可 export
```

不要跳过第 4、5 步直接批量运行。

---

## 5. 安装

### 5.1 Windows（PowerShell，项目根目录）

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果使用 Browser 回退模式，再安装 Chromium：

```powershell
.\.venv\Scripts\playwright.exe install chromium
```

仅使用 Requests 模式时不需要安装浏览器。

如果机器没有 `py -3.12`，先安装 Python 3.10 以上版本，再把第一行换成可用的 `py -3.x`。

### 5.2 Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

---

## 6. 配置

### 6.1 私有会话文件（必需）

复制模板：

```text
config/wechat-sessions.private.example.txt  ->  config/wechat-sessions.private.txt
```

每个会话使用一个 `### session:` 块，也可直接粘贴未经整理的原始抓包文本。

```text
### session: desktop-01
GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?...&fingerprint=...&token=... HTTP/1.1
User-Agent: <后台请求的原始 User-Agent>
Cookie: slave_sid=...; data_ticket=...; wxuin=...

GET https://mp.weixin.qq.com/mp/geticon?biz=...&mid=...&idx=1 HTTP/1.1
User-Agent: <同一会话的原始 User-Agent>
Cookie: appmsg_token=...; wap_sid2=...; pass_ticket=...; wxtokenkey=777; devicetype=UnifiedPCWindows; version=f2541b36
```

要点：

- 程序以 `appmsgpublish` 提供后台 `token`、`fingerprint` 和 User-Agent，并**只**在同一 `### session:` 标签内合并后续请求的 Cookie。
- 不同标签的 Cookie **绝不混拼**；后出现的同名 Cookie 值覆盖旧值。
- 不要复制响应体。
- 最终 Cookie 至少需要 `slave_sid`、`data_ticket`、`appmsg_token`、`wap_sid2`、`pass_ticket`、`wxuin`；建议保留 `wxtokenkey`、`devicetype`、`version`。
- 字段齐全不代表会话有效，仍必须执行 `validate-sessions`。

可以直接把抓包导出的 `4_Full.txt` 作为 `--sessions-file`。一个文件可包含多个独立登录会话：

| 来源请求 | 读取字段 | 用途 |
| --- | --- | --- |
| `/cgi-bin/appmsgpublish` | URL 中的 `fakeid`、`token`、`fingerprint`；原始 `User-Agent`、`Cookie` | 匹配目标公众号、读取历史目录 |
| 同一会话的文章页或 `/mp/geticon` | `appmsg_token`、`wap_sid2`、`pass_ticket` 等补充 Cookie | 刷新文章会话、采集指标与评论数 |

`fakeid` 即公众号主页链接中的 `__biz`。程序只会把会话用于其抓包时关联的 `fakeid`；同一文件内的多个会话按顺序作为**失效接替候选**，不会并发请求。

限制文件权限（Windows）：

```powershell
icacls config\wechat-sessions.private.txt /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

Linux：

```bash
chmod 600 config/wechat-sessions.private.txt
```

### 6.2 环境变量 `.env.local`（可选）

`main.py` 启动时会自动读取项目根目录的 `.env.local`（已加入 `.gitignore`）。用于 `import-profile-ext` 等读取环境凭据的低层命令。已支持的环境变量：

```text
WECHAT_COOKIE
WECHAT_USER_AGENT
WECHAT_APPMSG_TOKEN
WECHAT_PASS_TICKET
WECHAT_UIN
WECHAT_KEY
WECHAT_MP_TOKEN
WECHAT_FAKEID
WECHAT_FINGERPRINT
WECHAT_REFERER
WECHAT_ARTICLE_PAGE_SESSION
```

设置 `CSSCI_SKIP_LOCAL_ENV=1` 可跳过读取。正式批量入口（`crawl-history`）使用 `--sessions-file`，不依赖本文件。

### 6.3 数据库与输出目录

- 默认数据库：`data/cssci_wechat.db`；可用 `--db sqlite:///路径` 覆盖。
- 每位队友应使用**独立数据库和独立输出目录**，避免同名冲突。
- `data/`、`logs/`、`output/` 均在 `.gitignore` 内，不要提交。

---

## 7. 运行：服务端授权会话路线

以下命令均在项目根目录执行。`<目标__biz>` 从目标主页链接 `...__biz=...` 中取得。

### 7.1 会话预检

```powershell
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db validate-sessions `
  --sessions-file config\wechat-sessions.private.txt `
  --fakeid <目标__biz> `
  --transport auto
```

校验会依次验证后台目录、文章页刷新 token、`appmsgstat` 和评论总数；输出只含会话标签与状态。

### 7.2 首次单账号试跑

```powershell
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db crawl-history `
  --sessions-file config\wechat-sessions.private.txt `
  --journal-links data\sources\CSSCI来源刊公众号主页链接.md `
  --max-accounts 1 `
  --transport auto
```

`auto` 会优先使用 Requests；运行中出现认证失效、限流、后台拒绝、登录页或缺少 `appmsgstat` 时，程序会暂停 checkpoint，并用同一候选会话尝试 Browser，再继续同一 checkpoint。`--headed` 可让 Browser 回退显示窗口，便于本机排查；常规运行不需要。

### 7.3 扩大范围

```powershell
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db crawl-history `
  --sessions-file config\wechat-sessions.private.txt `
  --journal-links data\sources\CSSCI来源刊公众号主页链接.md `
  --max-accounts 10 `
  --transport auto
```

默认顺序执行，每篇文章链之间保守等待 3 秒。多个会话仅用于失效接替和授权范围补充，不用于并发或提高请求频率。

### 7.4 实时日志与断点

`crawl-history` 默认把同一组事件实时输出到终端，并同步写入按日日志 `logs/crawler_YYYY-MM-DD.log`：

```text
[账号 1/10] 开始抓取 | account=期刊名
会话预检结果 | account=期刊名 | status=usable | transport=requests
目录页 | account=期刊名 | page=1 | begin=0 | count=5 | total=1032
[文章 1] 已保存 | account=期刊名 | 目录总量=1032 | title=文章标题 | 正文字符=5787 | 图片=2 | 指标=6/6
```

`目录总量` 是微信后台返回的发布目录数量；一个发布批次可能展开多篇文章，因此它不是严格的文章总篇数。

每篇文章完整写入后立即提交 checkpoint，并记录当前目录偏移、已处理数量和状态。日志在最终写入边界统一移除完整 URL，并遮蔽 `credential`、Cookie、token、`fakeid`、`fingerprint`、`pass_ticket`、`appmsg_token` 等敏感键值。

### 7.5 恢复、重试、状态、导出

```powershell
# 恢复中断任务
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db crawl-history `
  --sessions-file config\wechat-sessions.private.txt `
  --journal-links data\sources\CSSCI来源刊公众号主页链接.md `
  --max-accounts 10 --transport auto --resume

# 查看进度
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db crawl-status

# 重试普通单篇失败
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db retry-failed `
  --sessions-file config\wechat-sessions.private.txt `
  --journal-links data\sources\CSSCI来源刊公众号主页链接.md --max-accounts 10

# 导出
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db export --format csv --output output\articles.csv
.\.venv\Scripts\python.exe main.py --db sqlite:///data/cssci_wechat.db export --format json --output output\articles.json
```

图片 URL 保留在数据库内部供显式下载阶段使用；默认导出不下载图片，以避免正文采集时引入额外网络副作用。

> 历史命令 `import-from-headers`、`import-profile-ext`、`import-backend-publish` 保留为迁移/低层命令，不是正式批量入口。`crawl-account` 和 `crawl-batch` 仍是旧的搜狗路线，不能替代授权会话链。

### 7.6 会话期限与故障处理

微信不为这些网页登录字段提供固定有效期。实测 Cookie 的名义时间可能约数天，但退出/重新登录、扫码、设备或出口 IP 变化、风控与权限变化都可能提前失效。**以运行时 preflight 为准，而不是依赖固定天数。**

所有会话不可用时，任务保留 checkpoint 并退出。重新捕获请求块、更新私有文件后加 `--resume` 继续。完成实验或正式任务后，退出并重新登录微信公众平台和桌面微信，使已暴露的旧会话失效。

---

## 8. 运行：桌面客户端 + Fiddler 路线（Windows）

该路线通过本机微信 PC 客户端的独立主页窗口配合 Fiddler 抓包，用于获取历史目录、正文与六项指标。**仅限 Windows。**

### 8.1 前置准备

1. 安装并登录旧版微信 PC 客户端。
2. 安装 Fiddler Classic，并放置于项目同级目录（脚本默认 `..\Fiddler\Fiddler.exe`，可用 `-FiddlerExe` 覆盖）。
3. 打开微信“搜一搜”窗口，保持不关闭；Fiddler 不必保持前台。
4. 首次运行前执行捕获规则安装：

```powershell
.\.venv\Scripts\python.exe -m capture.setup --fiddler-dir ..\Fiddler --output-dir output\pilot_20260906 --target-biz <目标__biz>
```

### 8.2 主要运维脚本

```powershell
# MSAA / Fiddler 名称搜索一键生产入口：主采集 → 失败重试 → 独立补采 → 中文总表导出
.\scripts\start_full_126_production.ps1 -Start 1 -MediaWorkers 4 -WaitSeconds 45

# 单轮采集调试入口（不含失败重试与补采收口）
.\scripts\start_msaa_full_run.ps1

# 通用客户端轮转采集（可指定 phase）
.\scripts\start_full_run.ps1            # capture-first
.\scripts\start_background_only.ps1     # 仅后台素材
.\scripts\start_capture_first.ps1       # 先抓目录再补正文

# 温和停止（当前文章到安全检查点后暂停，不强杀进程）
.\scripts\stop_full_run.ps1

# 查看运行状态
.\scripts\show_full_run_status.ps1
.\scripts\check_full_run.ps1
```

单刊会话路线（使用 `config/journal-session.private.example.txt` 模板）：

```powershell
.\scripts\run_journal.ps1                 # 运行/继续
.\scripts\status_journal.ps1              # 状态
.\scripts\pause_journal.ps1               # 暂停
.\scripts\stop_journal.ps1                # 停止
```

### 8.3 输出位置

默认运行目录为 `output/rollout/full_126/`，其中：

```text
output/rollout/full_126/
├── delivery/文章数据.csv      # 主交付 CSV
├── delivery/媒体索引.csv      # 媒体相对路径索引
├── delivery/附件/             # 图片等媒体附件
├── journals/<序号>_<刊名>/    # 每刊的 bundle、msaa 状态与中间产物
├── logs/                      # 运行日志
└── run_manifest.json          # 运行清单
```

采集完成后可导出单刊中文总表：

```powershell
.\.venv\Scripts\python.exe main.py export-client-csv --bundle output\rollout\full_126\journals\<刊目录>
.\.venv\Scripts\python.exe main.py audit-client-bundle --base output\rollout\full_126
```

详细操作要点见 [docs/微信公众号自动采集使用说明.md](docs/微信公众号自动采集使用说明.md) 与 [docs/full-run-operator-guide.md](docs/full-run-operator-guide.md)。

---

## 9. 部署

### 9.1 Windows 常驻

推荐用任务计划程序调用 `scripts\start_full_126_production.ps1`，并注意：

- 单实例运行：`utils/runtime_lock.py` 已提供运行锁，但同一输出目录仍不应并发启动两个采集进程。
- 不配置“失败即无限重启”；会话失效属于需要人工重新捕获的状态，反复重启只会加重风控。
- 日志写入 `output/rollout/full_126/logs/` 与 `logs/`，定期归档，不要把私有会话文件混入归档。

### 9.2 Linux systemd

仅在本地单账号 Requests 验证成功后部署。服务器没有桌面微信或 Chrome 也可运行，但本地有效会话迁移到新出口 IP 后**可能失效**，因此必须先做服务器 preflight。

完整步骤见 [docs/server-deployment.md](docs/server-deployment.md)。示例 unit 位于：

```text
deploy/systemd/cssci-wechat-crawler.service.example
deploy/systemd/cssci-wechat-crawler.timer.example
```

```bash
sudo cp deploy/systemd/cssci-wechat-crawler.service.example /etc/systemd/system/cssci-wechat-crawler.service
sudo cp deploy/systemd/cssci-wechat-crawler.timer.example /etc/systemd/system/cssci-wechat-crawler.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cssci-wechat-crawler.timer
journalctl -u cssci-wechat-crawler.service -f
```

service 使用 `flock` 防止 SQLite 多写者。不要配置认证失败时的无限重启。

### 9.3 第一版部署限制

固定出口 IP、原始 User-Agent、单进程、单 SQLite 写者、顺序账号处理；不启用代理池、IP 轮换、Docker 多副本或多 worker。

---

## 10. 测试与自检

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall main.py config crawl capture metrics storage export sources parsers discovery scheduler utils tests
```

已知既有问题：`tests/test_progress_logger.py::test_progress_logger_writes_realtime_status_lines` 在部分环境独立失败，需单独诊断，不能据此跳过其他测试。

---

## 11. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `validate-sessions` 输出 `可用会话：0/N` | 会话已失效或与目标 `fakeid` 不匹配。重新抓取该账号的 `appmsgpublish` 请求块，不要复用旧 Cookie 或更换账号绕过。 |
| 运行中 `ret=-6` / “操作繁忙请稍后重试” | 疑似平台限流。**停止重复启动**，等待访问恢复正常；不要轮换账号或凭据强行重试。 |
| 目录页能翻，文章页/指标为空 | 会话在文章级降级。程序会记录为会话不可用并暂停 checkpoint，`--resume` 续跑即可。 |
| Browser 回退报缺少浏览器 | 执行 `.\.venv\Scripts\playwright.exe install chromium`。 |
| 脚本提示找不到 Python | `scripts\run_journal.ps1` 接受 `-Python` 参数；请传入自己的解释器路径，或改用第 7 章的 `main.py` 命令。 |
| 中文文件名在 Linux 上乱码 | 确认 locale 为 UTF-8，并使用项目自带的 `main.py`（启动时会强制 UTF-8 输出）。 |
| SQLite `database is locked` | 同一数据库被并发写入。确认只有一个采集进程；systemd 场景确认 `flock` 生效。 |

---

## 12. 安全边界与团队分发

发给队友的压缩包**只应**包含代码、`requirements.txt`、公开来源 Markdown、`config/*.private.example.txt` 和文档。

每位队友**自行**创建 `config/wechat-sessions.private.txt` 并使用**自己已获授权**的登录会话；不要共享、合并或通过聊天工具发送真实 `4_Full.txt`、`*_Headers.txt`、Cookie、token、`data/*.db` 或抓取结果。

打包前必须排除：

```text
.venv/
.pytest_cache/
__pycache__/
config/*.private.txt
config/*.private.json
.env  .env.local  .env.*
*.db  *.sqlite  *.sqlite3
data/raw_html/  data/raw_text/  data/verify_*/
logs/
output/
*.bak*
*_Headers.txt
```

队友收到压缩包后：解压 → 按第 5 章创建 `.venv` 并安装依赖（和 Chromium）→ 复制凭据模板为私有文件 → 粘贴自己的同会话请求块 → 执行一次 `validate-sessions` → 从 `--max-accounts 1` 开始采集。完成任务后应退出相关微信登录以使旧会话失效。

**不得**将私有会话文件放入 Git、云同步目录、镜像、数据库、日志、导出或备份。逐篇刷新出的 `appmsg_token` 只留在内存。

---

## 13. 许可

仅限学术研究和已授权的数据采集。
