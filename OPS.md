# 谷物 OJ 运维常用指令手册

> 适用主机：web 源站（/www/wwwroot/guwu-oj）。判题机相关命令需在对应判题主机上执行，已单独标注。
> 最后核对：2026-09-16。

## 0. 环境速查

| 项 | 值 |
| --- | --- |
| 代码目录 | `/www/wwwroot/guwu-oj` |
| Python 虚拟环境 | `/www/wwwroot/guwu-oj/venv` |
| Web 服务 | `guwu-oj.service`（Granian，12 workers，UDS `/run/guwu-oj/guwu-oj.sock`） |
| WebSocket 服务 | `guwu-oj-ws.service`（Granian ASGI，仅监听 127.0.0.1:8447，nginx `/ws/` 反代） |
| 判题 worker（判题机） | `guwu-oj-judge-worker.service`，单进程线程池并发 `OJ_JUDGE_CONCURRENCY`（默认 4） |
| 判题机 | judge-1 `64.90.3.112`（权重 3）、judge-2 `192.168.196.147`（权重 2）、judge-3 已停用 |
| 队列优先级（高→低） | `{base}-pro` → `{base}-plus` → `{base}`（免费）→ `{base}-ai`（AI 验证） |
| PostgreSQL | `ojdb` @ 127.0.0.1:5432（TLS），库用户走 `.env` |
| Redis | 本机 db1：缓存/限流；判题机 db0：RQ 队列 |
| 管理命令入口 | `venv/bin/python manage.py ...` |

通用约定：

```bash
cd /www/wwwroot/guwu-oj
# 所有 manage.py 命令均用 venv 解释器，不要直接用系统 python
venv/bin/python manage.py <command>
```

> 改动后端 Python 代码后需 `systemctl restart guwu-oj`；仅改模板/静态文件通常无需重启（静态文件需 collectstatic）。

---

## 1. 服务管理

```bash
# Web（源站）
systemctl status guwu-oj
systemctl restart guwu-oj
journalctl -u guwu-oj -f                      # 实时日志
journalctl -u guwu-oj --since "30 min ago"    # 最近 30 分钟

# WebSocket 实时状态（提交详情页）
systemctl status guwu-oj-ws
journalctl -u guwu-oj-ws -f

# Nginx（宝塔环境）
nginx -t && nginx -s reload

# 判题机上执行
systemctl status guwu-oj-judge-worker
systemctl restart guwu-oj-judge-worker
journalctl -u guwu-oj-judge-worker -f
```

判题机健康巡检（在 web 上执行，会通过 Redis 探测各判题机的 redis/worker 心跳；在 worker 角色上还会检查 docker 与镜像）：

```bash
venv/bin/python manage.py check_judge_health
```

健康检查接口 `/health/` 仅 staff 登录用户可访问（未登录会跳转/403），命令行巡检优先用上面的管理命令。

---

## 2. 题库查询（testcase / 标签 / 来源）

均通过 Django shell 执行，只读不写。

### 2.1 列出所有 testcase 数量小于 N 的题目

```bash
venv/bin/python manage.py shell -c "
from problems.models import Problem
from django.db.models import Count
N = 3
qs = (Problem.objects.annotate(tc=Count('test_cases'))
        .filter(tc__lt=N).order_by('id'))
print('count =', qs.count())
for p in qs:
    print(f'P{p.id}\t{p.tc}\t{p.title}')
"
```

只统计数量：

```python
Problem.objects.annotate(tc=Count('test_cases')).filter(tc__lt=N).count()
```

> 参考快照（2026-09-16）：题库 30000 题，其中 testcase < 3 的有 14178 题（多为第三批弱测试导入题）。

### 2.2 其他常用题库筛查

```bash
venv/bin/python manage.py shell
```

```python
from problems.models import Problem, TestCase
from django.db.models import Count

# 完全没有测试用例的题
Problem.objects.annotate(tc=Count('test_cases')).filter(tc=0)

# 没有样例（is_sample=True）的题
Problem.objects.exclude(test_cases__is_sample=True).distinct()

# 缺算法标签（只有 cf:/cc:/taco:/ht:/usaco: 等来源键）的题数
from problems.tag_complete import count_incomplete_problems
count_incomplete_problems()

# 按题源前缀统计（cf/cc/taco/ht/usaco）
from collections import Counter
c = Counter()
for (tags,) in Problem.objects.values_list('tags'):
    for t in (tags or '').replace('，', ',').replace('、', ',').replace(';', ',').split(','):
        t = t.strip()
        if ':' in t:
            c[t.split(':', 1)[0]] += 1
print(c)

# 按难度统计
for row in Problem.objects.values_list('difficulty').order_by().annotate(n=Count('id')):
    print(row)

# 把 tc<N 的题号导出到文件（shell 内）
import csv
with open('/tmp/weak_problems.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['id', 'title', 'testcase_count'])
    for p in Problem.objects.annotate(tc=Count('test_cases')).filter(tc__lt=N).order_by('id'):
        w.writerow([p.id, p.title, p.tc])
```

