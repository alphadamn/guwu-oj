# 洛谷风格 OJ - 在线评测系统

一个模仿洛谷的在线评测系统，使用 Python、Django 和 Bootstrap 5 构建。

## 功能特性

- 用户系统
  - 用户注册、登录、登出
  - 个人资料管理（头像、昵称、简介）
  - 用户主页展示提交记录和已解决的题目

- 题库系统
  - 题目列表（支持按难度、标签筛选）
  - 题目详情页（包含题目描述、输入输出格式、样例等）
  - 难度分级（入门、普及-、普及、普及+、提高-、提高、提高+、省选、NOI）

- 提交系统
  - 代码提交（支持 C、C++、Python、Java、JavaScript、Go、Rust、Ruby、Kotlin、Assembly）
  - 提交记录查看
  - 提交详情（代码、评测结果、运行时间、内存使用）
  - 评测状态 WebSocket 实时推送（逐测试点刷新，失败自动降级为 HTTP 轮询）
  - 分布式判题：Celery + 中央 Redis 队列（单队列 + 消息优先级）、多机竞争消费、原子抢占 + 租约心跳 + 栅栏写回，测评机可完全无数据库凭证（DB-less）
  - 直连回源与动态 IP 自愈：NAT 后的测评机公网 IP 频繁变化，静态白名单无法维护，改为「worker 自报 IP + Web 侧 iptables 链同步」自动放行直连端口；直连不可达时自动回退 CDN 回源（详见下方「生产架构：中央判题代理 + DB-less 测评机」）

- 排行榜
  - 用户排名（按已解决题目数排序）
  - 统计信息（已解决数、提交数、通过率）

- 管理后台
  - 题目管理
  - 用户管理
  - 提交记录管理

## 核心架构

- Django 5+
- Bootstrap
- PostgreSQL
- Docker (用于沙箱评测环境)
- Redis + Celery (缓存 + 判题任务队列 + pub/sub 状态推送)
- Granian 双服务：WSGI 主站 + 独立 ASGI WebSocket 服务（不引入 Channels）

## 安装步骤

### 使用 Docker（推荐）

项目使用按语言拆分的轻量 Docker 镜像进行沙箱评测，替代单一巨型镜像：

```bash
# 构建所有评测镜像（一次性）
./scripts/build-containers.sh
```

构建后将产生 5 个独立镜像：

| 镜像 | 语言 | 大小 |
|------|------|------|
| `oj-python` | Python | ~44MB |
| `oj-c` | C | ~98MB |
| `oj-cpp` | C++ | ~116MB |
| `oj-java` | Java | ~183MB |
| `oj-other` | Go, Rust, JS, Ruby, Kotlin, ASM | ~640MB |

判题时系统会根据提交语言自动选择对应镜像，无需手动指定。

如果你更倾向于本地直接运行而非 Docker，可跳过此段，继续使用常规的 Python 环境。

## 安装步骤

### 1. 克隆项目

```bash
git clone https://github.com/alphadamn/guwu-oj
cd guwu-oj
```

### 2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # macOS/Linux
# 或
venv\Scripts\activate  # Windows
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 数据库迁移

```bash
python manage.py makemigrations
python manage.py migrate
```

### 5. 创建超级用户

```bash
python manage.py createsuperuser
```

### 6. 处理静态文件

```bash
python manage.py collectstatic
```

### 7. 启动开发服务器

```bash
python manage.py runserver
```
#### 或

