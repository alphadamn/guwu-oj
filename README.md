# 洛谷风格 OJ - 在线评测系统

模仿洛谷的在线评测系统，Django 5 + Bootstrap 5 构建，支持分布式判题、多优先级队列、DB-less 测评机与 WebSocket 实时评测状态。

## 功能特性

- **用户**：注册 / 登录、个人资料（头像、昵称、简介）、个人主页（提交记录、已解决题目）
- **题库**：题目列表（难度 / 标签筛选）、题目详情、9 级难度（入门 → NOI）
- **提交**：10 种语言在线评测、提交详情（代码、逐测试点结果、耗时 / 内存）、WebSocket 实时状态（失败自动降级 HTTP 轮询）
- **分布式判题**：Celery + 中央 Redis 单队列多优先级、多机竞争消费、原子抢占 + 租约心跳 + 栅栏写回；测评机可完全无数据库凭证（DB-less）
- **NAT 自愈**：测评机自报公网 IP，Web 侧自动同步 iptables 放行直连端口；直连不可达时回退 CDN
- **其他**：排行榜、比赛、题解、AI 解题讲解、文档搜索、积分与签到、管理后台

## 技术栈

Django 5 · PostgreSQL · Redis（缓存 + Celery broker + pub/sub）· Celery（线程池 worker）· Docker（语言沙箱 + 容器池）· Granian 双服务（WSGI 主站 + 独立 ASGI WebSocket，无 Channels）

## 快速开始（开发）

```bash
git clone https://github.com/alphadamn/guwu-oj && cd guwu-oj
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # 填入 SECRET_KEY / DB / Redis 等
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic

# 构建判题沙箱镜像（一次性，见下表）
./scripts/build-containers.sh

# 三个常驻进程（三个终端）
python manage.py runserver                                                          # 主站
celery -A oj_project worker -Q judge -P threads -c 4 -l info                        # 判题 worker
granian oj_project.asgi:application --interface asgi --host 127.0.0.1 --port 8447 --workers 1  # WS
```

访问 http://127.0.0.1:8000 。环境自检：`python verify_setup.py`；测试：`python manage.py test`。

沙箱按语言拆为 5 个轻量镜像，判题时按提交语言自动选择：

| 镜像 | 语言 | 约大小 |
|------|------|--------|
| `oj-python` | Python | 44MB |
| `oj-c` | C | 98MB |
| `oj-cpp` | C++ | 116MB |
| `oj-java` | Java | 183MB |
| `oj-other` | Go, Rust, JS, Ruby, Kotlin, ASM | 640MB |

## 判题架构

### 中央队列与优先级

所有判题任务投递到中央 Redis 的**单一 Celery 逻辑队列 `judge`**，优先级用 Redis transport 的优先级桶实现，**数字越小越先消费**：

| 优先级 | 用户层级 | priority | 物理桶 |
|--------|----------|:--------:|--------|
| pro | Pro 订阅 | 0 | `judge` |
| plus | Plus 订阅 | 3 | `judge:3` |
| free（默认） | 免费用户 | 6 | `judge:6` |
| ai | AI 讲解的判题验证（专用服务账号，最低优先级） | 9 | `judge:9` |

### 抢占 · 租约 · 栅栏（claim / lease / fence）

`Submission` 上的状态机：`PENDING → QUEUED → JUDGING → DONE / FAILED`（迁移 0017/0018 新增字段，旧 `status` 判定字段不变）。核心逻辑在 [submissions/claiming.py](submissions/claiming.py)：

