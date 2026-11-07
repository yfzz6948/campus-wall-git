# -*- coding: utf-8 -*-
"""
校园信息墙 v5 (Campus Wall)
==========================
Flask + MariaDB(MySQL) + Bootstrap 5

v5 架构变更（多机部署）：
  - 数据库由 SQLite 改为 MariaDB，集中部署在一台数据库服务器上；
    多台 Web 服务器通过环境变量连接同一个库，数据天然一致，配合 DNS 轮询 / nginx 负载均衡使用。
  - 图片不再写本地磁盘，改为存入 posts.image_data（MEDIUMBLOB），通过 /media/post/<id> 读取；
    这样 Web 服务器完全无状态，两台机器看到的图片一致，备份只需 mysqldump。
  - 初始化建库改为手动执行：python app.py initdb（只需在数据库就绪后执行一次）。

历史功能（全部保留）：
  1. 四要素（学号/年级/班级/姓名）强校验登录，已存在学号必须四项完全一致才放行；
     不存在则进入防外校答题注册（随机 3 题，全对才建号，永久占用）。
  2. 朋友圈式时间线，帖子支持 Markdown（bleach 严格白名单清洗，禁 script/iframe/img/a），
     支持匿名发帖与单张图片上传（图片入库，魔数验真）。
  3. 帖子内回复（正序），超管删帖/删回复（学号 25710202061，注册时自动获得管理员身份）。
  4. 管理员题库增删改查 /admin/questions。
  5. 管理员置顶，全站最多 3 条；90 天前的帖子自动清理。
  6. 注册答题防爆破：题目 ID 绑定当次会话，同一学号最多错 5 次，超限锁定 1 小时。
     登录提交前先过一次 Cloudflare Turnstile（不可见模式），校验失败直接拒绝；
     未配置 TURNSTILE_SITE_KEY/SECRET_KEY 或访问 .onion 时自动跳过。
  7. 每日发帖上限：每个账号每个北京时间自然日最多 10 条，管理员不受限制。
  8. 数据库不可用时返回 503 友好降级页，不向用户抛栈。

【环境变量】（两台 Web 必须设置一致；建议写入 systemd 的 EnvironmentFile）
  DB_HOST      数据库地址，数据库本机填 127.0.0.1，其他机器填数据库机内网 IP
  DB_PORT      默认 3306
  DB_USER      数据库账号
  DB_PASSWORD  数据库密码
  DB_NAME      库名，默认 campus_wall
  SECRET_KEY   Flask Session 签名密钥，【两台必须完全相同】，否则登录状态随机掉线

【安全与零日志原则】
  - 数据库不存任何 IP、User-Agent 等设备特征字段，代码中也不读取这些值；
  - 关闭 werkzeug 访问日志（默认会记录访客 IP）；
  - 用户内容中的外链图片/脚本一律由 bleach 剥离，图片只能来自本库 /media 接口；
  - 上传校验：扩展白名单 + MIME 校验 + 文件头魔数验真，防伪造后缀，防视频/附件。
"""
import os
import re
import sys
import json
import html as html_lib
import logging
import urllib.request
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import quote, urlsplit, urlencode

import bleach
import markdown as md_lib
import pymysql
from pymysql.cursors import DictCursor
from markupsafe import escape
from flask import (
    Flask, g, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, Response, make_response,
    has_request_context, has_app_context,
)

# 零日志：werkzeug 默认逐请求打印“IP - - [时间] 请求行”，直接压到 ERROR 级别
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# 配置（数据库连接信息全部来自环境变量，同一份代码在两台机器上跑）
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
ENV_FILE = os.path.join(BASE_DIR, "campus_wall.env")


def _compute_build():
    """版本号 = app.py 与模板里最新的修改时间。

    换文件就换版本，不用手改常量；两台机器各自算自己的，
    谁没更新一眼就能看出来（响应头 X-CW-Build、页脚都会显示）。
    """
    latest = 0.0
    try:
        latest = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        pass
    tpl_dir = os.path.join(BASE_DIR, "templates")
    try:
        for name in os.listdir(tpl_dir):
            if name.endswith(".html"):
                latest = max(latest, os.path.getmtime(os.path.join(tpl_dir, name)))
    except OSError:
        pass
    if not latest:
        return "unknown"
    return datetime.fromtimestamp(latest).strftime("%Y%m%d.%H%M")


APP_BUILD = os.environ.get("APP_BUILD") or _compute_build()


