# 授权会话操作说明

本项目的正式历史采集入口使用私有 `appmsgpublish` 请求块，而不是逐项填写 Cookie、token 或文章参数。

## 捕获方式

1. 在微信 PC 或公众号后台正常登录。
2. 打开后台文章列表。
3. 在开发者工具 Network 中筛选 `appmsgpublish`。
4. 复制一条最新的完整 `appmsgpublish` 请求头块：请求行、`User-Agent`、`Cookie`。
5. 如果该 Cookie 缺少文章会话字段，再复制同一登录、相邻时间的文章页或 `geticon` 请求头块。
6. 不要复制响应体、响应头或文章指标响应。
7. 粘贴到 `config/wechat-sessions.private.txt`，每个独立登录会话使用唯一标签：

```text
### session: desktop-01
GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?...&fingerprint=...&token=... HTTP/1.1
User-Agent: <原始 User-Agent>
Cookie: slave_sid=...; data_ticket=...; wxuin=...

GET https://mp.weixin.qq.com/mp/geticon?biz=...&mid=...&idx=1 HTTP/1.1
User-Agent: <同一会话的原始 User-Agent>
Cookie: appmsg_token=...; wap_sid2=...; pass_ticket=...; wxtokenkey=777; devicetype=UnifiedPCWindows; version=f2541b36
```

解析器以 `appmsgpublish` 作为主请求，并只在同一标签内合并后续请求的 Cookie；不同 `### session:` 标签绝不混拼。字段齐全后仍须通过运行时校验，不能只按字段名判断会话有效。
文件内容只在本机或受控服务器保存，绝不提交、发送、记录到日志、导出或备份。

## 启动校验

```bash
python main.py validate-sessions \
  --sessions-file config/wechat-sessions.private.txt \
  --fakeid <__biz>
```

校验对每个会话执行：后台目录请求、官方文章页、文章级 token 刷新、`getappmsgext` 与评论总数请求。输出只显示会话标签和状态，不显示 Cookie、token、请求 URL 或身份字段。

## 有效期

没有稳定 SLA。登录态可能在数小时到数天内变化，也可能因退出、扫码、账号状态、设备或出口 IP 变化提前失效。不要以“多复制几个相同请求块”延长会话；只有独立、有效的登录会话才可用作顺序接替。

任务完成后，退出并重新登录相关微信环境，废弃曾公开或用于实验的会话。

## 不支持的行为

- 自动登录、自动扫码或自动续期。
- 绕过验证码、权限或风控。
- 代理池、IP 轮换和多会话并发。
- 将会话写入 `.env`、SQLite、CSV、JSON、日志、镜像或备份。