```bash
gunicorn oj_project.wsgi --bind 0.0.0.0:8000
```
访问 http://127.0.0.1:8000 查看网站。
#### 或使用systemd（参考，nginx反代的上游）
```
[Unit]
Description=Guwu Online Judge (Granian WSGI over Unix domain socket)
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/www/wwwroot/guwu-oj
Environment="PATH=/www/wwwroot/guwu-oj/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="DJANGO_SETTINGS_MODULE=oj_project.settings"
Environment="SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https"
# With multiple workers, django_prometheus needs multiprocess mode for
# correct /metrics aggregation.
Environment="PROMETHEUS_MULTIPROC_DIR=/run/guwu-oj/prom"

RuntimeDirectory=guwu-oj
RuntimeDirectoryMode=0755
ExecStartPre=/usr/bin/mkdir -p /run/guwu-oj/prom
ExecStartPre=/usr/bin/rm -f /run/guwu-oj/guwu-oj.sock

# Granian serves the Django WSGI app on a Unix domain socket (loopback-only,
# no TLS/HTTP-3 needed for a local hop); nginx proxies to it over HTTP/1.1
# keep-alive. Granian binds the socket root:root 660; nginx workers run as
# www, so hand the socket's group to www after startup.
#
# Race fix: with Type=simple, ExecStartPost runs immediately after fork,
# before granian has created the socket file. The wait loop polls for the
# socket up to ~6s before chgrp, so the service no longer fails on a fast
# system where granian takes 50-200ms to bind.
ExecStartPost=/bin/bash -c 'for i in $(seq 1 60); do [ -S /run/guwu-oj/guwu-oj.sock ] && chgrp www /run/guwu-oj/guwu-oj.sock && exit 0; sleep 0.1; done; exit 1'
ExecStart=/www/wwwroot/guwu-oj/venv/bin/granian \
    --uds /run/guwu-oj/guwu-oj.sock \
    --uds-permissions 660 \
    --interface wsgi \
    --http 1 \
    --workers 12 \
    --blocking-threads 2 \
    --backpressure 30 \
    --loop auto \
    --access-log \
    --log-level info \
    --log-config /www/wwwroot/guwu-oj/deploy/granian_log_config.json \
    --workers-lifetime 12h \
    --workers-max-rss 500 \
    --rss-samples 3 \
    --respawn-failed-workers \
    oj_project.wsgi:application

# Clean shutdown: SIGTERM gives in-flight requests 30s.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
Restart=on-failure
RestartSec=1

# Auto-restart worker processes that crash or get OOM-killed.
RestartPreventExitStatus=0

# Memory safety: 12 workers x ~150 MB normal RSS (~1.8 GB total). A cgroup
# ceiling turns any runaway allocation into a deterministic, automatic service
# restart instead of a global OOM that kills other services on the box.
# MemoryHigh throttles softly (per-worker --workers-max-rss 500 recycles
# bloated workers first), MemoryMax cgroup-OOMs -> Restart=on-failure.
MemoryHigh=2200M
MemoryMax=3200M

[Install]
WantedBy=multi-user.target
```
```bash
systemctl enable guwu-oj
systemctl start guwu-oj
```

### 8. 启动 Redis 服务

```bash
redis-server
```

### 9. 启动 Celery Worker (用于异步评测)

判题任务由 Celery 投递到中央 Redis 的单一逻辑队列 `judge`，队列优先级通过消息优先级实现（Redis transport 的优先级桶），**数字越小越先消费**：

| 优先级（高→低） | 用户层级 | Celery priority | Redis 桶 |
|----------------|----------|-----------------|----------|
| pro | Pro 订阅 | 0 | `judge` |
| plus | Plus 订阅 | 3 | `judge:3` |
| free（默认） | 免费用户 | 6 | `judge:6` |
| ai | AI 讲解判题验证（专用服务账号） | 9 | `judge:9` |

#### Redis 密码与 TLS 配置

判题队列 Redis 必须同时启用 TLS 和密码认证。不要将真实密码写入版本控制；在 Web 服务器和每台判题机的受限 `.env` 文件中设置相同的密码。密码至少 12 个字符，并包含字母、数字和特殊字符。

判题机通过 `JUDGE_BROKER_URL` 指向中央 Redis（推荐，Web 与判题机同址部署时也可省略，回退到 `RQ_REDIS_*` 变量）：

```dotenv
JUDGE_BROKER_URL=rediss://:<generated-secret>@judge-redis.example.internal:6379/0?ssl_ca_certs=/etc/redis/tls/ca.crt
# Web 端未设置 JUDGE_BROKER_URL 时，broker 由下列变量拼出：
RQ_REDIS_HOST=judge-redis.example.internal
RQ_REDIS_PORT=6379
RQ_REDIS_DB=0
RQ_REDIS_PASSWORD=<generated-secret>
RQ_REDIS_TLS=true
RQ_REDIS_CA_CERT=/etc/redis/tls/ca.crt
```

Redis 服务端必须使用相同密码配置 `requirepass`，并保持 `port 0` 与 TLS 端口配置。每次修改密码时，先更新所有 Web/判题机 `.env` 文件，再重启 Redis，最后重启所有 Celery worker。

可使用下列命令验证认证与 TLS，其中未提供密码的命令必须返回 `NOAUTH Authentication required`：

```bash
REDISCLI_AUTH="$RQ_REDIS_PASSWORD" redis-cli --tls --cacert "$RQ_REDIS_CA_CERT" \
  -h "$RQ_REDIS_HOST" -p "$RQ_REDIS_PORT" ping
```

**开发环境启动 worker（线程池，并行度由 `OJ_JUDGE_CONCURRENCY` 控制，默认 4）：**
```bash
celery -A oj_project worker -Q judge -P threads -c 4 -l info
```

> 生产环境使用 systemd 单元 `guwu-oj-judge-worker.service`（ExecStart 已封装上述命令），不要在生产用 `runserver` 旁挂 worker。

#### 多判题机部署

所有判题机运行**完全相同**的 worker 单元，竞争消费中央 Redis 上的同一个 `judge` 队列；添加一台测评机不需要任何 Django 侧配置，只需让它指向同一个中央 Redis（设置 `JUDGE_BROKER_URL`）。

**1. 在各判题机上启动 Celery Worker**