def load_env_file(path=ENV_FILE):
    """把同目录下的 campus_wall.env 读进环境变量，已存在的变量不覆盖。

    这样 `python app.py initdb` 不用先 `set -a && source campus_wall.env`；
    systemd 通过 EnvironmentFile 注入的值优先级仍然更高（不会被文件改写）。
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if not key or key in os.environ:
                    continue
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        return   # 没有这个文件就按"没配"处理，由下游给出明确报错


load_env_file()

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "campus_wall")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "campus_wall")

ADMIN_STUDENT_ID = "25710202061"   # 超级管理员学号（注册时自动 is_admin=1）

POST_MAX_LEN = 2000
REPLY_MAX_LEN = 200
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 单图上限 10MB（MEDIUMBLOB 上限 16MB，需 max_allowed_packet ≥ 12M）
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp"}
IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}

# 帖子自动清理：删除 90 天（约 3 个月）前的帖子；每 24 小时最多真正清理一次
POST_RETENTION_DAYS = 90
SWEEP_INTERVAL = timedelta(days=1)
SWEEP_FLAG = os.path.join(BASE_DIR, ".last_sweep")   # 用文件 mtime 记录上次清理时间

# 注册答题防爆破：同一学号累计答错 5 次即锁定 1 小时（题目 ID 同时绑定当次会话）
MAX_QUIZ_FAILS = 5
QUIZ_LOCK_HOURS = 1
QUIZ_ATTEMPT_KEEP_DAYS = 30   # 失败记录保留天数，随每日清理一起清掉

# 每日发帖上限（按北京时间自然日计算；管理员不受限制）
DAILY_POST_LIMIT = 10

# 登录防冒充：同一学号连续 5 次「学号存在但年级/班级/姓名不符」即锁定 1 小时
MAX_LOGIN_FAILS = 5
LOGIN_LOCK_HOURS = 1

# 登录人机验证：Cloudflare Turnstile（不可见模式，只在登录提交时跑一次）
# 两个环境变量都配了才启用；留空则完全跳过（本机开发不用配）。
# .onion 与 localhost 自动跳过：那里连不到 challenges.cloudflare.com，强校验等于把门焊死。
TURNSTILE_SITE_KEY = (os.environ.get("TURNSTILE_SITE_KEY") or "").strip()
TURNSTILE_SECRET_KEY = (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TIMEOUT = 6   # 秒；校验接口超时就当"没法判"，放行，别把登录卡死

# 隐私政策页展示的联系邮箱：支持多个（逗号/分号/空白分隔），留空则只提示站内联系
# 形如 "a@x.com,b@y.com"；非法项会被丢掉，避免在页面上渲染出一个畸形链接
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def parse_contact_emails(raw):
    """把一个字符串切成合法邮箱列表：去重、忽略非法项、最多 5 个。

    分隔符容忍真人写法：逗号/分号/顿号/斜杠/空白，以及中文的「或」。
    （邮箱里不可能出现这些字符，所以拿来当分隔符是安全的。）
    """
    out = []
    for part in re.split(r"[,;，；、|/\s]+|或", (raw or "").strip()):
        part = part.strip().strip("\"'")
        if not part or not RE_EMAIL.fullmatch(part):
            continue
        if part.lower() not in (x.lower() for x in out):
            out.append(part)
    return out[:5]


PRIVACY_CONTACT_EMAILS = parse_contact_emails(os.environ.get("PRIVACY_CONTACT_EMAIL"))


def parse_contact_phone(raw):
    """把配置里的电话号码规整成可直接用的形式；不像号码就返回 None。

    接受 +86 138 0013 8000 / +8613800138000 / 13800138000 这几种写法；
    没写国际区号时按中国大陆（+86）处理——本站是校内墙，这个默认值最省事。
    """
    s = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if not s:
        return None
    if "," in s or ";" in s:      # 只留一个号码，多了就不渲染，免得页面乱
        return None
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not has_plus:
        if len(digits) == 11:     # 13800138000 这种，补上 86
            digits = "86" + digits
    if not (8 <= len(digits) <= 15):
        return None

    # 常见区号（先长后短，避免把 852 误当成 86 之类的子串）
    cc, rest = "", digits
    for prefix in ("852", "853", "886", "86", "81", "82", "65", "60", "62", "63", "66", "84", "44", "1"):
        if digits.startswith(prefix) and len(digits) - len(prefix) >= 7:
            cc, rest = prefix, digits[len(prefix):]
            break

    if cc == "86" and len(rest) == 11:
        display = f"+86 {rest[:3]} {rest[3:7]} {rest[7:]}"
    elif cc == "852" and len(rest) == 8:
        display = f"+852 {rest[:4]} {rest[4:]}"
    elif cc:
        display = f"+{cc} {rest}"
    else:
        display = f"+{digits}"
    return {
        "display": display,
        "digits": digits,            # tel: 用
        "wa": digits,                # wa.me 只认纯数字
    }


PRIVACY_CONTACT_PHONE = parse_contact_phone(os.environ.get("PRIVACY_CONTACT_PHONE"))

# 回复频率：普通账号每小时最多 30 条（发的时候不提示，超了才拒绝；管理员不受限制）
REPLY_LIMIT_PER_HOUR = 30

# 草稿箱：每人最多 3 条，超出自动删最早的一条
MAX_DRAFTS = 3

# 时间线分页：一页 15 条帖子（回复只加载当前页的，页面体积不随时间线总长度增长）
POSTS_PER_PAGE = 15

app = Flask(__name__)

# ---------- 会话 Cookie 安全 ----------
# SameSite=Lax：跨站 POST（删帖、置顶、发帖这类表单）不会带上 Cookie，等于一层免费的 CSRF 防护；
# HttpOnly 防脚本读取会话；Secure 只在纯 HTTPS 部署时打开——
# 本站还有 Tor（http）入口，全局强制 Secure 会让人在那条路上根本登录不了，
# 所以做成开关，只走 HTTPS 的部署可设 COOKIE_SECURE=1。
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"

DEFAULT_SECRET = "dev-secret-key-please-change-me"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", DEFAULT_SECRET)
app.config["MAX_CONTENT_LENGTH"] = 13 * 1024 * 1024  # 10MB 图 + 文本 + multipart 开销

# 用默认密钥上线，等于把 Session 签名公开了：任何人都能伪造一个 is_admin 的 Cookie。
# 与其静悄悄跑起来，不如直接拒绝服务；本地开发需要时设 ALLOW_INSECURE_SECRET=1。
INSECURE_SECRET = (
    app.config["SECRET_KEY"] in ("", DEFAULT_SECRET)
    and os.environ.get("ALLOW_INSECURE_SECRET", "0") != "1"
)

BJT = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 格式校验正则（前后端一致，后端是最终边界）
# ---------------------------------------------------------------------------
RE_STUDENT_ID = re.compile(r"^\d{11}$")              # 学号：11 位纯数字
RE_GRADE = re.compile(r"^\d{1,2}$")                  # 年级：1~2 位数字，如 24
RE_CLASS = re.compile(r"^[一-龥A-Za-z0-9]{1,10}$")   # 班级：如 会计2 / 网络1
RE_NAME = re.compile(r"^[一-龥]{2,4}$")              # 姓名：2~4 个中文字符

# Markdown 渲染白名单：排版能力全开（标题/列表/引用/代码块/表格/删除线），
# 链接（a）开放，但站外链接一律改写成本站的中转提示页 /link-out；
# img 仍然禁用：外链图片会让每个浏览者的浏览器主动去请求第三方地址，
# 把 IP / Referer 送出去，与本项目「零日志、匿名」的设计正面冲突。
MD_ALLOWED_TAGS = [
    "p", "br", "b", "strong", "i", "em", "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote", "hr", "del", "s",
    "table", "thead", "tbody", "tr", "th", "td", "a",
]
MD_ALLOWED_ATTRIBUTES = {"a": ["href", "title"]}
# 只放行这几种协议；javascript:/data: 之类会被 bleach 直接剥掉
MD_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

# 本站自己的域名：指向这些主机的链接属于站内链接，直接跳转、不套提示页。
# 实际判断时还会叠加当前请求的 Host（包括 Tor 隐藏服务的域名）。
SITE_HOSTS = {
    h.strip().lower().rstrip(".")
    for h in os.environ.get("SITE_HOSTS", "y4006948.com,www.y4006948.com").split(",")
    if h.strip()
}

# ---------------------------------------------------------------------------
# 数据库连接（每请求一个连接，请求结束自动关闭；校园墙量级无需连接池）
# ---------------------------------------------------------------------------
class DatabaseUnavailable(RuntimeError):
    """数据库连不上时抛出，由 errorhandler 渲染 503 友好降级页。"""


class CWConnection(pymysql.connections.Connection):
    """在标准 PyMySQL 连接上加一个 execute() 快捷方法。

    PyMySQL 原生连接不像 sqlite3 那样自带 connection.execute()，
    必须每次显式建 cursor；包一层后，db.execute(sql, args) 直接返回 cursor，
    可链式 fetchone()/fetchall()，插入后用返回游标的 lastrowid。
    """

    def execute(self, sql, args=None):
        cur = self.cursor()
        cur.execute(sql, args)
        return cur


def connect_db(database=True):
    """新建一个 MariaDB 连接。database=False 时不指定库（initdb 建库前用）。"""
    try:
        return CWConnection(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME if database else None,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=5,
        )
    except pymysql.MySQLError as e:
        raise DatabaseUnavailable(
            f"无法连接数据库 {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}：{e}\n"
            f"请检查 DB_HOST/DB_PORT/DB_USER/DB_PASSWORD 环境变量，以及数据库是否已启动。"
        ) from e


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        try:
            db.close()
        except pymysql.MySQLError:
            pass


# 首次初始化内置的示例验证题（任何人都能答对，仅用于开发；上线后管理员必须替换为校内真题）
SEED_QUESTIONS = [
    ("（示例题，请管理员在题库管理中替换为本校题目）我国的首都是？",
     "上海", "北京", "广州", "深圳", "B"),
    ("（示例题）1+1 等于几？", "1", "2", "3", "4", "B"),
    ("（示例题）一周有多少天？", "5", "6", "7", "8", "C"),
    ("（示例题）红绿灯中表示“准许通行”的颜色是？", "红色", "黄色", "绿色", "蓝色", "C"),
    ("（示例题）“你好”的英文是？", "Hello", "Goodbye", "Thanks", "Sorry", "A"),
    ("（示例题）2+2 等于几？", "1", "2", "3", "4", "D"),
    ("（示例题）太阳从哪个方向升起？", "西方", "南方", "北方", "东方", "D"),
]

# 建表语句（幂等；InnoDB 支持外键级联，utf8mb4 支持 emoji 和所有中文）
SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id         INT PRIMARY KEY AUTO_INCREMENT,
        student_id VARCHAR(11) NOT NULL UNIQUE,
        grade      VARCHAR(4)  NOT NULL,
        class_name VARCHAR(20) NOT NULL,
        name       VARCHAR(10) NOT NULL,
        is_admin   TINYINT(1)  NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        id           INT PRIMARY KEY AUTO_INCREMENT,
        user_id      INT NOT NULL,
        content      TEXT,
        image_data   MEDIUMBLOB,
        image_mime   VARCHAR(32),
        is_anonymous TINYINT(1) NOT NULL DEFAULT 0,
        is_pinned    TINYINT(1) NOT NULL DEFAULT 0,
        pinned_at    DATETIME(6),
        timestamp    DATETIME NOT NULL,
        CONSTRAINT fk_posts_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS replies (
        id           INT PRIMARY KEY AUTO_INCREMENT,
        post_id      INT NOT NULL,
        user_id      INT NOT NULL,
        content      TEXT NOT NULL,
        is_anonymous TINYINT(1) NOT NULL DEFAULT 0,
        timestamp    DATETIME NOT NULL,
        CONSTRAINT fk_replies_post FOREIGN KEY (post_id) REFERENCES posts (id) ON DELETE CASCADE,
        CONSTRAINT fk_replies_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS questions (
        id             INT PRIMARY KEY AUTO_INCREMENT,
        question_text  VARCHAR(255) NOT NULL,
        option_a       VARCHAR(255) NOT NULL,
        option_b       VARCHAR(255) NOT NULL,
        option_c       VARCHAR(255) NOT NULL,
        option_d       VARCHAR(255) NOT NULL,
        correct_answer ENUM('A','B','C','D') NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS quiz_attempts (
        student_id   VARCHAR(11) PRIMARY KEY,
        fail_count   INT NOT NULL DEFAULT 0,
        last_attempt DATETIME NOT NULL,
        locked_until DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        student_id   VARCHAR(11) PRIMARY KEY,
        fail_count   INT NOT NULL DEFAULT 0,
        last_attempt DATETIME NULL,
        locked_until DATETIME NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS drafts (
        id           INT PRIMARY KEY AUTO_INCREMENT,
        user_id      INT NOT NULL,
        content      TEXT NOT NULL,
        is_anonymous TINYINT(1) NOT NULL DEFAULT 0,
        created_at   DATETIME NOT NULL,
        CONSTRAINT fk_drafts_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


# 已有库的增量迁移：CREATE TABLE IF NOT EXISTS 不会给旧表补列，
# 新增的列必须单独执行一次。MariaDB 支持 ADD COLUMN IF NOT EXISTS；
# 不支持该写法的库会报 1060（列已存在），直接视为迁移已完成。
SCHEMA_MIGRATIONS = [
    "ALTER TABLE replies ADD COLUMN IF NOT EXISTS "
    "is_anonymous TINYINT(1) NOT NULL DEFAULT 0",
    # 时间线清理按 timestamp 过滤、每日配额按 (user_id, timestamp) 过滤，
    # 没索引时每次都是全表扫描；数据量涨起来前先补上
    "CREATE INDEX idx_posts_time ON posts (timestamp)",
    "CREATE INDEX idx_posts_user_time ON posts (user_id, timestamp)",
    # 每小时回复上限要按 (user_id, timestamp) 统计，同样需要索引
    "CREATE INDEX idx_replies_user_time ON replies (user_id, timestamp)",
]


def apply_migration(cur, stmt):
    """执行一条迁移语句：已经做过就跳过（列/索引已存在），语法不支持 IF NOT EXISTS 时退回普通写法。"""
    try:
        cur.execute(stmt)
        return
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else None
        if code in (1060, 1061):   # 1060 列已存在 / 1061 索引名已存在
            return
        if code != 1064:           # 不是语法错误，那就是真的出问题
            raise
    # 后端不支持 IF NOT EXISTS（例如 MySQL 的 ALTER）：退回普通语句，重复执行同样忽略
    try:
        cur.execute(stmt.replace("IF NOT EXISTS ", "", 1))
    except pymysql.MySQLError as exc:
        if not (exc.args and exc.args[0] in (1060, 1061)):
            raise


def init_db():
    """幂等建表 + 首次写入示例题库（python app.py initdb，部署时只执行一次）。"""
    db = connect_db()
    try:
        with db.cursor() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
            for stmt in SCHEMA_MIGRATIONS:
                apply_migration(cur, stmt)
            cur.execute("SELECT COUNT(*) AS n FROM questions")
            if cur.fetchone()["n"] == 0:
                cur.executemany(
                    "INSERT INTO questions (question_text, option_a, option_b, option_c, "
                    "option_d, correct_answer) VALUES (%s, %s, %s, %s, %s, %s)",
                    SEED_QUESTIONS,
                )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def now_utc_sql():
    """UTC 当前时间，DATETIME 标准格式（字符串字典序与时间序一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now_utc_sql_us():
    """UTC 当前时间，微秒精度，用于置顶排序。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def bjt_day_start_utc():
    """北京时间当天 00:00 对应的 UTC 时间字符串（每日发帖计数的时间起点）。"""
    start = datetime.now(BJT).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _as_utc_datetime(value):
    """把库里读出的 DATETIME（naive）或字符串统一成 UTC aware 时间。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("T", " "))
    return value.replace(tzinfo=timezone.utc)


