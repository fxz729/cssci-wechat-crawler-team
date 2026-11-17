# Linux 服务器部署

仅在本地单账号 Requests 验证成功后部署。服务器没有桌面微信或 Chrome 也可运行，但本地有效会话迁移到新出口 IP 后可能失效，因此必须先做服务器 preflight。

## 安装

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin cssci-crawler
sudo install -d -o cssci-crawler -g cssci-crawler /opt/cssci-wechat-crawler /var/lib/cssci-wechat /var/log/cssci-wechat
sudo -u cssci-crawler python3 -m venv /opt/cssci-wechat-crawler/.venv
sudo -u cssci-crawler /opt/cssci-wechat-crawler/.venv/bin/pip install -r /opt/cssci-wechat-crawler/requirements.txt
```

将项目代码放到 `/opt/cssci-wechat-crawler`，将 SQLite 数据库和输出放到 `/var/lib/cssci-wechat`。会话文件不放进代码目录、容器镜像或备份：

```bash
sudo install -d -m 700 -o cssci-crawler -g cssci-crawler /etc/cssci-wechat
sudo install -m 600 -o cssci-crawler -g cssci-crawler \
  /path/to/wechat-sessions.private.txt /etc/cssci-wechat/wechat-sessions.private.txt
```

## 预检与试跑

```bash
sudo -u cssci-crawler /opt/cssci-wechat-crawler/.venv/bin/python main.py \
  validate-sessions \
  --sessions-file /etc/cssci-wechat/wechat-sessions.private.txt \
  --fakeid <__biz>

sudo -u cssci-crawler /opt/cssci-wechat-crawler/.venv/bin/python main.py \
  --db sqlite:////var/lib/cssci-wechat/cssci_wechat.db \
  crawl-history \
  --sessions-file /etc/cssci-wechat/wechat-sessions.private.txt \
  --max-accounts 1 --resume
```

如果预检失败，不要反复重试或改用代理。重新在与服务器出口条件匹配的正常授权环境中捕获会话，再做单账号验证。

## systemd

复制示例文件：

```bash
sudo cp deploy/systemd/cssci-wechat-crawler.service.example /etc/systemd/system/cssci-wechat-crawler.service
sudo cp deploy/systemd/cssci-wechat-crawler.timer.example /etc/systemd/system/cssci-wechat-crawler.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cssci-wechat-crawler.timer
```

service 使用 `flock` 防止 SQLite 多写者。不要配置认证失败时的无限重启；更新私有会话文件后由下一次 timer 或人工启动恢复。

查看日志：

```bash
journalctl -u cssci-wechat-crawler.service -f
```

日志中不得出现 Cookie、token、完整请求 URL、文章 HTML 或身份字段。备份数据库前确认备份系统不包含 `/etc/cssci-wechat/`。