标签补全（DeepSeek 中文算法标签）走 **Admin → 题目 → 补全标签** 页面（后台批量任务，词表与提示词可配），命令行单次试跑可用：

```python
from problems.tag_complete import incomplete_problems, collect_vocabulary
len(incomplete_problems(limit=100))   # 只看前 100 道缺标签题，不写库
```

### 2.3 标签筛选相关缓存

题库标签目录全表扫描约 0.3s，缓存键 `problem_tag_catalog`（10 分钟）。改了标签数据后想立刻生效：

```python
from django.core.cache import cache
cache.delete('problem_tag_catalog')
```

---

## 3. 提交记录与判题队列

```bash
venv/bin/python manage.py shell
```

```python
from django.utils import timezone
from datetime import timedelta
from django.db.models import Count
from submissions.models import Submission

# 当前积压（Pending）
Submission.objects.filter(status='Pending').count()

# 最近 1 小时提交量
Submission.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count()

# 最近 24 小时各状态分布
since = timezone.now() - timedelta(hours=24)
for row in (Submission.objects.filter(created_at__gte=since)
            .values('status').annotate(n=Count('id')).order_by('-n')):
    print(row)

# 系统错误（判题环境问题，重点排查）
list(Submission.objects.filter(status='System Error')
     .order_by('-created_at').values_list('id', 'user__username', 'created_at')[:20])
```

查看各判题机四个优先级队列的实时积压：

```bash
venv/bin/python manage.py shell -c "
from submissions.judge_load_balancer import load_balancer
for m in load_balancer.get_enabled_machines():
    r = load_balancer._machine_redis(m, decode_responses=True)
    base = m['queue']
    for q in (base+'-pro', base+'-plus', base, base+'-ai'):
        print(m['name'], q, r.llen('rq:queue:'+q))
"
```

RQ 统计（web 本机的 default/high/low/ai 回退队列）：

```bash
venv/bin/python manage.py rqstats          # 一次
venv/bin/python manage.py rqstats -i 2     # 每 2 秒刷新
venv/bin/python manage.py rqstats -j       # JSON
```

### 3.1 卡住的 Pending 重新入队

worker 宕机/重启后，长时间停留在 Pending 的提交不会自动重投，可手动重新入队：

```python
from django.utils import timezone
from datetime import timedelta
from submissions.models import Submission
from submissions.judge_queue import enqueue_judge

cutoff = timezone.now() - timedelta(minutes=10)
ids = list(Submission.objects.filter(status='Pending', created_at__lt=cutoff)
           .values_list('id', flat=True))
for sid in ids:
    enqueue_judge(sid)
print('requeued', len(ids))
```

> 先确认积压原因（判题机健康/队列长度），否则重投只会再次堆积。

### 3.2 停用/启用判题机（立即生效，无需重启）

```python
from submissions.models import JudgeMachine
m = JudgeMachine.objects.get(name='judge-3')
m.enabled = False          # 停止派发；True 恢复
m.save()
```

### 3.3 AI 判题服务账号巡检（硬约束）

```python
from django.contrib.auth import get_user_model
U = get_user_model()
bot = U.objects.get(username='__ai_judge_bot__')
assert bot.is_active is False          # 必须停用，禁止登录
```

---

## 4. 流量高峰期应对

按「先观测 → 再收紧 → 最后扩容」的顺序处理。

### 4.1 观测（只读）

```bash
# 队列积压（见第 3 节，持续刷新）
venv/bin/python manage.py rqstats -i 2

# Web 资源
free -h
ss -xln | grep guwu-oj.sock           # Recv-Q 持续 >0 说明应用 accept 不动
cat /sys/fs/cgroup/system.slice/guwu-oj.service/memory.events 2>/dev/null | grep -E 'high|max'
                                       # high 计数暴涨 = 被 cgroup 内存限流（曾导致全站 504）
journalctl -u guwu-oj --since "10 min ago" | tail -100
tail -f /www/wwwroot/guwu-oj/logs/granian.access.log

# 判题机上
docker stats                           # 判题容器 CPU/内存
journalctl -u guwu-oj-judge-worker -f
```

### 4.2 应用层收紧（改配置立即生效，无需重启）

验证码/提交频率均为数据库单例配置（Admin → 开发日志 → 验证码配置），命令行等价操作：

