# 为 Submission 模型添加评测时长列

> **Workspace:** /www/wwwroot/guwu-oj

---

**User**

在submission数据库模型中添加方便未来继续优化测评系统以及分析评测瓶颈用的时长列（如：enqueued_at、claimed_at、judge_started_at、compile_done_at、tests_done_at、result_written_at……）

**Assistant**

I'll explore the submission model and judge pipeline first.

我将开始研究这个代码库。首先并行探索项目结构和关键文件。

这是一个 Django 项目。核心文件在 `submissions/` 应用下。让我并行读取模型定义和判题管道的关键文件。

现在读取判题管道的核心文件：任务、判题核心、结果写回等。

现在读取主判题逻辑 `judge.py`、提交视图 `views.py` 和 HTTP claim 接口 `worker_api.py`。

现在读取提交创建/入队视图、HTTP 内部接口、结果消费命令和相关迁移文件。

研究已完成。以下是针对 `/www/wwwroot/guwu-oj` 的完整、详尽报告，覆盖了你提出的全部 6 个问题。所有路径均为绝对路径，行号均基于当前磁盘上的文件内容，未对任何文件做修改。

---

## 总览：ORM / DB 层

- 这是一个 **Django 5.x 项目**（`venv` 中为 Django 5.2.x），使用 **Django ORM**（`django.db.models`）。
- 生产数据库是 **PostgreSQL**（`django.db.backends.postgresql`）；`DEMO_MODE` 下退化为 SQLite；DB-less 判题 worker 角色使用一个惰性 SQLite 占位库。
- 迁移系统是 **Django 内置 migrations**（`django.db.migrations`），不是 umzug/knex/alembic/sequelize。
- 判题任务队列是 **RQ / django-rq**（Redis），另有 Redis List 作为结果队列。
- 核心 app 是 `submissions`。

---

## 1. Submission 模型定义

**文件：** `/www/wwwroot/guwu-oj/submissions/models.py`
**类 `Submission`：** 第 14–127 行

字段逐一（含类型、索引、约束）：

| 字段 | 定义（行号） | 说明 |
|---|---|---|
| `LANGUAGE_CHOICES` | 15–26 | C, C++, Python, Java, JavaScript, Golang, Rust, Ruby, Kotlin, Assembly |
| `STATUS_CHOICES` | 28–37 | 见第 6 节 |
| `STATE_PENDING/QUEUED/JUDGING/DONE/FAILED` | 42–46 | 生命周期状态常量 |
| `JUDGE_STATE_CHOICES` | 47–53 | 生命周期枚举 |
| `TERMINAL_JUDGE_STATES` | 54 | `(DONE, FAILED)` |
| `problem` | 56 | `ForeignKey(Problem, null=True, blank=True, on_delete=CASCADE, related_name='submissions')` |
| `user` | 57 | `ForeignKey(User, on_delete=CASCADE, related_name='submissions')` |
| `contest_problem` | 58–61 | `ForeignKey('contests.ContestProblem', null=True, blank=True, on_delete=SET_NULL, related_name='submissions', db_index=True)` |
| `code` | 62 | `TextField()` |
| `language` | 63 | `CharField(max_length=32, choices=LANGUAGE_CHOICES)` |
| `status` | 64 | `CharField(max_length=30, choices=STATUS_CHOICES, default='Pending')` |
| `runtime` | 65 | `IntegerField(blank=True, null=True)`，单位毫秒 |
| `memory` | 66 | `IntegerField(blank=True, null=True)`，单位 KB |
| `created_at` | 67 | `DateTimeField(auto_now_add=True)` |
| `judge_state` | 70–73 | `CharField(max_length=10, choices=JUDGE_STATE_CHOICES, default=STATE_PENDING, db_index=True)` |
| `worker_id` | 74 | `CharField(max_length=128, blank=True, default='')` |
| `claim_token` | 75 | `UUIDField(null=True, blank=True, unique=True)` |
| `claimed_at` | 76 | `DateTimeField(null=True, blank=True)` |
| `heartbeat_at` | 77 | `DateTimeField(null=True, blank=True)` |
| `finished_at` | 78 | `DateTimeField(null=True, blank=True)` |

