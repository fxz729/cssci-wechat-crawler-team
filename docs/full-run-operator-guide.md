# 微信公众号 126 刊全量运行操作手册

更新日期：2026-09-07

## 当前放行状态

**尚未放行全量。** 离线代码与输出校验已通过，但 2026-09-07 的现场只读预检没有发现已打开的微信“搜一搜”网页窗口，同时未发现 Fiddler 进程。必须完成有限现场跨刊、断点恢复和新目录完整性验证后，才能把本手册中的全量命令交给最终运行。

## 最终运行前用户只需准备

1. 登录微信 Windows 客户端；
2. 打开 Fiddler Classic，并保持项目已经安装的捕获脚本不变；
3. 在微信中打开“搜一搜”网页窗口并保持打开；
4. 不需要填写 Cookie、Authorization、token、pass_ticket 或窗口句柄。

程序不会关闭搜一搜，不会向聊天输入框写字，不会发送消息，也不会自动点赞、收藏、评论或分享。

## 四个操作入口

### 1．只读健康检查

```powershell
cd "G:\论文\王\微信公众号自动化\cssci-wechat-crawler-team"
.\scripts\check_full_run.ps1
```

检查内容：固定名单恰为 126 刊、唯一搜一搜输入框、有效 HWND、Fiddler 脚本及捕获控制状态。该命令不启动采集。

### 2．启动或从断点继续

```powershell
.\scripts\start_full_run.ps1
```

默认运行目录：

```text
output\rollout\full_126
```

电脑重启后仍使用同一命令和同一运行目录；已通过完整性校验的文章不会重抓。系统锁会拒绝第二个并发实例。

### 3．查看状态

```powershell
.\scripts\show_full_run_status.ps1
```

输出包括：各期刊状态计数、当前正在运行的期刊、当前文章、尝试次数、文章状态计数及是否已请求停止。

### 4．温和停止

```powershell
.\scripts\stop_full_run.ps1
```

该命令只原子写入：

```text
output\rollout\full_126\run_control.json
```

程序在当前文章的安全检查点暂停，不强杀 Python、微信或 Fiddler。再次运行启动脚本会在取得系统锁后重置停止请求并继续。

## 输出结构

每刊目录：

```text
output\rollout\full_126\journals\NNN_期刊名\
├─ catalog_verified_snapshot.json
├─ capture_state.json
├─ catalog_identity.json
├─ network_events.jsonl
├─ refill_queue.json                 # 运行独立补采后生成
├─ refill_report.json                # 运行独立补采后生成
└─ bundle\
   ├─ 文章数据.csv
   ├─ 媒体索引.csv
   ├─ journal.db
   └─ articles\
      └─ mid_idx\
         ├─ metadata.json
         ├─ capture_evidence.json
         ├─ article.html
         ├─ article.txt
         ├─ content_fragment.html
         └─ images、audio、video 等媒体目录
```

CSV 使用 UTF-8 BOM、完整字段不截断。指标空值保持为空，不写成 0。六项指标只接受真实响应字段：

```text
read_count      ← $html.cgiData.read_num
like_count      ← $html.appmsg_bar_data.old_like_count
watching_count  ← $html.appmsg_bar_data.like_count
share_count     ← $html.appmsg_bar_data.share_count
favorite_count  ← $html.appmsg_bar_data.collect_count
comment_count   ← $html.appmsg_bar_data.comment_count
```

## 单刊独立补采

只补冻结目录中没有通过完整性校验的文章、六项指标或媒体；不重新扫描完整目录，不使用 `--force` 覆盖已完成文章：

```powershell
python -X utf8 -B main.py refill-client-journal `
  --base "output\rollout\full_126\journals\001_期刊名" `
  --pilot-dir "output\pilot_20260906" `
  --biz "该刊biz" `
  --account "公众号名称"
```

只进行后台媒体、CSV 和审计，不控制微信：

```powershell
python -X utf8 -B main.py refill-client-journal `
  --base "单刊目录" `
  --pilot-dir "output\pilot_20260906" `
  --biz "该刊biz" `
  --account "公众号名称" `
  --offline-only
```

若仍有文章或指标需要客户端，离线模式返回码为 2，并写入 `refill_report.json`。

## 状态含义

- `pending`：尚未开始；
- `running`：目录或文章采集中；
- `media_running`：媒体整理中；
- `paused`：用户停止、客户端缺失、媒体未完成或其他可恢复错误；
- `needs_verification`：数据存在，但严格目录／基线／CSV／证据审计未通过；
- `completed`：严格审计全部通过，且 `completed_at` 有效。

旧完成声明一旦被新审计否定，会移入 `prior_completion_claim` 留痕，不能继续作为完成证据。

## 正式两阶段运行模式

`start_full_run.ps1` 现在按以下顺序执行：

1. `capture-first`：若 Fiddler 未运行，脚本从 `..\Fiddler\Fiddler.exe` 隐藏启动；随后验证唯一搜一搜窗口，逐刊冻结完整目录并逐篇缓存脱敏后的文章事件与六项指标事件。
2. 只有 126 刊缓存全部完整时，才立即进入 `background-only`。
3. `background-only` 不检查、不导入也不控制微信、搜一搜或 Fiddler；它从本地缓存建库、生成正文文件、下载媒体、导出两个 CSV 并运行严格审计。

单独运行前台缓存：

```powershell
.\scripts\start_capture_first.ps1
```

单独运行后台物化：

```powershell
.\scripts\start_background_only.ps1
```

一键连续运行：

```powershell
.\scripts\start_full_run.ps1
```

新增本地缓存产物：

```text
capture_cache\articles\000000\capture_evidence.json
capture_cache_state.json
capture_cache_summary.json
background_refill_queue.json
background_report.json
```

缓存只写入 `capture.reader.to_wire_event()` 产生的已验证字段；不保存 Cookie、Authorization、token、uin、key、pass_ticket 或原始请求头。缓存完整身份为 `(biz, mid, idx, sn)`。六项指标缺失时返回码为 2，保留待补采索引，绝不把空值改写为 0。