```python
from devlog.models import CaptchaConfig
c, _ = CaptchaConfig.objects.get_or_create(pk=1)   # 单例固定 pk=1

# 高风险时期：所有 POST 强制图形验证码（发帖/提交等）
c.captcha_require_on_all_post = True

# 收紧高频提交验证码阈值（默认每 60 分钟 30 次）
c.captcha_submission_captcha_enabled = True
c.captcha_submission_limit = 10
c.captcha_submission_window_minutes = 60
c.save()
```

高峰期过后恢复：`captcha_require_on_all_post = False`、阈值改回 30，再 `c.save()`。

其他现成防护层（高峰时确认开启即可，一般不要临时改）：

- 登录/注册/忘记密码验证码、头像高频访问验证码（同一单例）。
- 全局限流：Redis ZSET 滑动时间窗口（本机 db1）。
- WAF：雷池 SafeLine（docker，`docker ps | grep safeline`）；aiwaf Django 应用：
  ```bash
  venv/bin/python manage.py aiwaf_list
  venv/bin/python manage.py diagnose_blocking
  ```
- 双 CDN：`.cn` 走 Cloudflare，`.com` 走阿里云 CDN；静态资源 cookie-free 由 CDN 缓存，源站压力主要来自动态页与提交。

### 4.3 扩容（谨慎）

- **判题能力**：在判题机上修改 `/root/guwu-oj/.env`（或部署目录下 `.env`）中的 `OJ_JUDGE_CONCURRENCY`（单 worker 内线程数），然后 `systemctl restart guwu-oj-judge-worker`。总产能 = 机器数 × 每机并发。需配合判题机 CPU/核数与 docker 容量，不要超过物理能力。
- **Web workers**：Granian `--workers 12` 与 unit 的 `MemoryHigh=2200M / MemoryMax=3200M` 是配套的。**加 worker 必须同步调大两个内存阈值**（每 worker 常态约 150MB），否则会被 memcg 限流导致两个 CDN 域名同时 504（2026-09-13 事故）。改 unit 后：
  ```bash
  systemctl daemon-reload && systemctl restart guwu-oj
  ```
- 临时性算力补充可在 Admin 中启用备用判题机（如 judge-3），见 3.2。

---

## 5. 缓存与 Redis

缓存走本机 Redis db1，连接参数在 `.env`（密码含 `%&#?+` 等特殊字符，**不要直接 `source .env`**，shell 内取数最稳妥）：

```python
from django.core.cache import cache
cache.clear()                              # 清空全库缓存（所有用户下次请求回源，慎用）

# 精确失效常用键
for k in ['problem_tag_catalog', 'home_stats', 'home_recent_problems',
          'leaderboard_users', 'problem_list_version']:
    cache.delete(k)
```

需要直连 Redis（看滑窗 ZSET 等）时：

```python
from django_redis import get_redis_connection
r = get_redis_connection('default')       # 即本机 db1
r.dbsize()
r.scan_iter(match='*', count=100)         # 浏览键，勿在高峰用 keys *
```

判题机 Redis（db0）的连接请复用 `load_balancer._machine_redis(machine)`，不要手抄密码。

---

## 6. 日志位置

| 日志 | 路径 / 命令 |
| --- | --- |
| Web 标准输出 | `journalctl -u guwu-oj` |
| Granian 访问/错误 | `/www/wwwroot/guwu-oj/logs/granian.{access,error}.log` |
| Django 业务日志 | `/www/wwwroot/guwu-oj/logs/django.log`、`django_error.log` |
| 文件变更扫描（每 10 分钟） | `/www/wwwroot/guwu-oj/logs/file-scan.log` |
| Nginx（宝塔） | 两域名共用 edge_json 日志：`/www/wwwlogs/guwu.camluni.cn_4449.log`（错误日志同名 `.error.log`；防火墙 learn 依赖其中的 peer/ali_cdn 字段） |
| 判题 worker | 判题机上 `journalctl -u guwu-oj-judge-worker` |

---

## 7. 数据库

```bash
# 进入 psql（自动带 TLS 与 .env 凭据）
venv/bin/python manage.py dbshell

# 迁移
venv/bin/python manage.py migrate
venv/bin/python manage.py showmigrations

# 整库/表备份优先用后台：Admin → 开发日志 → 站点配置 → 数据库备份/恢复
# （备份目录在 SiteConfig.database_backup_dir 配置）
```

`dumpdata` 仅适合小表导出，题库 testcase 数据量很大（33 万+ 测试点），不要用它做全库备份：

```bash
venv/bin/python manage.py dumpdata problems.Problem --indent 2 -o /tmp/problems.json
```

```bash
./backup.sh
```

---

## 8. 题库导入脚本

脚本在 `scripts/`，全部幂等（各有来源幂等键），**先 `--dry-run` 再正式跑**，统一以 `oscar` 为 created_by：