`Meta`（第 80–90 行）：`ordering = ['-created_at']`，`verbose_name/verbose_name_plural = '提交记录'`，索引 `models.Index(fields=['judge_state', 'heartbeat_at'], name='subm_judgestate_hb_idx')`。

其他成员：`effective_problem` 属性（92–104）、`__str__`（106–108）、重写的 `save()`（110–126，保存后清理 `problem_pass_rate_*` 缓存与 Redis 的 `problem_list_query_*` 键）。

同文件另有相关模型：
- **`SubmissionTestResult`**（129–160）：`submission` FK、`test_case` FK、`contest_test_case` FK、`case_index`、`status`、`runtime`、`actual_output`、`expected_output`、`error_message`；`unique_together = [['submission', 'case_index']]`，`ordering=['case_index']`。
- **`JudgeMachine`**（163–208）：评测机配置（host/port/db/queue/enabled/weight/TLS 证书路径/加密 Redis 密码），含 `set_redis_password/get_redis_password`（Fernet）。
- **`JudgeConfig`**（211–250）：全局评测配置（`subprocess_timeout_sec`、`updated_at=auto_now`），保存时用 `select_for_update` 做单行锁。注意：代码里多处调用 `JudgeConfig.get_solo()`（如 `/www/wwwroot/guwu-oj/submissions/internal_views.py:123`），但该模型并未定义 `get_solo` 类方法（`get_solo` 只定义在 `points/models.py:39`、`ai_assistant/models.py:36`）——这些调用点都包在 `try/except` 里，失败会回退到 settings 默认值。

---

## 2. 时间戳 / 时长字段（Submission 及相关判题模型）

**Submission 上的时间戳/时长：**
- 时长类：`runtime`（毫秒，models.py:65）、`memory`（KB，models.py:66）。
- 时间戳：`created_at`（auto_now_add，models.py:67）、`claimed_at`（76）、`heartbeat_at`（77）、`finished_at`（78）。
- **没有 `updated_at` 字段。** 也没有 `judge_time` / `time_used` / `memory_used` 等命名。

**历史上存在但已被删除的字段：**
- `judge_started_at` 在迁移 `0014_submission_judge_started_at.py`（第 13–17 行）中添加，随后在 `0015_remove_submission_submissions_status_75026c_idx_and_more.py`（第 17–20 行）中被移除；当前模型与代码中已无任何引用（全仓库仅在 0014/0015 两个迁移文件中出现）。

**队列 / worker 相关字段：**
- Submission：`judge_state`、`worker_id`、`claim_token`、`claimed_at`、`heartbeat_at`、`finished_at`（models.py:70–78）。
- JudgeMachine：`queue`、`host`、`port`、`db`、`weight`、`enabled`（models.py:165–171）。
- JudgeConfig：`updated_at`（auto_now，models.py:217）。
- 重试计数不落库，而是存在 Redis：`/www/wwwroot/guwu-oj/submissions/claiming.py:300-315`（键 `oj:judge:attempts:{submission_id}`，TTL 3600s，最多 `DEFAULT_MAX_ATTEMPTS = 3`）。

**SubmissionTestResult：** 只有 `runtime`（models.py:150），无时间戳。

---

## 3. 判题 / 队列流水线（生命周期事件 + 写库方式）

### 3.1 入队（enqueue）

- 普通提交：`/www/wwwroot/guwu-oj/submissions/views.py:80-94` —— `Submission.objects.create(...status='Pending')`（80–86），随后 `enqueue_judge(submission.id)`（94）。
- 竞赛提交：`/www/wwwroot/guwu-oj/contests/views.py:136-145` —— `Submission.objects.create(...)`（136）后 `enqueue_judge`（145）。
- AI 判题工具：`/www/wwwroot/guwu-oj/ai_assistant/judge_tool.py:232-239` —— 在 `transaction.atomic()` 内 `create`，`transaction.on_commit(lambda: enqueue_judge(...))`（239）。
- 健康检查：`/www/wwwroot/guwu-oj/devlog/views.py:450-458`。