```bash
# 两台机器命令完全一致（-n 的 %h 自动取主机名）
celery -A oj_project worker -Q judge -P threads -c ${OJ_JUDGE_CONCURRENCY:-4} -n judge@%h -l info
# 或直接：
systemctl enable --now guwu-oj-judge-worker
```

每个 worker 进程内部以线程池并行评测多个提交，单机并行度由 `OJ_JUDGE_CONCURRENCY` 控制（judge-1=4，judge-2=3，即 m 台机器并行度之和为总评测能力）。worker 启动时预热各语言 Docker 容器池，并把 `judge:worker:celery:<host>` 心跳（90s TTL，30s 刷新）写入中央 Redis。

**2. 检查判题机健康状态**

```bash
python manage.py check_judge_health   # 逐台 Redis + 中央 broker worker 心跳
python manage.py judge_overview       # 四个优先级桶深度、在线 worker、租约、延迟
```

健康检查与 WebSocket 推送的多机 Redis 订阅仍使用 `JUDGE_MACHINES_JSON` / Admin 中的 `JudgeMachine` 记录（仅 Web 进程使用：健康探测和状态发布订阅），它不再参与任务分发。

#### 生产架构：中央判题代理 + DB-less 测评机（推荐）

旧的「每机一个队列 + 加权分发 + worker 直连 PostgreSQL」已废弃，当前生产判题架构为 Celery 中央队列 + 抢占租约 + DB-less 测评机：

1. **中央队列（Celery + central broker）**：Web 端只把判题任务投到中央 Redis 的单一 Celery 队列 `judge`，优先级通过消息优先级桶区分（pro=0 / plus=3 / free=6 / ai=9，数字越小越先消费），所有测评机运行相同的 worker 单元竞争消费。添加测评机无需任何 Django 侧配置，只需让它指向同一个中央 Redis（`JUDGE_BROKER_URL`）。
2. **抢占 / 租约 / 栅栏（claim · lease · fence）**：`Submission` 增加 `judge_state`（PENDING/QUEUED/JUDGING/DONE/FAILED）、`worker_id`、`claim_token`、`claimed_at` / `heartbeat_at` / `finished_at` 字段（迁移 `0017`，旧 `status` 判定字段保持不变）。worker 开工前用单条 `UPDATE ... WHERE judge_state IN (...) RETURNING` 原子抢占；判题期间每 15 秒经心跳续租；结果写回带 `claim_token` 栅栏——丢失租约的 worker 无法覆盖新 owner 的结果，重复投递也只有一台机器真正判题。僵尸任务由常驻 reaper 按 JUDGING 300s / QUEUED 600s 阈值自动回收重投。
3. **DB-less 测评机**：开启后测评机进程内**不存在任何 PostgreSQL 凭证**（`DATABASES` 退化为惰性 sqlite 仅用于 Django 引导）。worker 通过 HTTPS 调用 Web 内部 API 抢占任务并取回代码与测试点，判题期间 HTTP 心跳续租，判完把结果信封 `LPUSH` 到中央 Redis 的 `judge:result` 列表；Web 端常驻消费者以「处理中队列 + ACK + 死信」的可靠队列模式取出信封，经同一套栅栏逻辑幂等写库（测试点、solved 关系、积分均幂等）。

```
                ┌──────────────────────────── Web ───────────────────────────┐
 HTTPS /internal/judge/claim|heartbeat/  (X-Judge-Token)                     │
 worker ───────────────────────────────▶│ Django: 原子抢占 / 心跳校验          │
                                        │ result consumer ──▶ PostgreSQL       │
                                        └──────▲──────────────▲───────────────┘
                                               │ BRPOPLPUSH    │ Celery LPUSH
                                  judge:result │   judge 队列   │ (priority 桶)
                            ┌──────────────────┴───────────────┴──────────┐
                            │      中央 Redis（TLS + 密码，单队列+优先级）   │
                            └──────▲──────────────────────────▲────────────┘
                                   │ 竞争消费                  │ 竞争消费
                          ┌────────┴────────┐        ┌────────┴────────┐
                          │ judge-1 DB-less │        │ judge-2 DB-less │
                          │ 无 PG 凭证，只出 │        │ 无 PG 凭证，只出 │
                          │ 站 443 + Redis  │        │ 站 443 + Redis  │
                          └─────────────────┘        └─────────────────┘
 reaper（Web 侧，30s 巡检）：回收心跳超时的 JUDGING / QUEUED 任务并重新入队
```

**环境变量**（Web `.env`）：

```dotenv
# Celery broker（省略时由 RQ_REDIS_* 拼出本机 TLS Redis 地址）
JUDGE_BROKER_URL=rediss://:<password>@<central-redis-host>:6379/0?ssl_ca_certs=/etc/redis/tls/ca.crt

# 内部判题 API（worker 用此令牌鉴权，务必使用高强度随机值）
JUDGE_INTERNAL_TOKEN=<generated-secret>
```