def is_admin_user(db, user_id):
    """实时查库判断管理员（不依赖 Session 里的旧身份）。"""
    row = db.execute("SELECT is_admin FROM users WHERE id = %s", (user_id,)).fetchone()
    return bool(row and row["is_admin"])


def today_post_count(db, user_id):
    """该账号在【北京时间今天】已发布的帖子数。"""
    return db.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE user_id = %s AND timestamp >= %s",
        (user_id, bjt_day_start_utc()),
    ).fetchone()["n"]


def post_quota_exceeded(db, user_id):
    """返回 (是否已达每日上限, 今日已发条数)。管理员永远不超限。"""
    if is_admin_user(db, user_id):
        return False, today_post_count(db, user_id)
    count = today_post_count(db, user_id)
    return count >= DAILY_POST_LIMIT, count


def trim_drafts(db, user_id):
    """草稿超过 MAX_DRAFTS 条时，删掉最早的那些。"""
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM drafts WHERE user_id = %s ORDER BY id DESC", (user_id,)
    ).fetchall()]
    for extra_id in ids[MAX_DRAFTS:]:
        db.execute("DELETE FROM drafts WHERE id = %s", (extra_id,))
    return min(len(ids), MAX_DRAFTS)


def quiz_lock_remaining(student_id):
    """返回该学号剩余锁定秒数（0 表示未锁定）。表缺失时按未锁定处理。"""
    try:
        row = get_db().execute(
            "SELECT locked_until FROM quiz_attempts WHERE student_id = %s",
            (student_id,),
        ).fetchone()
    except pymysql.MySQLError:
        return 0
    locked_until = _as_utc_datetime(row["locked_until"]) if row else None
    if locked_until is None:
        return 0
    return max(0, int((locked_until - datetime.now(timezone.utc)).total_seconds()))