**入队函数：** `/www/wwwroot/guwu-oj/submissions/judge_queue.py`
- `enqueue_judge(submission_id)` 定义于 144–251。
- 优先级解析 `resolve_submission_priority`（47–69），层级 Pro>Plus>Free>AI（19–38）。
- 关键：165 行调用 `mark_queued()`（PENDING→QUEUED 的原始 SQL 门禁），终态行被丢弃。
- 三条投递路径：中央 broker 队列（175–201，`judge:queue{suffix}`）、多机按权重负载均衡（203–237，`load_balancer.reserve_machine`）、单机回退队列（239–248，`high/default/low/ai`）。
- 任务函数是 `judge_submission_task`（`judge_queue.py:9` 导入）。

### 3.2 认领（claim / 出队）

- 入口：`/www/wwwroot/guwu-oj/submissions/tasks.py` 的 `judge_submission_task`（53–74）→ 根据 `OJ_WORKER_DBLESS` 分流到 `_judge_task_db`（220）或 `_judge_task_dbless`（111）。
- **DB 路径**：`tasks.py:233` 调用 `claim_submission(submission_id, worker_id)`；`tasks.py:245-246` 启动 `Claim` 心跳线程；`tasks.py:250` 调用 `judge_submission(submission_id, claim=claim)`。
- **DB-less 路径**：`tasks.py:131` 通过 HTTP `client.claim(...)`（`worker_api.py:126-132`）→ 服务端 `/www/wwwroot/guwu-oj/submissions/internal_views.py` `claim_view`（73–142），其内部 87 行调用 `claim_submission`，并一次性返回代码、时限、测试数据（130–142）。

**原子认领实现：** `/www/wwwroot/guwu-oj/submissions/claiming.py`
- `claim_submission`（65–95）：原始 SQL `UPDATE submissions_submission SET judge_state='JUDGING', worker_id, claim_token, claimed_at, heartbeat_at WHERE id=%s AND judge_state IN ('PENDING','QUEUED') RETURNING claim_token`。
- `heartbeat_claim`（98–110）：`UPDATE ... SET heartbeat_at WHERE id AND claim_token AND judge_state='JUDGING'`。
- `finalize_claim`（113–141）：终局写回，`UPDATE ... SET status, judge_state, finished_at, runtime, memory WHERE id AND claim_token AND judge_state='JUDGING'`。
- `requeue_claim`（144–164）：`SET judge_state='QUEUED', worker_id='', claim_token=NULL, claimed_at=NULL, heartbeat_at=NULL`。
- `mark_queued`（167–181）。
- `Claim` 句柄类（184–252，含后台心跳线程与 `ensure_alive`）。
- `reap_stale_claims`（255–297）：回收心跳过期的 JUDGING 与超时未认领的 QUEUED。
- 文档说明：并发安全依赖 PostgreSQL 的行锁 `UPDATE ... RETURNING`，因此刻意用原始 SQL 而非 ORM（1–25 行注释）。

### 3.3 判题开始 / 编译结束 / 测试结束 / 写回（DB 路径）

**文件：** `/www/wwwroot/guwu-oj/submissions/judge.py`
- 主入口 `judge_submission(submission_id, claim=None)`：587–769。
  - 594 行先删除旧的部分用例结果：`SubmissionTestResult.objects.filter(submission=submission).delete()`。
  - 容器获取 621–639；`SandboxRunner` 上下文 644–656。
  - **编译阶段**：657–720 分语言调用 `runner.compile_cpp/compile_c/...`（各 `compile_*` 定义在 263–417），编译失败即调用 `save_compile_error(...)`。
  - **测试阶段**：723–761 逐用例循环，每个用例前 726–727 `claim.ensure_alive()`；结果用 `save_case_result(...)`（757–760）落库。
  - **判定汇总**：763–764 `finalize_submission(...)`。
- 写库辅助函数：
  - `save_case_result`（456–478）：`SubmissionTestResult.objects.update_or_create(submission=..., case_index=..., defaults={...})`——幂等。
  - `commit_verdict`（481–519）：无 claim 时走 ORM `submission.save(update_fields=[...])`（490–502）；有 claim 时走 `finalize_claim(...)` 原始 SQL（504–507），失败抛 `ClaimLostError`（508–512），成功后把内存对象上的 `status/judge_state/runtime/memory` 同步（513–518）。
  - `finalize_submission`（521–560）：选出最终 verdict，调 `commit_verdict`；Accepted 时附加副作用 `solved_problems.add` 与 `apply_points`（545–560）。
  - `save_compile_error`（563–570）：写一条 "Skipped" 首用例 + `commit_verdict("Compile Error")`。