**环境变量**（每台测评机 `.env`；DB-less 模式下**不配置任何 `DB_*`**）：

```dotenv
OJ_ROLE=worker
JUDGE_BROKER_URL=rediss://:<password>@<central-redis-host>:6379/0?ssl_ca_certs=/etc/redis/tls/ca.crt

# DB-less
OJ_WORKER_DBLESS=true
JUDGE_API_BASE=https://<web-host>
JUDGE_INTERNAL_TOKEN=<same-secret-as-web>
# OJ_WORKER_ID 不设则默认取主机名；心跳间隔/租约超时一般保持默认
# OJ_JUDGE_HEARTBEAT_SECS=15   OJ_JUDGE_LEASE_TIMEOUT_SECS=300
```

> 内部 API 固定使用 `X-Judge-Token` 请求头做 HMAC 常量时间比较（`hmac.compare_digest`），并豁免 CSRF；抢占失败返回 `409 {"claimable": false}`，worker 直接空 ACK。经 CDN 暴露时注意：Python urllib 默认 User-Agent 可能被 WAF 拦截，worker 客户端已固定为 `GuwuOJ-JudgeWorker/1.0`。

#### 直连回源与动态 IP 自愈（NAT 后的测评机）

测评机的 `JUDGE_API_BASE` 指向 Web 主机的**直连端口**（绕过 Cloudflare），`JUDGE_API_FALLBACK_BASE` 指向 CDN 上的正式域名兜底：

```dotenv
# 每台测评机 .env（DB-less）
JUDGE_API_BASE=http://154.12.60.26:8446          # 直连，绕开 Cloudflare
JUDGE_API_FALLBACK_BASE=https://guwu.camluni.cn  # 直连不可达时回退 CDN 回源
```

客户端按 base 逐个尝试：主 base 不可达时自动轮换到 fallback；抢占因网络错误失败后，worker 会**强制重报一次公网 IP** 再重试一次抢占，仍失败才按基础设施故障处理。

NAT 后的测评机公网 IP 每天会变，静态白名单无法维护，因此改为**自报 + 防火墙同步**闭环：

1. worker 定期（`OJ_JUDGE_IP_REPORT_INTERVAL`，默认 600s）以及每次直连抢占失败后，经 CDN 调用 `POST /internal/judge/report_ip/`。
2. Web 端从**连接本身**取观测到的源 IP（`X-Forwarded-For` → `CF-Connecting-IP` → `X-Real-IP` → `REMOTE_ADDR`，**不信任请求体**），仅接受公网 IPv4，写入 `OJ_JUDGE_DIRECT_STATE`（默认 `/etc/guwu/judge-direct-ips.json`）。
3. `guwu-oj-judge-firewall.service` 定期调用 `manage.py sync_judge_firewall`，按「静态 IP + 已上报 IP」重建 iptables 链：

```
INPUT        -p tcp --dport 8446 -j JUDGE_DIRECT
JUDGE_DIRECT -s <allow>          -j ACCEPT
JUDGE_DIRECT                     -j DROP      # 兜底拒绝：链被清空也不会暴露端口
```

> 直连端口（`OJ_JUDGE_DIRECT_PORT`，默认 8446）的源过滤由该 iptables 链全权接管，nginx vhost **不再配置 IP 白名单**（对动态 IP 必然失效）。**启用 nginx vhost 前必须先在 Web 主机跑一次 `sync_judge_firewall` 建链**，否则 8446 处于无人看守状态。

Web 侧 `.env` 相关配置：

```dotenv
OJ_JUDGE_DIRECT_PORT=8446
OJ_JUDGE_DIRECT_STATIC_IPS=64.90.3.112           # 静态测评机，逗号分隔，始终放行
# OJ_JUDGE_DIRECT_CHAIN=JUDGE_DIRECT
# OJ_JUDGE_DIRECT_STATE=/etc/guwu/judge-direct-ips.json
# OJ_JUDGE_IP_REPORT_INTERVAL=600
```

**Web 侧常驻辅助服务**（单元文件均在 `deploy/systemd/`）：

| 服务 | 作用 |
|------|------|
| `guwu-oj-judge-reaper.service` | 30 秒巡检一轮，回收心跳超时的判题任务并重新入队（`reap_stale_judgments --loop`） |
| `guwu-oj-judge-result-consumer.service` | `BLPOP` 消费 `judge:result`，栅栏校验后幂等写库；处理中崩溃的消息启动时自动回队，反复失败的消息进 `judge:result:dead` |
| `guwu-oj-judge-firewall.service` | 每 5 分钟重建 `JUDGE_DIRECT` 链，把 8446 直连端口放行给静态 IP 与自报 IP（`sync_judge_firewall --loop`，需 root） |

```bash
cp deploy/systemd/guwu-oj-judge-reaper.service \
   deploy/systemd/guwu-oj-judge-result-consumer.service \
   deploy/systemd/guwu-oj-judge-firewall.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now guwu-oj-judge-reaper guwu-oj-judge-result-consumer guwu-oj-judge-firewall
```