| 脚本 | 数据源 | 幂等键 | 常用参数 |
| --- | --- | --- | --- |
| `import_codeforces.py` | HF open-r1/codeforces | `cf:<id>` | `--include-incomplete --min-tests N`、`--limit`、`--split train\|test\|both`、`--include-checker` |
| `import_code_contests.py` | HF deepmind/code_contests | `cc:<sha1>` | `--target-total`、`--min-tests`、`--limit` |
| `import_taco.py` | HF BAAI/TACO | `taco:<sha1>` | `--target-total`、`--min-tests`、`--limit` |
| `import_usaco.py` | HF usacobench_formatted | `usaco:<sha1>` | `--limit` |
| `import_hardtests.py` | HF sigcp/hardtests_problems | `ht:<sha1>` | `--skip-platforms codeforces`、`--target-total`、`--min-tests` |
| `import_apps.py` | HF codeparrot/apps | — | `--dry-run`（历史评估无净新增） |

```bash
# 示例：先演练，再只导入 hardtests 中非 CF 平台、≥5 个测试点的题，总量补到 31000
venv/bin/python scripts/import_hardtests.py --dry-run --skip-platforms codeforces --min-tests 5
venv/bin/python scripts/import_hardtests.py --skip-platforms codeforces --min-tests 5 --target-total 31000

# 所有参数以 --help 为准
venv/bin/python scripts/import_hardtests.py --help
```

parquet 本地缓存位于 `scripts/.cache/<数据集>/`。导入完成后按需在 Admin 触发标签补全。

---

## 9. 定时任务

django-crontab 注册的两个任务（由系统 crontab 每 30 分钟 / 每 5 分钟驱动）：

- `*/30 * * * *` devlog 组件状态刷新
- `*/5 * * * *` 比赛结束自动发布 `contests.jobs.publish_finished_contests_job`

```bash
venv/bin/python manage.py crontab show     # 查看已注册任务及 hash
venv/bin/python manage.py crontab add      # 重新写入系统 crontab（改 CRONJOBS 后）
venv/bin/python manage.py crontab remove

# 手动补跑“发布已结束比赛”
venv/bin/python manage.py publish_finished_contests
```

另有系统级 cron：`scripts/scan-file-changes.sh`（每 10 分钟，日志见第 6 节）。

---

## 10. 防火墙 / CDN

CDN 白名单脚本 `/root/firewall/cdn-whitelist.sh`（ipset + 自定义链，仅管控入向 tcp 443/8445；80 对全网开放）：

```bash
/root/firewall/cdn-whitelist.sh              # 查看状态与当前模式（log/drop）
/root/firewall/cdn-whitelist.sh apply log    # 切观察模式：非白名单仅记录不拦截
/root/firewall/cdn-whitelist.sh apply drop   # 切拦截模式（当前为 drop）
/root/firewall/cdn-whitelist.sh learn        # 从 nginx edge_json 日志学习阿里云回源段
/root/firewall/cdn-whitelist.sh panic-open   # 应急：一键摘除，80/443 立即全网可达
```

> 切 drop 前必须先 `learn` 覆盖阿里云动态回源段，否则 .com 域名可能 502。应急恢复优先用 `panic-open`，事后再 `apply log` 挂回观察。

Nginx 站点配置：

- vhost：`/www/server/panel/vhost/nginx/guwu.camluni.cn.conf`、`guwu.camluni.com.conf`
- 公共片段：`guwu-edge-common.inc`（`.inc` 后缀避开 vhost glob，勿改名）
- 阿里云回源 realip：`/root/firewall/ali-nginx-realip.conf`（learn 生成）
- 改完务必 `nginx -t && nginx -s reload`

证书：`/etc/letsencrypt/live/guwu.camluni.com/`（注意到期时间，.well-known 续期经 CDN）。

---

## 11. 判题容器与镜像（判题机上）

```bash
docker ps                                          # 运行中的匿名判题容器
docker images | grep -E 'guwu|judge'              # 各语言判题镜像

# 清理超时残留的判题容器（web 代码也会自动调，手工兜底用）
cd /root/guwu-oj && venv/bin/python manage.py shell -c "
from submissions.docker_cleanup import cleanup_stale_judge_containers
print(cleanup_stale_judge_containers())
"

# 重建/部署镜像（脚本在代码仓库 scripts/，判题机上）
bash scripts/build-judge-image.sh
bash scripts/deploy-judge.sh
```

---

## 12. 发布变更检查清单

1. `venv/bin/python manage.py check`
2. 有模型变更：`venv/bin/python manage.py migrate`
3. 有静态文件变更：`venv/bin/python manage.py collectstatic --no-input`
4. 改了后端代码：`systemctl restart guwu-oj`
5. 改了 nginx：`nginx -t && nginx -s reload`
6. 验证：`venv/bin/python manage.py check_judge_health`，浏览器经 CDN 访问两个域名各打开一个动态页和一个静态资源