**任务侧收尾：** `/www/wwwroot/guwu-oj/submissions/tasks.py:262-269` —— 清除重试计数并 `publish_submission_changed(submission_id)`（因原始 UPDATE 绕过 `post_save`）。

### 3.4 判题开始 / 编译 / 测试 / 写回（DB-less 路径）

- 纯判题核心（零 ORM）：`/www/wwwroot/guwu-oj/submissions/judge_core.py` —— `judge_spec`（77–177），编译分支 `_compile`（180–254），结果归一化 `_finalize_outcome`（257–283）。
- worker 把结果推到 Redis List：`tasks.py:207-210` → `/www/wwwroot/guwu-oj/submissions/result_queue.py:push_envelope`（40–43，键 `judge:result`）。
- Web 侧消费者命令：`/www/wwwroot/guwu-oj/submissions/management/commands/consume_judge_results.py`（`handle` 33–75，`BRPOPLPUSH`+ack+死信）。
- 结果持久化：`/www/wwwroot/guwu-oj/submissions/results.py`
  - `process_envelope`（210–251）：解析信封；`kind=='infra'` → `schedule_retry_or_fail`；`kind=='result'` → `write_judge_outcome`。
  - `write_judge_outcome`（66–137）：在 `transaction.atomic()` 内先 `finalize_claim` 做围栏（92–95），再 `SubmissionTestResult.objects.filter(...).delete()`（103）+ 逐条 `objects.create(...)`（112–121），Accepted 时 `grant_accepted_rewards`（44–63）。
  - `fail_unclaimed`（140–157）：原始 `UPDATE ... SET status='System Error', judge_state='FAILED', finished_at=... WHERE judge_state='QUEUED' AND claim_token IS NULL`。
  - `schedule_retry_or_fail`（160–207）：重试上限 `DEFAULT_MAX_ATTEMPTS`，耗尽后 `finalize_claim(..., failed=True)` 或 `fail_unclaimed`。

### 3.5 实时通知与回收

- `post_save` 信号：`/www/wwwroot/guwu-oj/submissions/signals.py:28-32`（连接在 `apps.py:8-12`），发布到 Redis pub/sub；`realtime.py:76-96` `publish_submission_changed`。
- 回收命令：`/www/wwwroot/guwu-oj/submissions/management/commands/reap_stale_judgments.py`（默认 judging-timeout 300s / queued-timeout 600s，可 `--loop`）。

---

## 4. 迁移文件与迁移系统

- **系统：** Django 内置 migrations（`django.db.migrations` / `manage.py makemigrations|migrate`）。
- **目录（每个 app 一个 `migrations/`）：**
  - `/www/wwwroot/guwu-oj/submissions/migrations/`（0001–0017）
  - `/www/wwwroot/guwu-oj/contests/migrations/`（0001–0004）
  - `/www/wwwroot/guwu-oj/problems/migrations/`（0001–0007）
  - `/www/wwwroot/guwu-oj/users/migrations/`（0001–0014）
  - `/www/wwwroot/guwu-oj/points/migrations/`（0001–0005）
  - `/www/wwwroot/guwu-oj/devlog/migrations/`（0001–0020）
  - `/www/wwwroot/guwu-oj/ai_assistant/migrations/`（0001–0003）
- **命名约定：** `NNNN_<描述>.py`（4 位序号，Django 自动生成描述性后缀，如 `0017_submission_claim_token_submission_claimed_at_and_more.py`）；每个目录有 `__init__.py`。
- Submission 相关迁移序列：`0001_initial` → `0002_submissiontestresult` → `0003_alter_submissiontestresult_status` → `0004/0008_alter_submission_language` → `0005_judgemachine` → `0006/0007_judgeconfig` → `0009_submission_system_error` → `0010_submission_contest_problem` → `0011_standalone_contest_submission_support` → `0012_add_contest_test_case` → `0013_pending_submission_deadline_index` → `0014_submission_judge_started_at` → `0015_remove...judge_started_at` → `0016_judge_machine_transport_security` → `0017_submission_claim_token_submission_claimed_at_and_more`。