**运维命令**：

```bash
python manage.py judge_overview        # 队列深度、在线 worker、状态分布、僵尸数、近 1 小时延迟/错误率
python manage.py reap_stale_judgments  # 手动回收一轮（--loop 常驻；--judging-timeout/--queued-timeout 调阈值）
python manage.py sync_judge_firewall   # 重建直连端口 iptables 链（--loop --interval 300 常驻；需 root）
```

**回滚**：Celery 是当前唯一的判题分发路径（旧 RQ lane 与 `OJ_CENTRAL_QUEUE` 开关已移除）；如需回到 DB 直连判题，恢复 worker 原 `.env`（含 `DB_*`）、去掉 `OJ_WORKER_DBLESS` 后重启 worker 即可，抢占/栅栏逻辑在 DB 路径同样生效（迁移 0017 为纯新增列/索引）。

### 10. 验证环境配置 (可选)

运行环境验证脚本检查所有组件是否正常工作:

```bash
python verify_setup.py
```

此脚本会检查:
- Python 版本兼容性
- 数据库连接和配置
- Redis 连接和缓存操作
- 数据库表完整性
- Docker 状态和安全配置
- Python 依赖包
- 文件权限
- 环境变量配置

或使用selenium测试网站基础功能：

```bash
python manage.py test
```

### 11. 启动 WebSocket 实时状态服务

提交状态推送由独立的 ASGI 进程提供，必须与主站分别启动（共需运行：主站 + Redis + Celery worker + WebSocket 服务）。

**开发环境**（主站仍可用 `runserver`，另开一个终端启动 ASGI 服务）：

```bash
granian oj_project.asgi:application \
    --interface asgi \
    --host 127.0.0.1 \
    --port 8447 \
    --workers 1
```

> 必须固定单 worker：consumer 在事件循环内缓存共享的 Redis 异步客户端（`submissions/ws.py`），多 worker 会破坏客户端缓存的一致性。

**生产环境（systemd）**：单元文件位于 `deploy/systemd/guwu-oj-ws.service`，安装后启动：

```bash
cp deploy/systemd/guwu-oj-ws.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now guwu-oj-ws
```

nginx 需将 `location /ws/` 反代到 `127.0.0.1:8447`（Upgrade/Connection 头、关闭 buffering、读超时 3600s）。

### 12. 启动完整服务（命令汇总）

完整站点由 Web 侧五个常驻服务与每台测评机的 worker 组成（另有 Redis/PostgreSQL 作为基础设施）：

| 组件 | 作用 | systemd 单元 |
|------|------|-------------|
| 主站 | Django（Granian WSGI，Unix socket） | `guwu-oj.service` |
| WebSocket | 提交状态 ASGI 推送（127.0.0.1:8447） | `guwu-oj-ws.service`（见 `deploy/systemd/`） |
| 判题 Worker | Celery 异步评测（中央 `judge` 队列竞争消费，可 DB-less） | `guwu-oj-judge-worker.service`（每台测评机，见 `deploy/systemd/`） |
| 判题回收器 | 回收心跳超时的僵尸判题任务并重投 | `guwu-oj-judge-reaper.service`（Web 侧，见 `deploy/systemd/`） |
| 结果消费者 | 消费 `judge:result`，栅栏校验后幂等写库 | `guwu-oj-judge-result-consumer.service`（Web 侧，见 `deploy/systemd/`） |
| 直连防火墙同步 | 按自报 IP 重建 8446 直连端口的 iptables 链 | `guwu-oj-judge-firewall.service`（Web 侧，见 `deploy/systemd/`） |

```bash
# 生产环境 Web 侧：一次性启动/重启
systemctl start guwu-oj guwu-oj-ws guwu-oj-judge-reaper guwu-oj-judge-result-consumer guwu-oj-judge-firewall
systemctl restart guwu-oj guwu-oj-ws guwu-oj-judge-reaper guwu-oj-judge-result-consumer guwu-oj-judge-firewall
systemctl status guwu-oj guwu-oj-ws guwu-oj-judge-reaper guwu-oj-judge-result-consumer guwu-oj-judge-firewall

# 每台测评机：
systemctl restart guwu-oj-judge-worker

# 开发环境：分别在三个终端运行
python manage.py runserver                          # 主站
celery -A oj_project worker -Q judge -P threads -c 4 -l info  # 判题 worker
granian oj_project.asgi:application --interface asgi --host 127.0.0.1 --port 8447 --workers 1
```

修改后端代码后，WSGI 主站与 ASGI WebSocket 服务都需要重启：

```bash
systemctl restart guwu-oj guwu-oj-ws
```


## 项目结构