- **抢占**：worker 开工用单条 `UPDATE ... WHERE judge_state IN ('PENDING','QUEUED') RETURNING claim_token`，竞争者中至多一个胜出；
- **心跳**：判题期间每 15s 凭 token 续租（`heartbeat_at`）；
- **栅栏写回**：结果写回必须同时匹配 `claim_token + JUDGING`，丢失租约的旧 worker 写入 0 行，积分 / solved M2M / 通知等副作用不会触发；
- **回收**：reaper 常驻巡检，JUDGING 心跳超时 300s / QUEUED 距上次投递（`enqueued_at`）超 600s 的任务自动重投。requeue 的陈旧判定在原子 UPDATE 内完成，worker 在扫描与写入之间恢复心跳不会被误抢。
- **基础设施重试**：有上限（默认 3 次 / 小时），指数退避；DB-less worker 的结果经 `judge:result` 可靠队列（处理中队列 + ACK + 死信 `judge:result:dead`）由 Web 侧消费者用同一套栅栏幂等写库。

### DB-less 测评机（当前生产形态）

测评机进程**不持有任何 PostgreSQL 凭证**：通过 HTTPS 内部 API（`/internal/judge/claim|heartbeat|cases/`，`X-Judge-Token` 常量时间比较，CSRF 豁免）抢占任务与拉取测试点，判完把结果信封 `LPUSH` 到中央 Redis；判题纯核心在 [submissions/judge_core.py](submissions/judge_core.py)（无 ORM）。多台机器跑**完全相同**的 worker 单元竞争消费，加机器只需把 `JUDGE_BROKER_URL` 指向同一个中央 Redis，无需任何 Django 侧配置。

```
worker ──HTTPS /internal/judge/*──▶ Django: 原子抢占 / 心跳 / 测试点
                                   result consumer ──▶ PostgreSQL
中央 Redis（TLS + 密码）: judge 优先级队列 + judge:result + pub/sub
reaper（Web 侧 30s 巡检）: 回收超时租约并重投
```

### Broker 连接配置

判题 broker Redis 必须启用 TLS + 密码（≥12 位，含字母 / 数字 / 特殊字符），不入库：

```dotenv
# 推荐：worker 与 web 均可直接用完整 URL
JUDGE_BROKER_URL=rediss://:<secret>@<redis-host>:6379/0?ssl_ca_certs=/etc/redis/tls/ca.crt
# Web 端省略 JUDGE_BROKER_URL 时由下列变量拼出（RQ_REDIS_* 为兼容旧名，与 RQ 框架无关）
RQ_REDIS_HOST=127.0.0.1
RQ_REDIS_PASSWORD=<secret>
RQ_REDIS_TLS=true
```

健康探测与 WebSocket 多机订阅使用 `JUDGE_MACHINES_JSON` 或后台 `JudgeMachine` 记录（**仅 Web 进程**用，不参与任务分发）；worker 配置模板见 [deploy/env.judge.example](deploy/env.judge.example)。

### 直连回源与动态 IP 自愈

NAT 后测评机公网 IP 频繁变化，静态白名单不可维护：

1. worker 定期（默认 600s）及直连抢占失败后，经 CDN 调 `POST /internal/judge/report_ip/`；
2. Web 只从连接本身取源 IP（`X-Forwarded-For → CF-Connecting-IP → X-Real-IP → REMOTE_ADDR`，不信任请求体），仅接受公网 IPv4，写入 `/etc/guwu/judge-direct-ips.json`；
3. 每次上报同步重建、且 `guwu-oj-judge-firewall.service` 每 5 分钟兜底重建两条 iptables 链：`JUDGE_DIRECT`（8446）与 `OJ_JUDGE_BROKER`（6379,8446，含内网静态 IP，生产实际先生效的链），链尾均为 DROP。

worker 端 `JUDGE_API_BASE` 指直连端口、`JUDGE_API_FALLBACK_BASE` 指 CDN 域名，主 base 不可达自动轮换。Web `.env` 关键项：

```dotenv
OJ_JUDGE_DIRECT_STATIC_IPS=64.90.3.112        # 静态公网 IP，逗号分隔
OJ_JUDGE_BROKER_STATIC_IPS=192.168.196.147    # broker 链额外放行的内网 IP
```

> 完整运维 Runbook（证书、防火墙脚本、CDN、排障）见 [OPS.md](OPS.md)。