**示例迁移全文** —— `/www/wwwroot/guwu-oj/submissions/migrations/0017_submission_claim_token_submission_claimed_at_and_more.py`（最新、也是生命周期状态机所在迁移）：

```python
# Phase 2: submission lifecycle state machine (claim / lease / fence).
#
# Adds judge_state + claim bookkeeping columns and backfills lifecycle state
# from the pre-existing verdict column. Rollback simply drops the columns; no
# historic verdict data lives in them, so no reverse data migration is needed.

from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def backfill_judge_state(apps, schema_editor):
    """Infer lifecycle state from the pre-existing verdict column.

    Any non-Pending verdict is terminal by definition (including System
    Error), so those rows settle as DONE with finished_at approximated by
    created_at. Rows still Pending predate the claim machinery and stay
    PENDING; the reaper only reclaims QUEUED/JUDGING rows, so legacy
    abandoned Pending rows are never silently re-judged.
    """
    Submission = apps.get_model('submissions', 'Submission')
    Submission.objects.exclude(status='Pending').update(
        judge_state='DONE',
        finished_at=F('created_at'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('contests', '0004_contest_entry_points_cost_contestenrollment'),
        ('problems', '0007_alter_problem_options_alter_solution_options_and_more'),
        ('submissions', '0016_judge_machine_transport_security'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='submission',
            name='claim_token',
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='claimed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='finished_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='heartbeat_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='submission',
            name='judge_state',
            field=models.CharField(
                choices=[
                    ('PENDING', 'Pending'),
                    ('QUEUED', 'Queued'),
                    ('JUDGING', 'Judging'),
                    ('DONE', 'Done'),
                    ('FAILED', 'Failed'),
                ],
                db_index=True, default='PENDING', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='submission',
            name='worker_id',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.RunPython(backfill_judge_state, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name='submission',
            index=models.Index(
                fields=['judge_state', 'heartbeat_at'],
                name='subm_judgestate_hb_idx',
            ),
        ),
    ]
```

---

## 5. DB schema 的创建 / 同步

**没有自动同步**（不存在 `sequelize.sync({alter:true})` / TypeORM `synchronize` / alembic / 原始 SQL init）。Schema 完全由 **手工执行 Django 迁移**创建：

- 应用配置 `DATABASES`：`/www/wwwroot/guwu-oj/oj_project/settings.py:583-621`
  - `DEMO_MODE` → SQLite（583–588）；
  - `OJ_ROLE=='worker' and OJ_WORKER_DBLESS` → 惰性 SQLite 占位 `/tmp/oj-dbless-dummy.sqlite3`（590–602）；
  - 其余（Web/DB worker）→ PostgreSQL，从环境变量 `DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT/DB_SSLMODE` 读取（604–621）。
- 手工执行点（均为 `manage.py migrate`）：
  - `/www/wwwroot/guwu-oj/scripts/oj-setup.sh:628-634`（`apply_migrations`，`env OJ_ROLE=web python manage.py migrate --noinput` + `collectstatic`）；
  - `/www/wwwroot/guwu-oj/OPS.md:355` 与 `:467`；
  - `/www/wwwroot/guwu-oj/README.md:94-95`（`makemigrations` + `migrate`）；
  - CI：`/www/wwwroot/guwu-oj/.github/workflows/test.yml:66`（测试前 `makemigrations && migrate`）。
- 无 `post_migrate` / `call_command('migrate')` / `connection.schema_editor` 之类的运行时建表钩子（全仓库搜索无命中）。
- `/www/wwwroot/guwu-oj/verify_setup.py`（第 66 行读 `settings.DATABASES['default']`，第 97–98 行查 `information_schema.tables`）只做校验，不建表。

---

## 6. 状态 / 状态字段取值及定义位置

三套彼此独立的枚举，都定义在 `/www/wwwroot/guwu-oj/submissions/models.py`：

1. **`Submission.STATUS_CHOICES`**（models.py:28-37）—— 面向用户/前端的判题结论，字段 `status`（默认 `'Pending'`，max_length=30）：
   `Pending`、`Accepted`、`Wrong Answer`、`Time Limit Exceeded`、`Memory Limit Exceeded`、`Runtime Error`、`Compile Error`、`System Error`。

