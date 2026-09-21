# PupulinBot

面向 QQ 群的雀魂助手：基于 **NoneBot2 + OneBot 11**，提供 UID/昵称绑定、牌谱屋查询、群友对局监控，以及可接入自托管 Mortal 的自动复盘和评分卡。项目采用 SQLite，单机即可运行。

> 牌谱数据来自第三方公开索引，通常只覆盖金之间及以上、并可能在结算后才出现；“开局提醒”取决于配置的上游是否返回尚未结束的记录。请合理设置轮询间隔并遵守上游服务条款。

## 功能

- QQ ↔ 雀魂 account ID（常称 UID）绑定，也可以用昵称检索绑定。
- 查询最近牌谱、名次、分数并生成可点击的雀魂牌谱链接。
- 按群订阅成员；定时发现开局和结算，使用数据库原子去重，多群互不影响。
- 结算牌谱自动提交 Mortal 服务；手动 `mortal` 命令支持指定座位。
- 将评分、等级和报告链接绘制成适合 QQ 发送的 PNG 卡片。
- Mortal 队列限流、HTTP 超时、服务错误提示、持久化绑定和订阅。

## 快速开始

### 1. 安装

需要 Python 3.11+。本项目遵循 `src` 布局：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
python bot.py
```

也可以执行 `docker compose up -d --build`。将 go-cqhttp、Lagrange.Core 或 NapCat 的 OneBot 11 反向 WebSocket 指向本 Bot 的 `/onebot/v11/ws`；连接鉴权等 NoneBot 环境变量按所使用的实现配置。

### 2. 配置

所有业务变量以 `PUPULIN_` 开头：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PUPULIN_DATABASE` | `data/pupulin.db` | SQLite 文件 |
| `PUPULIN_PAIPUYA_BASE_URL` | 牌谱屋四麻 API | 可替换为兼容代理/镜像 |
| `PUPULIN_MONITOR_INTERVAL` | `60` | 监控周期（秒，最小 20） |
| `PUPULIN_MORTAL_API_URL` | 空 | Mortal HTTP 桥地址；空时关闭自动分析 |
| `PUPULIN_MORTAL_API_TOKEN` | 空 | 可选 Bearer Token |
| `PUPULIN_MORTAL_TIMEOUT` | `180` | 单次分析超时 |
| `PUPULIN_REVIEW_WORKERS` | `1` | 并行分析数量 |

### 3. Mortal 桥接协议

Mortal 及雀魂日志转换器通常需要较大的模型和独立运行环境，因此 Bot 使用小而稳定的 HTTP 边界，可对接已有 Mortal/akochan-reviewer 部署或自行写适配器：

```http
POST /review
Authorization: Bearer <optional token>
Content-Type: application/json

{"url":"https://game.maj-soul.com/1/?paipu=...","seat":0}
```

成功响应：

```json
{"status":"done","rating":87.52,"rank":"A","url":"https://review.example/result/123"}
```

桥接服务负责下载雀魂牌谱、转换为 mjai、调用 Mortal，并返回汇总指标；这样不会把特定模型版本、GPU 配置或非稳定的雀魂私有协议耦合进 QQ Bot。`seat` 可以为 `null`。错误可返回 `{"status":"error","error":"原因"}`。

## 群命令

| 命令 | 说明 |
| --- | --- |
| `雀魂绑定 <UID或昵称>` | 绑定当前 QQ；昵称重名时优先精确匹配 |
| `雀魂解绑` | 解除绑定及关联订阅 |
| `牌谱屋 [UID或昵称]` | 不带参数时查询本人最近五场 |
| `雀魂监控 开` / `雀魂监控 关` | 在当前群开启或关闭本人的提醒 |
| `mortal <牌谱链接或UUID> [座位0-3]` | 排队分析并发送评分图片 |
| `麻将帮助` | 查看帮助 |

## 开发与安全

```bash
pytest
ruff check .
```

- 请勿提交 `.env`、Token 和数据库；它们已被 `.gitignore` 排除。
- 建议为 Mortal API 配置 Token、TLS 和请求体大小限制。
- 牌谱屋是社区数据源，不等同于雀魂官方 API。生产环境建议使用缓存/代理，并避免过于频繁地轮询。
- UID 绑定仅代表 QQ 用户的声明，并非雀魂账号所有权验证；涉及管理权限或奖品时应增加人工验证。

## 项目结构

```text
bot.py                       # NoneBot 入口
src/pupulinbot/plugin.py     # 命令、调度与消息发送
src/pupulinbot/paipuya.py    # 牌谱屋兼容客户端
src/pupulinbot/review.py     # Mortal 队列、协议与图片
src/pupulinbot/db.py         # SQLite 持久化及通知去重
tests/                       # 核心逻辑测试
```
