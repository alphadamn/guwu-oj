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
  - 代码提交（支持 C、C++、Python、Java）
  - 提交记录查看
  - 提交详情（代码、评测结果、运行时间、内存使用）

- 排行榜
  - 用户排名（按已解决题目数排序）
  - 统计信息（已解决数、提交数、通过率）

- 管理后台
  - 题目管理
  - 用户管理
  - 提交记录管理

## 技术栈

- Python 3.8+
- Django 4.2+
- Bootstrap 5
- PostgreSQL
- django-crispy-forms
- crispy-bootstrap5

## 安装步骤

### 1. 克隆项目

```bash
cd /Users/oscar.liu/Desktop/guwu-oj
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

### 6. 启动开发服务器

```bash
python manage.py runserver
```

访问 http://127.0.0.1:8000 查看网站。

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

## 使用说明

### 管理后台

访问 http://127.0.0.1:8000/admin 进入管理后台，使用超级用户账号登录。

在管理后台中可以：
- 创建和管理题目
- 管理用户
- 查看提交记录

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
2. 评测目前使用docker image进行环境隔离。
3. SECRET_KEY 需要在生产环境中更改。
4. 建议在生产环境中配置 ALLOWED_HOSTS。

## 开发计划TODO

- [x] 集成自动代码评测系统
- [ ] 添加AI解题功能
- [ ] 添加内置搜索引擎
- [ ] 添加比赛功能
- [ ] 添加讨论区
- [x] 添加题解功能
- [x] 优化移动端体验
- [ ] 添加更多编程语言支持

## 许可证

MIT License

## 联系方式

如有问题或建议，欢迎提 Issue。

# guwu-oj