2. **`Submission.JUDGE_STATE_CHOICES`**（models.py:42-53）—— 生命周期状态机，字段 `judge_state`（默认 `PENDING`，max_length=10，db_index）：
   - `STATE_PENDING='PENDING'`（42）：已创建未派发
   - `STATE_QUEUED='QUEUED'`（43）：在 broker 上未被认领
   - `STATE_JUDGING='JUDGING'`（44）：已被 worker 认领、心跳有效
   - `STATE_DONE='DONE'`（45）：终态（正常出结论）
   - `STATE_FAILED='FAILED'`（46）：终态（重试耗尽/基础设施失败）
   - `TERMINAL_JUDGE_STATES=(DONE, FAILED)`（54）

3. **`SubmissionTestResult.CASE_STATUS_CHOICES`**（models.py:130-137）—— 单测试点状态，字段 `status`：
   `Accepted`、`Wrong Answer`、`Time Limit Exceeded`、`Memory Limit Exceeded`、`Runtime Error`、`Skipped`。

这些枚举在迁移中被复制存档：`0001_initial.py:24`（旧 status choices）与 `0017_...py:61-70`（judge_state choices）。前端消费逻辑依赖 `judge_state`：`/www/wwwroot/guwu-oj/submissions/realtime.py:46-54` 与 `build_submission_status_payload`（37–73）。

---

### 补充要点（对本类研究通常重要）

- Submission **没有 `updated_at`**，也没有独立的 `judge_time`/`compile_time`；只有 `created_at`、`claimed_at`、`heartbeat_at`、`finished_at` 四个时间戳和 `runtime`（ms）、`memory`（KB）两个度量。若计划新增“排队时长/判题时长”等字段，需新增模型字段 + 新迁移（遵循 `NNNN_*.py` 命名），并注意 `runtime` 已被前端契约占用（`realtime.py:60`）。
- 写回 Submission 的路径有且仅有：`claiming.finalize_claim`（原始 SQL，DB 路径与 DB-less 消费者共用）、`results.fail_unclaimed`（原始 SQL）、`judge.commit_verdict` 的无 claim 分支（ORM `save(update_fields=...)`）。任何绕过这些路径的字段更新都需要自行发布 realtime 通知（因原始 UPDATE 不触发 `post_save`）。

---

**User**

测评机登录凭证

judge-1: ssh root@64.90.3.112 password: LZ46EqVCZAlauXjS（guwu-oj at /root/guwu-oj).

judge-2 ssh oscar@192.168.196.147 password: 123567Liu (登录后sudo su需密码）（guwu-oj at /home/oscar/guwu-oj，但是owner是root).

允许你读取/修改 .env

**Assistant**

Now I have the full picture. Let me check the environment and which judging path is active.

Now the model fields.

Now `finalize_claim` gets `result_written_at`.

Now the stamping in `save_compile_error` and `judge_submission`.