def record_quiz_failure(student_id):
    """记录一次答题失败，返回 (是否已锁定, 剩余机会数)。

    主存储是 quiz_attempts 表（换浏览器、清 Cookie 也绕不过）；
    表还没建（忘记重跑 initdb）时退化为会话内计数，不影响正常注册。
    """
    db = get_db()
    now_sql = now_utc_sql()
    try:
        row = db.execute(
            "SELECT fail_count FROM quiz_attempts WHERE student_id = %s",
            (student_id,),
        ).fetchone()
        count = (row["fail_count"] if row else 0) + 1
        if count >= MAX_QUIZ_FAILS:
            locked_until = (
                datetime.now(timezone.utc) + timedelta(hours=QUIZ_LOCK_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if row:
                db.execute(
                    "UPDATE quiz_attempts SET fail_count = 0, last_attempt = %s, "
                    "locked_until = %s WHERE student_id = %s",
                    (now_sql, locked_until, student_id),
                )
            else:
                db.execute(
                    "INSERT INTO quiz_attempts (student_id, fail_count, last_attempt, "
                    "locked_until) VALUES (%s, 0, %s, %s)",
                    (student_id, now_sql, locked_until),
                )
        elif row:
            db.execute(
                "UPDATE quiz_attempts SET fail_count = %s, last_attempt = %s "
                "WHERE student_id = %s",
                (count, now_sql, student_id),
            )
        else:
            db.execute(
                "INSERT INTO quiz_attempts (student_id, fail_count, last_attempt) "
                "VALUES (%s, %s, %s)",
                (student_id, count, now_sql),
            )
        db.commit()
        return count >= MAX_QUIZ_FAILS, max(0, MAX_QUIZ_FAILS - count)
    except pymysql.MySQLError:
        try:
            db.rollback()
        except pymysql.MySQLError:
            pass
        count = int(session.get("quiz_fails", 0)) + 1
        session["quiz_fails"] = 0 if count >= MAX_QUIZ_FAILS else count
        return count >= MAX_QUIZ_FAILS, max(0, MAX_QUIZ_FAILS - count)


def clear_quiz_failures(student_id):
    """注册成功后清掉该学号的失败记录。"""
    session.pop("quiz_fails", None)
    try:
        db = get_db()
        db.execute("DELETE FROM quiz_attempts WHERE student_id = %s", (student_id,))
        db.commit()
    except pymysql.MySQLError:
        pass


# ---------------------------------------------------------------------------
# 登录防冒充：学号存在但年级/班级/姓名对不上，连续 5 次锁定 1 小时
# 只针对「撞已有学号」这一类失败；空学号猜人（库里没有该学号）走的是注册流程，不计入
# ---------------------------------------------------------------------------
def login_lock_remaining(student_id):
    """返回该学号剩余锁定秒数（0 表示未锁定）。表缺失时按未锁定处理。"""
    try:
        row = get_db().execute(
            "SELECT locked_until FROM login_attempts WHERE student_id = %s",
            (student_id,),
        ).fetchone()
    except pymysql.MySQLError:
        return 0
    locked_until = _as_utc_datetime(row["locked_until"]) if row else None
    if locked_until is None:
        return 0
    return max(0, int((locked_until - datetime.now(timezone.utc)).total_seconds()))


def record_login_failure(student_id):
    """记录一次「信息不符」的登录失败，返回 (是否已锁定, 剩余机会数)。"""
    db = get_db()
    now_sql = now_utc_sql()
    try:
        row = db.execute(
            "SELECT fail_count FROM login_attempts WHERE student_id = %s",
            (student_id,),
        ).fetchone()
        count = (row["fail_count"] if row else 0) + 1
        if count >= MAX_LOGIN_FAILS:
            locked_until = (
                datetime.now(timezone.utc) + timedelta(hours=LOGIN_LOCK_HOURS)
            ).strftime("%Y-%m-%d %H:%M:%S")
            if row:
                db.execute(
                    "UPDATE login_attempts SET fail_count = 0, last_attempt = %s, "
                    "locked_until = %s WHERE student_id = %s",
                    (now_sql, locked_until, student_id),
                )
            else:
                db.execute(
                    "INSERT INTO login_attempts (student_id, fail_count, last_attempt, "
                    "locked_until) VALUES (%s, 0, %s, %s)",
                    (student_id, now_sql, locked_until),
                )
        elif row:
            db.execute(
                "UPDATE login_attempts SET fail_count = %s, last_attempt = %s "
                "WHERE student_id = %s",
                (count, now_sql, student_id),
            )
        else:
            db.execute(
                "INSERT INTO login_attempts (student_id, fail_count, last_attempt) "
                "VALUES (%s, %s, %s)",
                (student_id, count, now_sql),
            )
        db.commit()
        return count >= MAX_LOGIN_FAILS, max(0, MAX_LOGIN_FAILS - count)
    except pymysql.MySQLError:
        try:
            db.rollback()
        except pymysql.MySQLError:
            pass
        # 表还没建（忘记重跑 initdb）时退化为会话内计数，至少同一浏览器能挡住
        count = int(session.get("login_fails", 0)) + 1
        session["login_fails"] = 0 if count >= MAX_LOGIN_FAILS else count
        return count >= MAX_LOGIN_FAILS, max(0, MAX_LOGIN_FAILS - count)


def clear_login_failures(student_id):
    """登录成功后清掉该学号的失败记录。"""
    session.pop("login_fails", None)
    try:
        db = get_db()
        db.execute("DELETE FROM login_attempts WHERE student_id = %s", (student_id,))
        db.commit()
    except pymysql.MySQLError:
        pass


def reply_hour_count(db, user_id):
    """该用户最近 1 小时内的回复条数。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)
             ).strftime("%Y-%m-%d %H:%M:%S")
    return db.execute(
        "SELECT COUNT(*) AS n FROM replies WHERE user_id = %s AND timestamp >= %s",
        (user_id, since),
    ).fetchone()["n"]


@app.context_processor
def inject_build():
    """所有模板都能拿到 build，页面底部会显示，用来分辨「是不是旧缓存页面」。"""
    return {
        "build": APP_BUILD,
        "turnstile_site_key": TURNSTILE_SITE_KEY,
        "turnstile_on": turnstile_enabled(),
        "privacy_contact_emails": PRIVACY_CONTACT_EMAILS,
        "privacy_contact_phone": PRIVACY_CONTACT_PHONE,
    }


def turnstile_enabled():
    """登录页要不要插 Turnstile。三个条件：配了 key、不是 .onion、不是本机调试。"""
    if not (TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY):
        return False
    host = (request.host or "").split(":")[0].strip().lower()
    return not (host.endswith(".onion") or host in ("localhost", "127.0.0.1", "::1"))


def turnstile_verdict(token):
    """True=通过，False=没通过，None=校验接口够不着（网络问题，不是人机验证失败）。

    故意不传 remoteip：本站不保存访客 IP，也就不把它转手交给第三方。
    """
    if not token:
        return False
    payload = urlencode({
        "secret": TURNSTILE_SECRET_KEY,
        "response": token,
    }).encode("utf-8")
    req = urllib.request.Request(TURNSTILE_VERIFY_URL, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TURNSTILE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    return bool(data.get("success"))


def friendly_time(value):
    """UTC 时间 -> 北京时间友好文本（接受 DATETIME 对象或字符串）。"""
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.strip().replace("T", " ")).replace(tzinfo=timezone.utc)
    else:
        dt = value.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(BJT)
    now = datetime.now(BJT)
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60}分钟前"
    if dt.date() == now.date():
        return f"今天 {dt:%H:%M}"
    if dt.date() == (now - timedelta(days=1)).date():
        return f"昨天 {dt:%H:%M}"
    if secs < 7 * 24 * 3600:
        return f"{secs // (24 * 3600)}天前"
    if dt.year == now.year:
        return f"{dt:%m-%d %H:%M}"
    return f"{dt:%Y-%m-%d %H:%M}"


def _is_internal_url(url):
    """判断链接是否站内：相对路径、本站域名、mailto/tel 都算站内（不需要提示页）。"""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if not scheme:                     # /path、#anchor、相对路径
        return True
    if scheme in ("mailto", "tel"):
        return True
    if scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return True
    current = ""
    if has_request_context():
        current = (request.host or "").split(":")[0].lower().rstrip(".")
    return bool(current and host == current) or host in SITE_HOSTS


_ANCHOR_OPEN_RE = re.compile(r"<a([^>]*)>", re.IGNORECASE)
_HREF_ATTR_RE = re.compile(r'href="([^"]*)"', re.IGNORECASE)


def _rewrite_links(html_text):
    """把帖子里的站外链接改写成 /link-out?u=...，点开先看到"即将离开本站"提示页。

    bleach 已经把 href 清洗成只剩 http/https/mailto 与相对地址，
    这里只做改写，不再重新解析 URL 的合法性。
    """
    def repl(match):
        attrs = match.group(1)
        href_attr = _HREF_ATTR_RE.search(attrs)
        if not href_attr:
            return match.group(0)
        target = html_lib.unescape(href_attr.group(1)).strip()
        if not target:
            return "<a%s>" % _HREF_ATTR_RE.sub("", attrs)
        if _is_internal_url(target):
            return match.group(0)
        base = url_for("link_out") if has_app_context() else "/link-out"
        hint = base + "?u=" + quote(target, safe="")
        new_attr = 'href="%s"' % escape(hint)
        new_attrs = attrs[:href_attr.start()] + new_attr + attrs[href_attr.end():]
        return "<a%s>" % new_attrs

    return _ANCHOR_OPEN_RE.sub(repl, html_text)


def render_markdown(text):
    """Markdown -> HTML -> bleach 严格清洗（安全边界，务必先转后洗）-> 站外链接改走提示页"""
    if not text:
        return ""
    raw_html = md_lib.markdown(text, extensions=["nl2br", "fenced_code", "tables"])
    cleaned = bleach.clean(
        raw_html,
        tags=MD_ALLOWED_TAGS,
        attributes=MD_ALLOWED_ATTRIBUTES,
        protocols=MD_ALLOWED_PROTOCOLS,
        strip=True,
    )
    return _rewrite_links(cleaned)


app.jinja_env.filters["ftime"] = friendly_time
app.jinja_env.filters["md"] = render_markdown


def wants_json():
    """判断这次请求是不是 AJAX（要 JSON 而不是重定向）。"""
    if request.headers.get("X-Requested-With") == "fetch":
        return True
    # 兜底：某些反代/中间层会吃掉自定义请求头，Accept 是标准头更不容易丢。
    # 普通浏览器表单提交带的是 text/html，不会误判成 AJAX。
    return "application/json" in (request.headers.get("Accept") or "")


def fail(message, code=400, endpoint="index"):
    if wants_json():
        return jsonify(ok=False, error=message), code
    flash(message, "danger")
    return redirect(url_for(endpoint))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if wants_json():
                return jsonify(ok=False, error="登录已过期，请重新登录"), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if wants_json():
                return jsonify(ok=False, error="登录已过期，请重新登录"), 401
            return redirect(url_for("login"))
        # 实时查库校验管理员身份：被超管取消后立即生效，不依赖旧 Session 残留
        row = get_db().execute(
            "SELECT is_admin FROM users WHERE id = %s", (session["user_id"],)
        ).fetchone()
        if not row or not row["is_admin"]:
            if wants_json():
                return jsonify(ok=False, error="无管理员权限"), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def start_session(user):
    """登录成功后写入 Session（user 为 dict 行）"""
    session.clear()
    session["user_id"] = user["id"]
    session["student_id"] = user["student_id"]
    session["grade"] = user["grade"]
    session["class_name"] = user["class_name"]
    session["name"] = user["name"]
    session["is_admin"] = bool(user["is_admin"])


def full_name(grade, class_name, name):
    """展示名拼接：年级 + 班级 + 姓名，如 24会计2 黄宝莹"""
    return f"{grade}{class_name} {name}"


def sniff_image_ext(header):
    """通过文件头魔数判断真实图片类型，返回标准扩展名；不是图片返回 None"""
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return None


def read_uploaded_image(f):
    """校验上传图片，返回 (True, (mime, data)) 或 (False, 错误原因)。图片直接入库。"""
    filename = f.filename or ""
    client_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if client_ext not in ALLOWED_IMAGE_EXT:
        return False, "仅支持 JPG / PNG / GIF / WEBP 格式的图片"
    if not (f.mimetype or "").lower().startswith("image/"):
        return False, "只能上传图片文件，禁止视频或其他附件"

    header = f.stream.read(12)
    f.stream.seek(0)
    real_ext = sniff_image_ext(header)
    if real_ext is None:
        return False, "文件内容不是有效图片（请勿伪造文件后缀）"

    data = f.read()
    if len(data) > MAX_IMAGE_SIZE:
        return False, f"图片大小不能超过 {MAX_IMAGE_SIZE // (1024 * 1024)}MB"

    return True, (IMAGE_MIME[real_ext], data)


# ---------------------------------------------------------------------------
# 登录 / 答题注册 / 退出
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        student_id = (request.form.get("student_id") or "").strip()
        grade = (request.form.get("grade") or "").strip()
        class_name = (request.form.get("class_name") or "").strip()
        name = (request.form.get("name") or "").strip()

        # 人机验证放在最前面：没过这一关，后面连查库都不做
        if turnstile_enabled():
            verdict = turnstile_verdict(request.form.get("cf-turnstile-response") or "")
            if verdict is False:
                flash("人机验证未通过，请重试（若反复失败请刷新页面）", "danger")
                return render_template("login.html"), 400
            # verdict 为 None 表示 CF 校验接口暂时够不着：
            # 放行，交给下面的身份校验与连续失败锁定兜底，别让一次网络抖动把所有人挡在门外

        # 后端硬性正则校验
        if not RE_STUDENT_ID.fullmatch(student_id):
            flash("学号必须是 11 位纯数字", "danger")
            return render_template("login.html"), 400

        # 该学号是否因反复冒充被锁定（先查锁，后面的校验都不必再做）
        remain = login_lock_remaining(student_id)
        if remain > 0:
            flash(f"该学号因多次身份信息不符已被临时锁定，"
                  f"请 {max(1, (remain + 59) // 60)} 分钟后再试", "danger")
            return render_template("login.html"), 429

        if not RE_GRADE.fullmatch(grade):
            flash("年级必须是 1~2 位数字，例如 24", "danger")
            return render_template("login.html"), 400
        if not RE_CLASS.fullmatch(class_name):
            flash("班级格式不正确，例如 会计2、网络1（1~10 位中文/数字/字母）", "danger")
            return render_template("login.html"), 400
        if not RE_NAME.fullmatch(name):
            flash("姓名必须是 2~4 个中文字符", "danger")
            return render_template("login.html"), 400

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE student_id = %s", (student_id,)
        ).fetchone()

        if user is not None:
            # 学号已存在：年级/班级/姓名必须完全一致，否则一律拒绝（不透露是哪一项不符）
            if (user["grade"], user["class_name"], user["name"]) != (grade, class_name, name):
                locked, left = record_login_failure(student_id)
                if locked:
                    flash(f"连续 {MAX_LOGIN_FAILS} 次身份信息不符，该学号已锁定 "
                          f"{LOGIN_LOCK_HOURS} 小时，请稍后再试", "danger")
                    return render_template("login.html"), 429
                flash(f"登录失败，请检查身份信息是否匹配（还剩 {left} 次机会）", "danger")
                return render_template("login.html"), 401
            clear_login_failures(student_id)
            start_session(user)
            return redirect(url_for("index"))

        # 新学号：暂存待注册信息，先进「隐私政策及社区准则」确认页，同意后才到答题页
        session["pending"] = {
            "student_id": student_id, "grade": grade,
            "class_name": class_name, "name": name,
        }
        session.pop("agree_ok", None)     # 每次新开一次注册都要重新确认
        session.pop("quiz_ids", None)
        session.pop("quiz_fails", None)
        return redirect(url_for("register_agree"))

    return render_template("login.html")


@app.route("/register/agree", methods=["GET", "POST"])
def register_agree():
    """注册前的「隐私政策及社区准则」确认页。

    只对新注册的用户生效（已注册用户登录时不会经过这里）。
    前端把条款放在一个可滚动框里、滚到底才允许勾选；后端只认 agree=1，
    没勾就退回来重新看，防止有人直接 POST 绕过。
    """
    if not session.get("pending"):
        return redirect(url_for("login"))

    if request.method == "POST":
        if request.form.get("agree") != "1":
            flash("请先在条款末尾勾选「我已阅读并同意」，再进入答题", "danger")
            return redirect(url_for("register_agree"))
        session["agree_ok"] = True
        return redirect(url_for("register_quiz"))

    return render_template("agree.html")


@app.route("/register/quiz", methods=["GET", "POST"])
def register_quiz():
    pending = session.get("pending")
    if not pending:
        return redirect(url_for("login"))

    # 没确认过条款就不给进：这条守卫和服务端 POST 校验一起兜住直连的情况
    if not session.get("agree_ok"):
        return redirect(url_for("register_agree"))

    db = get_db()

    # 该学号已被锁定（连续答错达到上限）：清掉待注册状态，退回登录页
    remain = quiz_lock_remaining(pending["student_id"])
    if remain > 0:
        session.pop("pending", None)
        session.pop("quiz_ids", None)
        flash(f"该学号验证失败次数过多，请 {max(1, (remain + 59) // 60)} 分钟后再试", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        # 收集 q-<id> 答案，必须正好 3 题且取值合法
        answers = {}
        for key, value in request.form.items():
            if key.startswith("q-"):
                qid = key[2:]
                if qid.isdigit() and value in ("A", "B", "C", "D"):
                    answers[int(qid)] = value

        if len(answers) != 3:
            flash("验证失败：请完整作答 3 道题", "danger")
            return redirect(url_for("register_quiz"))

        # 提交的题号必须与本次会话发下去的那 3 道完全一致：
        # 防止固定题号反复提交、把每轮答案慢慢套出来的爆破方式
        if set(answers) != set(session.get("quiz_ids") or []):
            session.pop("quiz_ids", None)
            flash("题目已刷新，请重新作答", "warning")
            return redirect(url_for("register_quiz"))

        placeholders = ",".join(["%s"] * len(answers))
        rows = db.execute(
            f"SELECT id, correct_answer FROM questions WHERE id IN ({placeholders})",
            list(answers.keys()),
        ).fetchall()

        all_correct = (
            len(rows) == 3
            and all(answers[r["id"]] == r["correct_answer"] for r in rows)
        )
        if not all_correct:
            locked, left = record_quiz_failure(pending["student_id"])
            if locked:
                session.pop("pending", None)
                session.pop("quiz_ids", None)
                flash(f"连续答错 {MAX_QUIZ_FAILS} 次，该学号已锁定 {QUIZ_LOCK_HOURS} 小时，"
                      "请稍后再试", "danger")
                return redirect(url_for("login"))
            flash(f"验证失败：有题目答错，还剩 {left} 次机会", "danger")
            return redirect(url_for("register_quiz"))

        # 全部正确：正式建号（UNIQUE 约束兜底并发重复注册）
        is_admin = 1 if pending["student_id"] == ADMIN_STUDENT_ID else 0
        try:
            cur = db.execute(
                "INSERT INTO users (student_id, grade, class_name, name, is_admin) "
                "VALUES (%s, %s, %s, %s, %s)",
                (pending["student_id"], pending["grade"], pending["class_name"],
                 pending["name"], is_admin),
            )
            db.commit()
        except pymysql.IntegrityError:
            session.pop("pending", None)
            flash("该学号已被注册，请直接登录", "danger")
            return redirect(url_for("login"))

        clear_quiz_failures(pending["student_id"])
        user = db.execute(
            "SELECT * FROM users WHERE id = %s", (cur.lastrowid,)
        ).fetchone()
        session.pop("pending", None)
        start_session(user)
        return redirect(url_for("index"))

    # GET：题库不足 3 道时无法注册
    total = db.execute("SELECT COUNT(*) AS n FROM questions").fetchone()["n"]
    if total < 3:
        session.pop("pending", None)
        flash("验证题库不足 3 道，请联系管理员补充题目后再注册", "danger")
        return redirect(url_for("login"))

    questions = db.execute(
        "SELECT * FROM questions ORDER BY RAND() LIMIT 3"
    ).fetchall()
    # 记录本次发下去的题号，提交时逐一对齐（防套题/防重放）
    session["quiz_ids"] = [q["id"] for q in questions]
    return render_template("quiz.html", questions=questions, pending=pending)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 图片读取（图片存数据库，Web 服务器无状态；登录后凭 cookie 可见）
# ---------------------------------------------------------------------------
@app.route("/media/post/<int:post_id>")
@login_required
def post_image(post_id):
    row = get_db().execute(
        "SELECT image_data, image_mime FROM posts WHERE id = %s", (post_id,)
    ).fetchone()
    if not row or row["image_data"] is None:
        abort(404)
    resp = Response(row["image_data"], mimetype=row["image_mime"] or "image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------------------------------------------
# 时间线
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    db = get_db()

    total = db.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"]
    pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    # 页码做钳位：?page=0 / 负数 / 超出末页 都落到合法范围内，不报 404
    page = request.args.get("page", type=int) or 1
    page = min(max(page, 1), pages)
    offset = (page - 1) * POSTS_PER_PAGE

    posts = db.execute(
        """
        SELECT p.id, p.content, (p.image_data IS NOT NULL) AS has_image,
               p.is_anonymous, p.is_pinned, p.pinned_at, p.timestamp,
               u.grade, u.class_name, u.name
          FROM posts AS p JOIN users AS u ON u.id = p.user_id
         ORDER BY p.is_pinned DESC, p.pinned_at DESC, p.id DESC
         LIMIT %s OFFSET %s
        """,
        (POSTS_PER_PAGE, offset),
    ).fetchall()

    replies_by_post = {}
    if posts:
        post_ids = [p["id"] for p in posts]
        placeholders = ",".join(["%s"] * len(post_ids))
        replies = db.execute(
            f"""
            SELECT r.id, r.post_id, r.content, r.is_anonymous, r.timestamp,
                   u.grade, u.class_name, u.name
              FROM replies AS r JOIN users AS u ON u.id = r.user_id
             WHERE r.post_id IN ({placeholders})
             ORDER BY r.id ASC
            """,
            post_ids,
        ).fetchall()
        for r in replies:
            replies_by_post.setdefault(r["post_id"], []).append(r)

    return render_template(
        "index.html",
        posts=posts,
        replies_by_post=replies_by_post,
        # 按库里的真实权限决定显不显示管理按钮：
        # 只信 Session 会出现「按钮在但一按就 403」的鬼打墙（权限被取消后 Session 还是旧值）
        is_admin=is_admin_user(db, session["user_id"]),
        page=page,
        pages=pages,
        total=total,
    )


@app.route("/privacy")
def privacy():
    """隐私政策页：公开可读，不需要登录。

    访客在填学号姓名之前就该能读到它；同时 Cloudflare 的 Turnstile 隐形模式
    明确要求站点隐私政策里引用其 Turnstile 隐私补充条款（本页已引用）。
    联系邮箱是可选的：没配 PRIVACY_CONTACT_EMAIL 就只提示站内联系方式。
    """
    return render_template("privacy.html")


@app.route("/link-out")
@login_required
def link_out():
    """站外链接中转提示页：先说明"即将离开本站"，用户确认后才真正跳出去。

    只接受 http/https 绝对地址，避免这个页面被当成任意跳转器使用；
    有登录要求，外人也无法拿本站域名当"看起来可信"的钓鱼中转。
    """
    target = (request.args.get("u") or "").strip()
    parsed = urlsplit(target)
    if (parsed.scheme.lower() not in ("http", "https")
            or not parsed.netloc or len(target) > 2048):
        flash("链接不合法，已取消跳转", "danger")
        return redirect(url_for("index"))

    resp = make_response(render_template(
        "linkout.html",
        target=target,
        site=parsed.hostname or parsed.netloc,
    ))
    # 提示页不该被搜索引擎索引，也不该把本站地址带给目标站
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/posts", methods=["POST"])
@login_required
def create_post():
    content = (request.form.get("content") or "").strip()
    image = request.files.get("image")
    has_image = bool(image and image.filename)

    if not content and not has_image:
        return fail("文字和图片至少要有一项")
    if len(content) > POST_MAX_LEN:
        return fail(f"正文不能超过 {POST_MAX_LEN} 字")

    db = get_db()

    # 每日发帖上限：管理员不受限（实时查库取身份，不依赖 Session 里的旧值）
    exceeded, today_count = post_quota_exceeded(db, session["user_id"])
    if exceeded:
        return fail(
            f"今天已发 {today_count} 条，达到每日上限 {DAILY_POST_LIMIT} 条，明天再来吧"
        )

    image_mime, image_data = None, None
    if has_image:
        ok, result = read_uploaded_image(image)
        if not ok:
            return fail(result)
        image_mime, image_data = result

    anonymous = 1 if request.form.get("is_anonymous") in ("1", "on", "true", "yes") else 0
    ts = now_utc_sql()
    cur = db.execute(
        "INSERT INTO posts (user_id, content, image_data, image_mime, "
        "is_anonymous, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
        (session["user_id"], content, image_data, image_mime, anonymous, ts),
    )
    db.commit()
    post_id = cur.lastrowid

    if wants_json():
        shown_name = "***" if anonymous else session["name"]
        return jsonify(ok=True, post={
            "id": post_id,
            "content_html": render_markdown(content),
            "image_url": url_for("post_image", post_id=post_id) if has_image else "",
            "display_name": f'{session["grade"]}{session["class_name"]} {shown_name}',
            "initial": "匿" if anonymous else session["name"][0],
            "time": friendly_time(ts),
        })
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 草稿箱：每人最多 3 条（超出自动删最早），只存文字，图片不随草稿保存
# ---------------------------------------------------------------------------
@app.route("/drafts")
@login_required
def drafts_list():
    rows = get_db().execute(
        "SELECT * FROM drafts WHERE user_id = %s ORDER BY id DESC",
        (session["user_id"],),
    ).fetchall()
    return render_template("drafts.html", drafts=rows, max_drafts=MAX_DRAFTS)


def _draft_of_current_user(draft_id):
    """取当前用户自己的草稿；别人的草稿一律 404。"""
    row = get_db().execute(
        "SELECT * FROM drafts WHERE id = %s AND user_id = %s",
        (draft_id, session["user_id"]),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@app.route("/drafts/save", methods=["POST"])
@login_required
def draft_save():
    content = (request.form.get("content") or "").strip()
    if not content:
        return fail("草稿内容不能为空", 400, "drafts_list")
    if len(content) > POST_MAX_LEN:
        return fail(f"草稿不能超过 {POST_MAX_LEN} 字", 400, "drafts_list")

    anonymous = 1 if request.form.get("is_anonymous") in ("1", "on", "true", "yes") else 0
    db = get_db()
    db.execute(
        "INSERT INTO drafts (user_id, content, is_anonymous, created_at) "
        "VALUES (%s, %s, %s, %s)",
        (session["user_id"], content, anonymous, now_utc_sql()),
    )
    db.commit()
    count = trim_drafts(db, session["user_id"])
    db.commit()

    if wants_json():
        return jsonify(ok=True, count=count)
    flash(f"已存入草稿箱（当前 {count} 条）", "success")
    return redirect(url_for("drafts_list"))


@app.route("/drafts/<int:draft_id>/update", methods=["POST"])
@login_required
def draft_update(draft_id):
    _draft_of_current_user(draft_id)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("草稿内容不能为空，不想保留的话请直接删除", "danger")
        return redirect(url_for("drafts_list"))
    if len(content) > POST_MAX_LEN:
        flash(f"草稿不能超过 {POST_MAX_LEN} 字", "danger")
        return redirect(url_for("drafts_list"))

    anonymous = 1 if "1" in request.form.getlist("is_anonymous") else 0
    db = get_db()
    db.execute(
        "UPDATE drafts SET content = %s, is_anonymous = %s WHERE id = %s",
        (content, anonymous, draft_id),
    )
    db.commit()
    flash("草稿已保存", "success")
    return redirect(url_for("drafts_list"))


@app.route("/drafts/<int:draft_id>/publish", methods=["POST"])
@login_required
def draft_publish(draft_id):
    draft = _draft_of_current_user(draft_id)

    # 发布时以表单里当前 textarea 的内容/匿名勾选为准（用户可能刚改了还没保存）
    content = (request.form.get("content") or draft["content"] or "").strip()
    anonymous = 1 if "1" in request.form.getlist("is_anonymous") else 0

    if not content:
        flash("草稿是空的，没法发布", "danger")
        return redirect(url_for("drafts_list"))
    if len(content) > POST_MAX_LEN:
        flash(f"正文不能超过 {POST_MAX_LEN} 字", "danger")
        return redirect(url_for("drafts_list"))

    db = get_db()
    exceeded, today_count = post_quota_exceeded(db, session["user_id"])
    if exceeded:
        flash(f"今天已发 {today_count} 条，达到每日上限 {DAILY_POST_LIMIT} 条，草稿先留在箱子里",
              "danger")
        return redirect(url_for("drafts_list"))

    db.execute(
        "INSERT INTO posts (user_id, content, image_data, image_mime, "
        "is_anonymous, timestamp) VALUES (%s, %s, %s, %s, %s, %s)",
        (session["user_id"], content, None, None, anonymous, now_utc_sql()),
    )
    db.execute("DELETE FROM drafts WHERE id = %s", (draft_id,))
    db.commit()
    flash("草稿已发布", "success")
    return redirect(url_for("index"))


@app.route("/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def draft_delete(draft_id):
    _draft_of_current_user(draft_id)
    db = get_db()
    db.execute("DELETE FROM drafts WHERE id = %s", (draft_id,))
    db.commit()
    flash("草稿已删除", "info")
    return redirect(url_for("drafts_list"))


@app.route("/posts/<int:post_id>/replies", methods=["POST"])
@login_required
def create_reply(post_id):
    db = get_db()
    if db.execute("SELECT id FROM posts WHERE id = %s", (post_id,)).fetchone() is None:
        if wants_json():
            return jsonify(ok=False, error="帖子不存在或已被删除"), 404
        abort(404)

    content = (request.form.get("content") or "").strip()
    if not content:
        return fail("回复内容不能为空")
    if len(content) > REPLY_MAX_LEN:
        return fail(f"回复内容不能超过 {REPLY_MAX_LEN} 字")

    # 每小时回复上限：发的时候不提示，超了才拒绝；管理员不受限制
    if not is_admin_user(db, session["user_id"]) and reply_hour_count(db, session["user_id"]) >= REPLY_LIMIT_PER_HOUR:
        return fail(f"回复太频繁了：每小时最多 {REPLY_LIMIT_PER_HOUR} 条，请稍后再试", 429)

    ts = now_utc_sql()
    anonymous = 1 if request.form.get("is_anonymous") in ("1", "on", "true", "yes") else 0
    cur = db.execute(
        "INSERT INTO replies (post_id, user_id, content, is_anonymous, timestamp) "
        "VALUES (%s, %s, %s, %s, %s)",
        (post_id, session["user_id"], content, anonymous, ts),
    )
    db.commit()

    if wants_json():
        return jsonify(ok=True, reply={
            "id": cur.lastrowid,
            "content": content,   # 回复按纯文本展示，前端用 textContent 填充
            # 匿名回复不能暴露真实姓名，否则"回一句就露馅"
            "display_name": "***" if anonymous else full_name(
                session["grade"], session["class_name"], session["name"]),
            "is_anonymous": bool(anonymous),
            "time": friendly_time(ts),
        })
    return redirect(url_for("index"))


@app.route("/posts/<int:post_id>/delete", methods=["POST"])
@admin_required
def delete_post(post_id):
    db = get_db()
    # 外键 ON DELETE CASCADE 会自动删回复；这里显式删一次，双保险
    db.execute("DELETE FROM replies WHERE post_id = %s", (post_id,))
    db.execute("DELETE FROM posts WHERE id = %s", (post_id,))
    db.commit()  # 图片 BLOB 随帖子行一并删除，无需再处理文件

    if wants_json():
        return jsonify(ok=True)
    return redirect(url_for("index"))


@app.route("/replies/<int:reply_id>/delete", methods=["POST"])
@admin_required
def delete_reply(reply_id):
    db = get_db()
    db.execute("DELETE FROM replies WHERE id = %s", (reply_id,))
    db.commit()
    if wants_json():
        return jsonify(ok=True)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 管理员：置顶 / 取消置顶（全站最多 MAX_PINNED_POSTS 条）
# ---------------------------------------------------------------------------
MAX_PINNED_POSTS = 3


@app.route("/posts/<int:post_id>/pin", methods=["POST"])
@admin_required
def set_pin(post_id):
    # 字段名叫 pin_act，不叫 action：表单里一旦有 <input name="action">，
    # 浏览器里 `form.action` 就会被这个 input 顶替（返回对象而不是 URL），
    # 前端 fetch(form.action) 会拼出 /object HTMLInputElement 这种地址。
    # 这里仍然收一次旧的 action 字段，兼容没刷新过的旧页面。
    raw = request.form.get("pin_act")
    if raw is None:
        raw = request.form.get("action", "pin")
    pin = 1 if raw == "pin" else 0
    db = get_db()
    post = db.execute(
        "SELECT is_pinned FROM posts WHERE id = %s", (post_id,)
    ).fetchone()
    if post is None:
        msg = f"帖子不存在或已被删除（id={post_id}）"
        if wants_json():
            return jsonify(ok=False, error=msg), 404
        abort(404)

    if pin and not post["is_pinned"]:
        pinned_count = db.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE is_pinned = 1"
        ).fetchone()["n"]
        if pinned_count >= MAX_PINNED_POSTS:
            return fail(f"最多只能置顶 {MAX_PINNED_POSTS} 条帖子，请先取消其他置顶")
        # 微秒精度，保证连续置顶时先后顺序明确
        db.execute(
            "UPDATE posts SET is_pinned = 1, pinned_at = %s WHERE id = %s",
            (now_utc_sql_us(), post_id),
        )
    elif not pin and post["is_pinned"]:
        db.execute(
            "UPDATE posts SET is_pinned = 0, pinned_at = NULL WHERE id = %s", (post_id,)
        )
    db.commit()

    if wants_json():
        return jsonify(ok=True, pinned=bool(pin))
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 帖子自动清理：删除 3 个月（90 天）前的帖子，由请求触发、每天最多执行一次
# 说明：两台 Web 都会跑，DELETE 幂等；用 .last_sweep 文件的 mtime 各自节流
# ---------------------------------------------------------------------------
def cleanup_old_posts():
    """删除过期帖子（图片 BLOB、回复随级联/显式删除一并释放），返回删除条数。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=POST_RETENTION_DAYS)
              ).strftime("%Y-%m-%d %H:%M:%S")

    db = get_db()
    # 置顶帖（管理员公告，最多 3 条）不参与 90 天清理，否则半年后公告会自己消失；
    # 取消置顶后它回到普通帖，下一次清理照常按时间删。
    old_posts = db.execute(
        "SELECT id FROM posts WHERE timestamp < %s AND is_pinned = 0", (cutoff,)
    ).fetchall()
    if not old_posts:
        return 0

    old_ids = [p["id"] for p in old_posts]
    placeholders = ",".join(["%s"] * len(old_ids))
    db.execute(f"DELETE FROM replies WHERE post_id IN ({placeholders})", old_ids)
    db.execute(f"DELETE FROM posts WHERE id IN ({placeholders})", old_ids)
    db.commit()
    return len(old_ids)


