# 安全说明

真实微信会话请求、Cookie、token、数据库、日志、抓包证据、文章正文、媒体 URL 和导出结果均属于私有运行数据。仓库只保留代码、占位符模板、公开来源清单和脱敏文档。

运行前请复制 `config/*.private.example.txt` 为本地私有文件，并在提交前检查：

```bash
git status --short
git check-ignore -v data/cssci_wechat.db output logs config/wechat-sessions.private.txt
```

如发现会话凭据、抓包文件或研究数据进入 Git、压缩包、日志或远程服务，应立即退出相关登录、撤销或更新会话、清理暴露副本，并通过私密渠道联系仓库维护者。不要在公开 Issue 中粘贴 Cookie、token、原始请求或正文数据。

Fiddler Classic 和旧版微信辅助工具均属于外部本地工具，仓库不分发其安装目录、可执行文件或修改版本。
