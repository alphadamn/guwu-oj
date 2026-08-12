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
- Redis+RQ (缓存 + 任务队列)

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
cd /Users/oscar.liu/Desktop/oj/CascadeProjects/windsurf-project
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

### 8. 启动 Redis 服务

```bash
redis-server
```

### 9. 启动 RQ Worker (用于异步评测)

#### Redis 密码与 TLS 配置

判题队列 Redis 必须同时启用 TLS 和密码认证。不要将真实密码写入版本控制；在 Web 服务器和每台判题机的受限 `.env` 文件中设置相同的 `RQ_REDIS_PASSWORD`。密码至少 12 个字符，并包含字母、数字和特殊字符。

```dotenv
RQ_REDIS_HOST=judge-redis.example.internal
RQ_REDIS_PORT=6379
RQ_REDIS_DB=0
RQ_REDIS_PASSWORD=<generated-secret>
RQ_REDIS_TLS=true
RQ_REDIS_CA_CERT=/etc/redis/tls/ca.crt
```

Redis 服务端必须使用相同密码配置 `requirepass`，并保持 `port 0` 与 TLS 端口配置。每次修改密码时，先更新所有 Web/判题机 `.env` 文件，再重启 Redis，最后重启所有 RQ worker。Django-RQ 从 `RQ_QUEUES` 的 URL 和 `REDIS_CONNECTION_KWARGS` 自动读取密码与 TLS 参数；`rqworker` 命令不应额外传入 Redis URL。

可使用下列命令验证认证与 TLS，其中未提供密码的命令必须返回 `NOAUTH Authentication required`：

```bash
REDISCLI_AUTH="$RQ_REDIS_PASSWORD" redis-cli --tls --cacert "$RQ_REDIS_CA_CERT" \
  -h "$RQ_REDIS_HOST" -p "$RQ_REDIS_PORT" ping
```

**macOS 用户需要设置环境变量:**
```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py rqworker default high low
```

**Linux 用户:**
```bash
python manage.py rqworker default high low
```

#### 多判题机部署 (Multi-Judge)

For production TLS/password and mTLS deployment, follow [Add a TLS + Password Judge Machine](docs/judge-machine-tls.md). It is the authoritative guide for per-machine credential paths, Django admin configuration, worker setup, verification, and credential rotation.

支持将评测任务分发到多个判题机并行处理，提升系统吞吐量。

**1. 配置判题机**

在 `settings.py` 中添加 `JUDGE_MACHINES`：

```python
JUDGE_MACHINES = [
    {
        'name': 'judge-1',
        'host': 'localhost',
        'port': 6379,
        'db': 0,
        'queue': 'judge-1',
        'enabled': True,
        'weight': 1,
    },
    {
        'name': 'judge-2',
        'host': '192.168.1.100',
        'port': 6379,
        'db': 0,
        'queue': 'judge-2',
        'enabled': True,
        'weight': 1,
    },
]
```

**2. 启用多判题模式**

```bash
export OJ_MULTI_JUDGE_ENABLED=true
```

**3. 在各判题机上启动 RQ Worker**

```bash
# 判题机 1
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py rqworker judge-1 --worker-class oj_project.customrq.AutoReconnectWorker

# 判题机 2
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES python manage.py rqworker judge-2 --worker-class oj_project.customrq.AutoReconnectWorker
```

**4. 检查判题机健康状态**

```bash
python manage.py check_judge_health
```

负载均衡策略：
- **加权随机分配**：根据 `weight` 字段按比例分发任务
- **健康检查**：每 30 秒检查一次 Redis 连接，自动跳过不健康的机器
- **降级兜底**：所有机器不可用时自动回退到 `default` 队列
- 关闭多判题模式（`OJ_MULTI_JUDGE_ENABLED=false`）即恢复单机模式

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


## 项目结构

```
windsurf-project/
├── manage.py                 # Django 管理脚本
├── requirements.txt          # 项目依赖
├── README.md                # 项目说明
├── oj_project/              # Django 项目配置
│   ├── __init__.py
│   ├── settings.py          # 项目设置
│   ├── urls.py              # 主 URL 配置
│   └── wsgi.py              # WSGI 配置
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
│   ├── models.py            # 提交模型
│   ├── views.py             # 提交视图
│   ├── urls.py              # 提交 URL
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

项目使用 Django-RQ 实现异步评测，避免评测阻塞 HTTP 请求：

- 评测任务通过 Redis 队列异步执行
- 支持多个优先级队列 (default, high, low)
- 评测结果自动更新到数据库
- 支持任务失败重试和错误日志记录

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
- [x] 添加比赛功能
- [ ] 植入AI解题功能
- [x] 文档搜索引擎
- [x] 添加题解功能
- [ ] 优化移动端体验
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