```
guwu-oj/
├── manage.py                 # Django 管理脚本
├── requirements.txt          # 项目依赖
├── README.md                # 项目说明
├── oj_project/              # Django 项目配置
│   ├── __init__.py
│   ├── celery.py            # Celery app（判题任务队列，Redis 优先级桶）
│   ├── settings.py          # 项目设置
│   ├── urls.py              # 主 URL 配置
│   ├── wsgi.py              # WSGI 配置（主站）
│   └── asgi.py              # ASGI 配置（WebSocket 服务）
├── users/                   # 用户应用
│   ├── __init__.py
│   ├── apps.py
│   ├── models.py            # 用户模型
│   ├── forms.py             # 用户表单
│   ├── views.py             # 用户视图
│   ├── urls.py              # 用户 URL
│   └── admin.py             # 用户管理后台
├── problems/                # 题目应用
│   ├── __init__.py
│   ├── apps.py
│   ├── models.py            # 题目模型
│   ├── views.py             # 题目视图
│   ├── urls.py              # 题目 URL
│   └── admin.py             # 题目管理后台
├── submissions/             # 提交应用
│   ├── __init__.py
│   ├── apps.py
│   ├── models.py            # 提交模型（含 judge_state/claim_token 租约字段）
│   ├── views.py             # 提交视图
│   ├── urls.py              # 提交 URL
│   ├── ws.py                # WebSocket ASGI consumer
│   ├── realtime.py          # Redis pub/sub 与状态载荷构建
│   ├── signals.py           # post_save 触发推送
│   ├── celery_worker.py     # Celery worker 信号：容器池预热 + worker 心跳
│   ├── judge_queue.py       # 判题任务入队（单队列 judge + pro/plus/free/ai 优先级）
│   ├── claiming.py          # 原子抢占、心跳续租、栅栏 finalize、僵尸回收
│   ├── judge.py             # 判题流程（DB 路径）
│   ├── judge_core.py        # 纯判题核心（无 ORM/缓存，DB-less worker 使用）
│   ├── results.py           # 栅栏幂等写回（测试点/solved/积分）与信封处理
│   ├── result_queue.py      # judge:result 可靠队列原语（处理中队列/ACK/死信）
│   ├── worker_api.py        # DB-less worker 的内部 HTTP 客户端（双 base 轮换）与 HTTP 心跳
│   ├── internal_views.py    # /internal/judge/claim|heartbeat|report_ip/ 内部 API
│   ├── internal_urls.py     # 内部 API 路由（/internal/judge/）
│   ├── judge_firewall.py    # 直连端口 iptables 链：自报 IP 状态与链重建
│   └── admin.py             # 提交管理后台
├── templates/               # 模板文件
│   ├── base.html            # 基础模板
│   ├── home.html            # 首页
│   ├── leaderboard.html     # 排行榜
│   ├── users/               # 用户模板
│   │   ├── login.html
│   │   ├── register.html
│   │   ├── profile.html
│   │   └── edit_profile.html
│   ├── problems/            # 题目模板
│   │   ├── problem_list.html
│   │   └── problem_detail.html
│   └── submissions/         # 提交模板
│       ├── submit.html
│       ├── detail.html
│       ├── list.html
│       └── all_list.html
└── static/                  # 静态文件目录
```

### Redis 缓存与后台任务

项目已在 `settings.py` 中配置了 **django‑redis**，默认使用 `redis://127.0.0.1:6379/1`。在生产环境建议使用独立的 Redis 实例，并通过环境变量覆盖 `REDIS_URL` 或直接修改 `CACHES` 配置。

```python
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1"),
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}
```

### WhiteNoise 静态文件服务

`WhiteNoise` 已加入 `MIDDLEWARE`，无需额外配置即可在 Gunicorn/uwsgi 等 WSGI 服务器上直接提供压缩和缓存的静态文件。若需要自定义缓存时间，可在 `settings.py` 添加：

```python
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
WHITENOISE_MAX_AGE = 31536000  # 1 year
```

## 生产环境特性

### 异步评测系统

项目使用 Celery（Redis transport）实现异步评测，避免评测阻塞 HTTP 请求：

- 评测任务通过中央 Redis 的单一 Celery 队列 `judge` 异步执行，支持多台测评机竞争消费；四级优先级走消息优先级桶（pro=0 / plus=3 / free=6 / ai=9，数字越小越先消费，AI 讲解验证最低）
- 原子抢占 + 租约心跳 + claim-token 栅栏：重复投递只有一台机器真正判题，worker 崩溃后由 reaper 自动回收重投，结果写回与积分/通过等副作用全部幂等
- DB-less 模式下测评机不持有任何 PostgreSQL 凭证：经 HTTPS 内部 API 抢占任务，结果经 `judge:result` 队列由 Web 侧消费者幂等写库
- 评测结果自动更新到数据库
- 支持基础设施故障自动重试（有次数上限）与错误日志记录，可用 `python manage.py judge_overview` 查看优先级桶深度、worker 心跳与租约状态

### 提交状态实时推送（WebSocket）