### WebSocket 实时推送

- 端点 `/ws/submissions/<id>/status/`：[submissions/ws.py](submissions/ws.py) 原生 ASGI consumer，session cookie 鉴权（仅本人 / staff），单 worker 固定；
- 链路：判题写库 → `post_save` 信号 → Redis `PUBLISH oj:submission:<id>` → ASGI 订阅查库推快照（消息不带数据，不泄露隐藏测试点）；另有 5s DB watchdog 兜底、断线 2s 重连；
- 前端优先 WS（25s 心跳），4401/4403/4404 或 3 次重连失败自动降级 800ms HTTP 轮询；
- nginx 需反代 `/ws/` 到 `127.0.0.1:8447`（Upgrade 头、`proxy_buffering off`、读超时 3600s）；经 CDN 需在控制台开通 WebSocket。

## 生产服务

单元文件均在 [deploy/systemd/](deploy/systemd/)：

| 组件 | 单元 | 部署位置 |
|------|------|----------|
| 主站（Granian WSGI，Unix socket） | `guwu-oj.service` | Web |
| WebSocket ASGI（127.0.0.1:8447） | `guwu-oj-ws.service` | Web |
| 判题 worker（Celery 线程池，竞争消费） | `guwu-oj-judge-worker.service` | 每台测评机 |
| 僵尸任务回收重投（30s 巡检） | `guwu-oj-judge-reaper.service` | Web |
| `judge:result` 消费者（栅栏幂等写库） | `guwu-oj-judge-result-consumer.service` | Web |
| 直连端口 iptables 链同步（5min） | `guwu-oj-judge-firewall.service` | Web（root） |

改后端代码后 WSGI 与 ASGI 都需重启。常用命令：

```bash
python manage.py judge_overview        # 优先级桶深度、在线 worker、状态分布、僵尸数、近 1h 延迟/错误率
python manage.py check_judge_health    # 逐机 Redis + 中央 broker 心跳
python manage.py reap_stale_judgments  # 手动回收一轮（--loop 常驻）
python manage.py sync_judge_firewall   # 重建 iptables 链（--loop 常驻，需 root）
```

## 支持的语言

| 语言 | 编译 / 运行 | | 语言 | 编译 / 运行 |
|------|------------|---|------|------------|
| C | `gcc -O2` | | Go | `go build` |
| C++ | `g++ -std=c++17 -O2` | | Rust | `rustc --edition=2021` |
| Python | `python3` | | Ruby | `ruby` |
| Java | `javac` / `java` | | Kotlin | `kotlinc` / `java -jar` |
| JavaScript | `node` | | Assembly | `as` + `ld` |

## 其他生产特性

- **缓存**：django-redis（`CACHE_REDIS_*` 配置）；页面 / 查询 / Markdown 渲染缓存，写操作自动失效；WhiteNoise 提供静态文件。
- **安全**：提交限流（3 次/分钟）、bleach 清理 Markdown HTML、全站 CSRF、输入长度校验；登录失败 / 注册 / 改密 / 高频提交 / 高风险模式强制「图形验证码 + ALTCHA PoW」双验证（参数见 `users/altcha.py`）；后台强制 staff 2FA + sudo 模式。
- **监控**：`/health/`（DB / Redis / 判题机）、`/metrics/`（Prometheus）、结构化 JSON 日志与轮转。

## 开发计划

- [x] 自动评测、10 种语言、按语言拆分沙箱镜像、容器池
- [x] 中央 Celery 队列 + 抢占 / 租约 / 栅栏 + 僵尸回收 + DB-less 测评机
- [x] 多判题机分布式、比赛、AI 解题、题解、文档搜索、移动端适配
- [ ] Windows 支持改进（Worker Class 重构）

## 许可证

MIT License。问题与建议欢迎提 Issue。

## Star History

<a href="https://www.star-history.com/#repos=alphadamn/guwu-oj&type=date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&legend=top-left" />
 </picture>
</a>
