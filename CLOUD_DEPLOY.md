# 云服务器部署

代码、原版段位素材、数字字体、牌风基准和浏览器适配器均包含在仓库中；
不需要从 Mac 的 Downloads 或临时目录复制源码。
`.env`、数据库、QQ 登录状态、浏览器登录状态和 Mortal 任务不提交 Git。

## 首次部署：Python

建议使用 Debian/Ubuntu、Python 3.11 及以上。安装中文字体，避免图片缺字：

```bash
sudo apt-get update
sudo apt-get install -y git python3-venv fonts-noto-cjk
git clone https://github.com/ActorBlack/pupulinbot.git
cd pupulinbot
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
cp .env.example .env
```

在服务器本地编辑 `.env`，填入 `PUPULIN_PAIPUYA_API_TOKEN`、OneBot 的
`ONEBOT_ACCESS_TOKEN`、`SUPERUSERS` 和 `PUPULIN_MONITOR_OWNER` 等实际配置。
API 域名保持 `https://5-data.amae-koromo.com/api/v2/pl4` 和 `/pl3`，使用标准 Bearer，
三麻、四麻、查询、监控和重试共用最多 1 QPS 的限速器。

维持当前仅结算监控、不打开雀魂网页的设置：

```dotenv
PUPULIN_MONITOR_NOTIFY_START=false
PUPULIN_SPECTATOR_BROWSER=false
```

接好 NapCat 等 OneBot 11 服务，将反向 WebSocket 指向
`ws://服务器地址:8080/onebot/v11/ws`，双方使用相同的访问令牌。
如果中间使用 HTTPS 反向代理，改用对应的 `wss://` 地址。
启动 Bot：

```bash
.venv/bin/python bot.py
```

正式常驻运行可使用已有的 systemd 或容器服务；工作目录必须是项目目录，
以便读取 `.env` 和相对路径 `data/`。Bot 本身不包含 QQ 客户端或扫码登录服务。

## Mortal 在线跑谱

使用外部 Mortal HTTP 桥时配置 `PUPULIN_MORTAL_API_URL` 即可。
使用浏览器在线引擎时，还需要 Chrome/Chromium 及虚拟显示：

```bash
sudo apt-get install -y chromium xvfb xauth
.venv/bin/python -m pip install '.[online]'
```

并在 `.env` 中设置：

```dotenv
PUPULIN_MORTAL_ONLINE=true
PUPULIN_MORTAL_ONLINE_HEADLESS=true
PUPULIN_MORTAL_ONLINE_CONCURRENCY=2
PUPULIN_MORTAL_BROWSER_PROFILE=data/mortal-chrome
```

Linux 会使用 Xvfb。首次运行可能下载匹配的浏览器驱动，需要服务器能够联网。
站点验证、限流及服务器排队仍可能影响任务；可用 `mortal status <任务编号>` 查看进度。
浏览器配置和驱动需在服务器重新准备，不复用 Mac 的运行进程或浏览器配置目录。

## Docker 基础部署

配置好 `.env`、安装 Docker Compose 后：

```bash
docker compose up -d --build
docker compose logs -f bot
```

默认镜像包含查询、图表和监控依赖，`data/` 持久化挂载；默认不包含 Chrome 或
在线跑谱可选依赖。需要完整浏览器引擎时，可采用上面的 Python 部署方式，
或自行在镜像中安装浏览器、Xvfb 和 `.[online]`。

## 更新及迁移现有数据

已经部署的服务器在项目目录执行：

```bash
git pull --ff-only origin main
.venv/bin/python -m pip install .
# 启用了浏览器引擎时改为：.venv/bin/python -m pip install '.[online]'
```

随后由服务器运维人员重启对应的服务。Docker 部署更新代码后执行
`docker compose up -d --build`。不要在更新时用 `.env.example` 覆盖已有 `.env`。

迁移群绑定和监控名单时，先停止旧 Bot，再备份并安全传输 `data/pupulin.db`；
SQLite 如果仍在运行，应使用 SQLite backup，而不是单独复制可能带 WAL 的数据库文件。
需要保留跑谱任务和报告时，一并安全传输 `data/batches/`。
数据库升级自动迁移，无需删库。服务群名单、超级用户及所有者配置仍须在服务器 `.env` 中保留。

同一账号和数据集只运行一个 Bot 实例。迁移完成前不要让新旧实例同时处理群命令、
跑谱队列和监控，以免重复回复、重复提交以及让同一 API Key 的总请求超过 1 QPS。

## 本版本主要变化

- 雀魂、天凤的群内独立关注及监控、查询与图表；英文帮助及命令。
- Bearer API Key、跨进程 1 QPS、超时重试及掉线补查。
- Mortal 批任务、双浏览器、恢复及评分图片；雀魂、天凤和一番街链接。
- 统一雀魂段位显示：普通段位使用原版亮暗花星；魂天使用蓝色 Lv 和魂珠图标。
  `900` 内部魂天分显示为 `9.0 魂珠`，原始结算 `-20` 显示为 `魂珠 -0.2`。
  跨段位历史的 PT 与魂珠分别统计。
- 四麻玉间豪2至圣3真实均值校准的补充牌风标签，详见 [规则表](STYLE_TAG_RULES.md)。