提交详情页的评测进度通过 WebSocket 毫秒级推送，取代旧的 long polling 轮询。实现上不引入 Channels/Twisted，而是用 Granian（`--interface asgi`）跑一个独立的 ASGI 服务（`guwu-oj-ws.service`，仅监听 `127.0.0.1:8447`，单 worker），主站的 Granian WSGI 服务完全不动。

- **端点**：`/ws/submissions/<id>/status/`，由 `oj_project/asgi.py` 路由到 `submissions/ws.py` 的原生 ASGI consumer；其余 HTTP 路径回落 Django ASGI。
- **鉴权**：复用 Django session cookie（与普通页面同源），仅提交者本人或 staff 可连接。握手前拒绝返回 HTTP 403，关闭码 `4401`（未登录）/ `4403`（无权访问）/ `4404`（未知路由）。
- **推送链路**：判题进程保存测试点/终态 → `submissions/signals.py` 中 `Submission` 与 `SubmissionTestResult` 的 `post_save` 信号 → `submissions/realtime.py` 经 django-redis `PUBLISH` 到频道 `oj:submission:<id>` → ASGI 服务订阅后重新查库并推送 JSON 快照。消息本身不带数据，避免泄露隐藏测试用例。
- **多 Redis 订阅**：consumer 同时订阅本机缓存 Redis 和所有启用判题机的 Redis（远程 worker 在其队列所在实例上发布；Redis pub/sub 不区分 db），不可达的判题机不阻塞首包，监听器断线 2 秒自动重连。
- **兜底机制**：除 pub/sub 外，consumer 每 5 秒轮询一次数据库（watchdog polling），防止漏消息或远程判题机未同步信号代码；另含 30 秒应用层 ping 与 0.25 秒取数合并；评测到达终态后服务端主动关闭连接。
- **载荷兼容**：WebSocket 推送与 HTTP 轮询接口 `/submissions/api/<id>/status/` 共用 `realtime.build_submission_status_payload`，JSON 结构完全一致。
- **前端降级**（`static/js/submission-detail.js`）：优先连 WebSocket（25 秒心跳），遇到 4401/4403/4404 或连续重连 3 次失败，自动降级为 800ms 间隔的 HTTP 轮询，旧接口保留可用。
- **nginx**：`location /ws/` 反代到 `127.0.0.1:8447`（`Upgrade`/`Connection` 头、`proxy_buffering off`、`proxy_read_timeout 3600s`）。经 CDN 访问时需在 CDN 控制台确认开通 WebSocket（否则前端自动降级轮询）。
- **远程判题机**：需手动同步信号相关代码并 restart worker 才能获得毫秒级推送；在此之前由 5 秒 polling 兜底，状态最迟约 5 秒更新。

### 缓存策略

- **页面缓存**: 使用 Redis 缓存视图响应
- **查询缓存**: 缓存数据库查询结果 (问题列表、排行榜等)
- **Markdown 缓存**: 缓存 Markdown 渲染结果，避免重复渲染
- **缓存失效**: 数据变更时自动清除相关缓存

### 监控与日志

- **结构化日志**: 记录到控制台、文件和错误日志
- **健康检查**: `/health/` 端点检查数据库、Redis 和缓存状态
- **Prometheus 指标**: `/metrics/` 端点提供监控指标
- **日志轮转**: 自动轮转日志文件，保留最近 5 个备份

### 安全特性

- **速率限制**: 提交限制为 3 次/分钟
- **输入验证**: 搜索端点验证输入长度和字符
- **XSS 防护**: Markdown 渲染使用 bleach 清理 HTML
- **CSRF 保护**: 所有 POST 请求受 CSRF 保护
- **验证码**: 图形验证码（原始 + 点阵双风格）+ ALTCHA 隐藏工作量证明，登录失败/注册/重置密码/高频提交/头像访问时要求

### 支持的编程语言

| 语言 | 编译方式 | 运行时 |
|------|---------|--------|
| C | `gcc -O2` | 原生执行 |
| C++ | `g++ -std=c++17 -O2` | 原生执行 |
| Python | — | `python3` |
| Java | `javac` | `java` |
| JavaScript | — | `node` |
| Go | `go build` | 原生执行 |
| Rust | `rustc --edition=2021` | 原生执行 |
| Ruby | — | `ruby` |
| Kotlin | `kotlinc` | `java -jar` |
| Assembly | `as` + `ld` | Linux 原生 |

## 验证码系统

项目在图形验证码之上叠加了一层**非交互式的隐藏工作量证明验证码（ALTCHA v2）**。凡是需要验证码的场景，图形验证码与 ALTCHA **始终同时出现、同时校验**，不允许单独出现。

### 图形验证码

两种渲染风格按 50/50 随机切换（`users/captcha.py`）：