def purge_stale_quiz_attempts():
    """清掉 30 天前的答题/登录失败记录（随每日清理一起执行，表小、开销可忽略）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=QUIZ_ATTEMPT_KEEP_DAYS)
              ).strftime("%Y-%m-%d %H:%M:%S")
    db = get_db()
    db.execute("DELETE FROM quiz_attempts WHERE last_attempt < %s", (cutoff,))
    try:
        db.execute("DELETE FROM login_attempts WHERE last_attempt < %s", (cutoff,))
    except pymysql.MySQLError:
        pass   # login_attempts 还没建（没重跑 initdb）时忽略
    db.commit()


def maybe_sweep_old_posts():
    """距上次清理不足 SWEEP_INTERVAL 就跳过（每次仅一次文件 stat，开销极小）。"""
    try:
        if os.path.exists(SWEEP_FLAG):
            last = datetime.fromtimestamp(os.path.getmtime(SWEEP_FLAG), timezone.utc)
            if datetime.now(timezone.utc) - last < SWEEP_INTERVAL:
                return
    except OSError:
        pass
    try:
        cleanup_old_posts()
        purge_stale_quiz_attempts()
    except (pymysql.MySQLError, RuntimeError):
        # 清理失败（含数据库连不上）绝不阻断正常请求；
        # 也不能落标记——否则库恢复前的 24 小时里都不会再重试
        return
    try:
        open(SWEEP_FLAG, "a").close()
        os.utime(SWEEP_FLAG, None)  # 记录本次清理时间
    except OSError:
        pass


@app.before_request
def _refuse_insecure_secret():
    """没有配 SECRET_KEY 时拒绝服务（伪造 Session 等于直接伪造管理员身份）。"""
    if not INSECURE_SECRET:
        return None
    if wants_json():
        return jsonify(ok=False, error="服务器配置错误：SECRET_KEY 未设置"), 500
    return (
        "<h4 style='text-align:center;margin-top:4rem;color:#888;'>"
        "服务器配置错误：SECRET_KEY 未设置，已拒绝提供服务<br>"
        "<span style='font-size:.8rem;color:#aaa;'>请在 campus_wall.env 中写入 SECRET_KEY 后重启服务</span>"
        "</h4>"
    ), 500


@app.after_request
def _security_headers(resp):
    """零日志/匿名设计延伸：不让浏览器把本站地址带给任何第三方，也不让爬虫收录。

    另外两件事：
    1. 页面（HTML/JSON）一律 no-store —— 之前踩过的坑：Cloudflare 边缘把首页 HTML
       缓存成旧版本，浏览器拿到的模板还是几小时前的，页面里跑的是老 JS，
       表现就是「同一个操作一会儿是站内提示、一会儿又变回浏览器弹窗」。
       CDN-Cache-Control / Cloudflare-CDN-Cache-Control 是给 CF 看的，CF 认这两个头。
    2. 每个响应都带 X-CW-Build，前端能和页面里的版本号比对，把「页面是旧缓存」
       这件事直接说出来，而不是让人靠猜。
    """
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Robots-Tag", "noindex")
    resp.headers.setdefault("X-CW-Build", APP_BUILD)

    path = request.path or ""
    if path.startswith("/static/") or path.startswith("/media/"):
        # 静态资源与图片内容不变，允许浏览器缓存，省流量也省数据库读取
        resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    else:
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["CDN-Cache-Control"] = "no-store"
        resp.headers["Cloudflare-CDN-Cache-Control"] = "no-store"
    return resp


@app.before_request
def _auto_sweep_old_posts():
    maybe_sweep_old_posts()


# ---------------------------------------------------------------------------
# 管理员：管理员账号管理（超管在网页上输入学号即可提拔/取消，无需操作数据库）
# ---------------------------------------------------------------------------
@app.route("/admin/admins")
@admin_required
def admin_admins():
    admins = get_db().execute(
        "SELECT id, student_id, grade, class_name, name FROM users "
        "WHERE is_admin = 1 ORDER BY id"
    ).fetchall()
    return render_template("admins.html", admins=admins,
                           super_admin_id=ADMIN_STUDENT_ID)


@app.route("/admin/admins/add", methods=["POST"])
@admin_required
def admin_add():
    sid = (request.form.get("student_id") or "").strip()
    if not RE_STUDENT_ID.fullmatch(sid):
        flash("学号必须是 11 位纯数字", "danger")
        return redirect(url_for("admin_admins"))

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE student_id = %s", (sid,)
    ).fetchone()
    if user is None:
        flash("该学号尚未注册：请对方先完成一次注册登录，之后再把他设为管理员", "danger")
        return redirect(url_for("admin_admins"))
    if user["is_admin"]:
        flash(f"{user['grade']}{user['class_name']} {user['name']} 已经是管理员", "info")
        return redirect(url_for("admin_admins"))

    db.execute("UPDATE users SET is_admin = 1 WHERE id = %s", (user["id"],))
    db.commit()
    flash(f"已将 {user['grade']}{user['class_name']} {user['name']} 设为管理员；"
          "若对方当前已登录，请通知其重新登录一次以生效", "success")
    return redirect(url_for("admin_admins"))


@app.route("/admin/admins/<int:uid>/remove", methods=["POST"])
@admin_required
def admin_remove(uid):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone()
    if user is None:
        abort(404)
    if user["student_id"] == ADMIN_STUDENT_ID:
        flash(f"超级管理员（学号 {ADMIN_STUDENT_ID}）不可被取消", "danger")
        return redirect(url_for("admin_admins"))
    if user["id"] == session["user_id"]:
        flash("不能取消你自己的管理员身份", "danger")
        return redirect(url_for("admin_admins"))
    if not user["is_admin"]:
        flash("该用户本来就不是管理员", "info")
        return redirect(url_for("admin_admins"))

    db.execute("UPDATE users SET is_admin = 0 WHERE id = %s", (uid,))
    db.commit()
    flash(f"已取消 {user['grade']}{user['class_name']} {user['name']} 的管理员身份，"
          "权限即时失效", "success")
    return redirect(url_for("admin_admins"))


# ---------------------------------------------------------------------------
# 管理员：题库增删改查
# ---------------------------------------------------------------------------
def read_question_form():
    """读取并校验题库表单，返回 (data_dict, error)"""
    data = {
        "question_text": (request.form.get("question_text") or "").strip(),
        "option_a": (request.form.get("option_a") or "").strip(),
        "option_b": (request.form.get("option_b") or "").strip(),
        "option_c": (request.form.get("option_c") or "").strip(),
        "option_d": (request.form.get("option_d") or "").strip(),
        "correct_answer": (request.form.get("correct_answer") or "").strip(),
    }
    if any(not v for v in data.values()):
        return None, "题干、四个选项和正确答案都不能为空"
    if data["correct_answer"] not in ("A", "B", "C", "D"):
        return None, "正确答案必须是 A / B / C / D 之一"
    if len(data["question_text"]) > 200 or any(
        len(data[k]) > 100 for k in ("option_a", "option_b", "option_c", "option_d")
    ):
        return None, "题干不超过 200 字，选项各不超过 100 字"
    return data, None


@app.route("/admin/questions")
@admin_required
def admin_questions():
    rows = get_db().execute("SELECT * FROM questions ORDER BY id").fetchall()
    return render_template("admin_questions.html", questions=rows)


@app.route("/admin/questions/create", methods=["POST"])
@admin_required
def admin_question_create():
    data, error = read_question_form()
    if error:
        flash(error, "danger")
        return redirect(url_for("admin_questions"))
    db = get_db()
    db.execute(
        "INSERT INTO questions (question_text, option_a, option_b, option_c, "
        "option_d, correct_answer) VALUES (%s, %s, %s, %s, %s, %s)",
        (data["question_text"], data["option_a"], data["option_b"],
         data["option_c"], data["option_d"], data["correct_answer"]),
    )
    db.commit()
    flash("题目已添加", "success")
    return redirect(url_for("admin_questions"))


@app.route("/admin/questions/<int:qid>/edit")
@admin_required
def admin_question_edit(qid):
    q = get_db().execute(
        "SELECT * FROM questions WHERE id = %s", (qid,)
    ).fetchone()
    if q is None:
        abort(404)
    return render_template("question_form.html", q=q)


@app.route("/admin/questions/<int:qid>/update", methods=["POST"])
@admin_required
def admin_question_update(qid):
    db = get_db()
    if db.execute("SELECT id FROM questions WHERE id = %s", (qid,)).fetchone() is None:
        abort(404)
    data, error = read_question_form()
    if error:
        flash(error, "danger")
        return redirect(url_for("admin_question_edit", qid=qid))
    db.execute(
        "UPDATE questions SET question_text=%s, option_a=%s, option_b=%s, option_c=%s, "
        "option_d=%s, correct_answer=%s WHERE id=%s",
        (data["question_text"], data["option_a"], data["option_b"],
         data["option_c"], data["option_d"], data["correct_answer"], qid),
    )
    db.commit()
    flash("题目已更新", "success")
    return redirect(url_for("admin_questions"))


@app.route("/admin/questions/<int:qid>/delete", methods=["POST"])
@admin_required
def admin_question_delete(qid):
    db = get_db()
    db.execute("DELETE FROM questions WHERE id = %s", (qid,))
    db.commit()
    flash("题目已删除", "success")
    return redirect(url_for("admin_questions"))


# ---------------------------------------------------------------------------
# 错误页
# ---------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(e):
    if wants_json():
        return jsonify(ok=False, error="无权限：该操作仅管理员可用"), 403
    return "<h4 style='text-align:center;margin-top:4rem;color:#888;'>403：仅管理员可执行此操作</h4>", 403


@app.errorhandler(404)
def not_found(e):
    if wants_json():
        # 把请求路径带出来：能立刻分清「接口/地址压根不存在」和「帖子被删了」
        return jsonify(ok=False, error=f"接口不存在：{request.path}"), 404
    return "<h4 style='text-align:center;margin-top:4rem;color:#888;'>404：内容不存在或已被删除</h4>", 404


@app.errorhandler(500)
def internal_error(e):
    # 详情仍打到 stderr（journalctl -u campus-wall 能看到完整堆栈），
    # 但客户端拿到的是 JSON / 友好文案，不会再出现「网页报错」或空白
    if wants_json():
        return jsonify(ok=False, error="服务器内部错误，请稍后再试"), 500
    return "<h4 style='text-align:center;margin-top:4rem;color:#888;'>500：服务器开小差了，请稍后再试</h4>", 500


@app.errorhandler(413)
def too_large(e):
    msg = f"上传内容过大（图片最大 {MAX_IMAGE_SIZE // (1024 * 1024)}MB）"
    if wants_json():
        return jsonify(ok=False, error=msg), 413
    flash(msg, "danger")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# 数据库不可用：不把堆栈甩给用户，统一回 503 友好降级页
# 覆盖三种情况：连不上库（DatabaseUnavailable）、连接中途断开/超时、库表异常
# ---------------------------------------------------------------------------
@app.errorhandler(DatabaseUnavailable)
@app.errorhandler(pymysql.OperationalError)
@app.errorhandler(pymysql.InterfaceError)
def database_down(e):
    if wants_json():
        return jsonify(ok=False, error="服务暂时不可用，请稍后重试"), 503
    return render_template("unavailable.html"), 503, {
        "Cache-Control": "no-store",
        "Retry-After": "30",
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "initdb":
        init_db()
        print(f"[ok] 数据库 {DB_NAME}@{DB_HOST}:{DB_PORT} 初始化完成（表已就绪，示例题已播种）。")
    else:
        # 绝不默认开 debug：Werkzeug 调试器等于把交互式 Python 执行权交出去
        app.run(host="127.0.0.1", port=5000,
                debug=os.environ.get("FLASK_DEBUG", "0") == "1")