Now the DB-less path — worker-side timings in [judge_core.py](file:///www/wwwroot/guwu-oj/submissions/judge_core.py).

Now the worker's envelope and the web-side consumer.

Now wire `timings` through `write_judge_outcome` and `process_envelope`.

Now generate the migration and verify.

Both temp tests pass. Let me check migration 0014, whose name mentions `judge_started_at`.

Migration 0014 already adds `judge_started_at`, but 0018 adds it again — yet the test DB applied both. Let me investigate the migration history.

Good — 0015 removed `judge_started_at`, so re-adding it in 0018 is legitimate. Now let me check production migration state and apply.

Applying the migration to the production database (additive, nullable columns).

任务本身已完成，下面是结果汇总。

## 改动内容

**1. 模型新增 5 列** — [models.py](file:///www/wwwroot/guwu-oj/submissions/models.py#L71-L95)

在 `finished_at` 后新增 `enqueued_at`、`judge_started_at`、`compile_done_at`、`tests_done_at`、`result_written_at`，全部 `null=True`，仅用于观测、不参与判题门控。各列对应流水线一个边界：

| 阶段 | 差值含义 |
|---|---|
| `created_at → enqueued_at` | 请求到 broker 交付 |
| `enqueued_at → claimed_at` | 中央队列排队等待 |
| `claimed_at → judge_started_at` | worker 领取/准备 |
| `judge_started_at → compile_done_at` | 容器启动 + 编译 |
| `compile_done_at → tests_done_at` | 测试用例执行 |
| `tests_done_at → result_written_at` | 结果回写/信封延迟 |

**2. 落库入口** — [claiming.py](file:///www/wwwroot/guwu-oj/submissions/claiming.py)
- `mark_queued()` 写 `enqueued_at`；`finalize_claim()` 写 `result_written_at`（与 `finished_at` 同值）。
- 新增 `stamp_progress()`：带 `claim_token` 围栏的白名单打点，token 失效则静默返回 `False`（被 reaper 回收的 worker 无法覆盖新 owner 的时间戳）。

**3. DB 判题路径** — [judge.py](file:///www/wwwroot/guwu-oj/submissions/judge.py)
新增 `stamp_phase()`（try/except 包裹，打点失败只 warning，绝不会把可判题变成 System Error），在判题开始、编译完成、用例跑完三处打点；编译失败时 `tests_done_at` 保持 NULL。

**4. DB-less 路径** — [judge_core.py](file:///www/wwwroot/guwu-oj/submissions/judge_core.py) → [tasks.py](file:///www/wwwroot/guwu-oj/submissions/tasks.py) → [results.py](file:///www/wwwroot/guwu-oj/submissions/results.py)
worker 无 DB 凭证，故用绝对 UTC ISO-8601 把 timings 塞进结果信封，由 Web 侧消费端在事务内代写；`_parse_timings()` 对非法/非白名单值只 warning 丢弃，不影响 verdict。

**5. 迁移** — [0018](file:///www/wwwroot/guwu-oj/submissions/migrations/0018_submission_compile_done_at_submission_enqueued_at_and_more.py)：已在生产库 `ojdb` 成功应用。

## 验证

- 临时端到端测试 2 项通过（打点全链路、围栏失效返回 False、非白名单列抛错、信封脏数据容错），验证后已删除该临时文件。
- `manage.py check` 无问题；`makemigrations --check` 无待生成变更。
- 此前 `tests.test_judge_claim` + `tests.test_judge_dbless`（29 项）、`tests.test_judge` + `tests.test_central_queue`（17 项）全部通过。

一个值得注意的点：`judge_started_at` 曾出现在 `0014`、又被 `0015` 删除，本次 `0018` 是重新加回，迁移历史一致，生产库应用无冲突。

## 关于两台测评机

本地改动尚未提交（`main` 分支上有未提交修改），测评机是从 GitHub 克隆的，**目前还没有这份 worker 侧改动**。你给的凭证我还没有使用。想确认下一步范围：

用户选择：worker 侧记录 + 通过 GitHub 同步。先看清楚工作区里哪些改动与本任务相关。

工作区里除了本次 timing 改动，还有一批**之前会话遗留的未提交 WIP**（"直连回源 + 动态 IP 自愈"：`settings.py`/`worker_api.py`/`internal_views.py`/`internal_urls.py`/`judge_firewall.py`/`sync_judge_firewall.py`/systemd 单元/README，以及 `tasks.py` 里的 `_report_worker_ip`）。另外 `scripts/stress_judge.py` 里有明显是调试留下的改动（注释掉 `time.sleep`、`--poll-interval` 默认 0.2）。

`tasks.py` 同时含两块改动，需要决定提交范围后再推送到共享的 `origin/main`。

明白，只推送 timing 功能。先构造 `tasks.py` 的"仅 timing"版本（该文件里还混着 WIP）。

`/tmp/tasks_head.py` 正好是 HEAD + 3 行 timing。现在按范围暂存。

暂存内容正确。提交并推送。

推送成功。现在连测评机（先检查 `sshpass` 与连通性）。

两台机器的部署状态是"文件已就位但 git HEAD 落后"（`claiming.py`、`judge_core.py`、`results.py` 等都是 untracked）。先 fetch 并逐文件比对，确认能否安全只更新这 7 个文件。

judge-1：除 `judge.py` 外，其余文件的差异行数与我这次提交完全吻合。先打印实际 diff 确认，并给 judge-2 用 sudo 完成 fetch。

两台机器的 `judge_core.py` / `tasks.py` 与 origin/main 的差异**恰好**就是本次 timing 改动（judge-1 的 `judge.py` 有额外本地调试代码 `log_to_file`，那是它自己的，我不动）。worker 是 DB-less，只需这两个文件。

先备份再更新，然后重启 worker。

judge-1 完成。现在 judge-2（需 sudo）。

两台 worker 均已更新重启（都是 DB-less）。现在做一次端到端验证：造一条提交，看时间戳列是否被填上。先确认入队入口。

I'll finish the end-to-end verification: confirm the new timing columns actually get populated by the DB-less workers.

Recent submissions predate the change. Let me create a real submission to verify end-to-end.

Now create a live submission and enqueue it.

Judgment completed but worker timings and `result_written_at` are NULL. Let me investigate the writeback path.

`enqueued_at`/`claimed_at` landed but `result_written_at` didn't — that points to the **long-running web consumer** still executing pre-change code. Let me check the running services.

The result consumer has been running since Sep 20 — old code. Let me confirm and restart the web-side services.

Confirmed — the consumer is on Sep 20 code. Restarting it (and the reaper, which shares the same modules) so they pick up the new timing writeback.

Now re-run the end-to-end test with fresh services.

All five phase timings now land. Let me verify both remote workers actually run the updated code (this run used worker `NUC6CAYH`; the earlier one was `m.camluni.cn`).

judge-1 = `m.camluni.cn`, judge-2 = `NUC6CAYH`. Let me verify both workers' files byte-match `origin/main`.

`origin/main` tracking ref here is stale (49c9d21) — the push went via an explicit URL. Let me compare the judges against the actual pushed commit `2330bf3`.

Both judges byte-match the pushed commit `2330bf3`. Let me update the stale tracking ref and clean up my two test submissions.

No points were awarded (idempotent). Removing my two test submissions to leave production clean.

端到端验证通过。

## 关键发现

首次验证失败的原因不是代码问题，而是**Web 侧的 `guwu-oj-judge-result-consumer` 服务仍运行 9/20 的旧代码**（该进程自 `Sun 2026-09-20 23:04` 起未重启）。旧的消费端不认识 `timings`、也不写 `result_written_at`，所以 worker 上报的时间被静默丢弃。重启后立即恢复正常。

## 验证结果（第 2 次，worker = `NUC6CAYH`）

| 阶段列 | 值 | 与上一阶段差值 |
|---|---|---|
| `created_at` | 12:46:53.184 | — |
| `enqueued_at` | 12:46:53.282 | +0.098s 请求→入队 |
| `claimed_at` | 12:46:53.938 | +0.656s 排队等待 |
| `judge_started_at` | 12:46:54.033 | +0.095s 认领→开判 |
| `compile_done_at` | 12:46:55.479 | +1.446s 容器启动+编译 |
| `tests_done_at` | 12:46:55.728 | +0.249s 测试执行 |
| `result_written_at` | 12:46:55.860 | +0.132s 写回落库 |

`finished_at == result_written_at`，符合预期。

## 部署一致性核对

- **judge-1** (`64.90.3.112`, host `m.camluni.cn`) 与 **judge-2** (`192.168.196.147`, host `NUC6CAYH`) 的 `judge_core.py` / `tasks.py` md5 分别为 `65d08100…` / `11b4c963…`，与已推送的 `2330bf3` 逐字节一致，worker 服务均 `active`。
- 顺带修正：本地 `origin/main` 跟踪引用此前停在 `49c9d21`（推送走的是显式 URL，未更新跟踪引用），已 `git fetch` 修正为 `2330bf3`。GitHub 远端本身一直是正确的，部署时测评机自 fetch 拿到的也是 `2330bf3`。

## 收尾

- 两台测试提交（7640/7641）已删除（级联清理 8 行）；确认未产生积分流水（`PointLedgerEntry` 近 30 分钟 0 条，用户早已解出该题，奖励逻辑幂等）。
- 工作区仍保留与本次 timing 无关的直连回源/防火墙 WIP（`tasks.py` 的 `_report_worker_ip`、`worker_api.py`、`internal_views.py`、`judge_firewall.py` 等），按你之前的选择未提交，留待后续单独处理。

timing 功能已完整落地并经真实判题链路验证。

---