- **原始风格**：多字体（DejaVu / Liberation / FreeSans）+ 逐字符旋转、剪切、缩放 + 波纹扭曲、干扰线、噪声等抗 OCR 处理。
- **点阵风格**：内置点阵字体 `users/fonts/ZenDots-Regular.ttf`（Google Fonts，SIL OFL 许可）渲染为圆点字符，复用同一套抗 OCR 管线。

两者均采用一次性消费（防重放）、每 IP 限流与缓存 TTL 到期失效。

### ALTCHA 隐藏验证码

- 使用官方 [`altcha`](https://pypi.org/project/altcha/) v2 库实现工作量证明（PoW），参数集中在 `users/altcha.py`：

  ```python
  ALGORITHM = 'PBKDF2/SHA-512'   # 可选 'PBKDF2/SHA-256' / 'ARGON2ID' / 'SCRYPT'
  KDF_COST = 30000               # PBKDF2 迭代次数
  COUNTER_MIN = 10               # 确定性 counter 范围（决定求解工作量）
  COUNTER_MAX = 50
  DEFAULT_TTL_SECONDS = 600      # 挑战 10 分钟内有效，一次性防重放
  ```

- 客户端在 `static/js/altcha-worker.js` 的 Web Worker 中用 WebCrypto 原生 `crypto.subtle.deriveBits` 求解，不阻塞主线程（无需 WASM）。若切换为 `ARGON2ID`，则服务端需安装 `argon2-cffi`，并使用对应的 WASM 求解器。
- 服务端验证为 O(1)：通过 HMAC `keySignature` 校验解出的密钥，无需重算 KDF；并带 nonce 一次性防重放。

### 触发位置

| 场景 | 图形验证码 | ALTCHA |
|------|:---------:|:------:|
| 登录失败后（默认 1 次失败即触发） | ✅ | ✅ |
| 注册 | ✅ | ✅ |
| 重置密码（请求 + 确认两步） | ✅ | ✅ |
| 提交频率超阈值 | ✅ | ✅ |
| 头像高频访问 | ✅ | ✅ |
| 全站高风险模式（所有 POST） | ✅ | ✅ |

## 生产部署建议

### 管理后台

访问 http://127.0.0.1:8000/admin 进入管理后台，使用超级用户账号登录。

### 添加题目

1. 登录管理后台
2. 进入 "Problems" -> "Problems"
3. 点击 "Add problem"
4. 填写题目信息：
   - 标题
   - 题目描述
   - 输入格式
   - 输出格式
   - 样例输入/输出
   - 提示（可选）
   - 难度
   - 时间限制（毫秒）
   - 内存限制（MB）
   - 标签
5. 保存

### 用户功能

- 注册账号后可以浏览题目
- 登录后可以提交代码
- 在个人主页查看提交记录和已解决的题目
- 在排行榜查看排名

## 注意事项

1. 本项目使用 PostgreSQL 作为默认数据库。
2. SECRET_KEY 需要在生产环境中更改。
3. 建议在生产环境中配置 ALLOWED_HOSTS。

## 开发计划

- [x] 实现自动化代码评测系统
- [x] 添加更多编程语言支持 (JavaScript/Go/Rust/Ruby/Kotlin...)
- [x] 判题 Docker 镜像按语言拆分
- [x] 多判题机分布式部署 (Multi-Judge)
- [x] Docker 容器池
- [x] 中央判题队列 + 原子抢占/租约心跳/栅栏写回 + 僵尸任务回收
- [x] DB-less 测评机（无 PostgreSQL 凭证，HTTP 抢占 + 结果队列幂等写回）
- [x] 添加比赛功能
- [x] 植入AI解题功能
- [x] 文档搜索引擎
- [x] 添加题解功能
- [x] 优化移动端体验
- [ ] 优化Windows支持（Worker Class重构……）

## 许可证

MIT License

## 联系方式

如有问题或建议，欢迎提 Issue。

## Star History

<a href="https://www.star-history.com/?repos=alphadamn%2Fguwu-oj&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&theme=dark&legend=top-left&sealed_token=yqA0UZU3rN-0gv3AQcGczh_JbALQAu_GVP0W649r7Fmb5fyVOzScWkdSCbrBAZl0Mr4MeCD4knhWPpDI8rZ2uyX2bhr45-LsZV66D8Nws7YrxMjbk51Srg" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&legend=top-left&sealed_token=yqA0UZU3rN-0gv3AQcGczh_JbALQAu_GVP0W649r7Fmb5fyVOzScWkdSCbrBAZl0Mr4MeCD4knhWPpDI8rZ2uyX2bhr45-LsZV66D8Nws7YrxMjbk51Srg" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=alphadamn/guwu-oj&type=date&legend=top-left&sealed_token=yqA0UZU3rN-0gv3AQcGczh_JbALQAu_GVP0W649r7Fmb5fyVOzScWkdSCbrBAZl0Mr4MeCD4knhWPpDI8rZ2uyX2bhr45-LsZV66D8Nws7YrxMjbk51Srg" />
 </picture>
</a>
