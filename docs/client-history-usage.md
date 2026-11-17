# 微信客户端历史文章采集使用说明

## 日常操作

1. 正常登录微信 PC 客户端，并保持公众号历史目录可以显示。
2. 启动 Fiddler Classic，确认监听 `127.0.0.1:8888`。
3. 不复制 Cookie、Authorization 或任何 token 到项目文件。
4. 在项目根目录执行：

```powershell
powershell -NoProfile -File scripts/check_wechat_capture.ps1 -Hwnd 331714
powershell -NoProfile -File scripts/run_wechat_history.ps1 -Hwnd 331714
```

运行器会自动：

- 检查微信浏览器窗口、Fiddler 进程、监听端口和规则归属；
- 按已冻结目录逐篇打开、强制重新加载、捕获并核对真实 `/s` 响应；
- 将六项指标和 `comment_id` 作为响应字段入库；
- 复用或下载媒体原文件；
- 生成文章/媒体 Excel 索引和严格验收报告。

## 指定补采

```powershell
powershell -NoProfile -File scripts/run_wechat_history.ps1 `
  -Hwnd 331714 -Indices '72,89,90' -Attempts 3 -WaitSeconds 90
```

`-Force` 只对明确指定的索引重新捕获；不指定时已完成项会跳过。出现验证码、登录过期或窗口抢焦点失败时，程序会记录暂停状态，不会伪造成功。

## 输出

当前一刊验证输出位于：

```text
output/journal_validation_20260906/
```

关键文件：

- `bundle/文章索引.xlsx`：文章字段和本地链接；
- `bundle/media_rows.json`：媒体原文件清单、状态、字节数和 SHA-256；
- `catalog_identity.json`：目录项与真实文章身份映射；
- `capture_inbox_validated.jsonl`：规范化的安全事件；
- `journal_validation_strict.md/json`：逐项验收结果。

作者或摘要在源响应中未返回时保持空值，并在字段来源中标记为 `not_returned`；不能从标题人名推断。阅读、点赞、在看、分享、收藏和评论数都是采集时点的累计值，不是发表时历史快照。
