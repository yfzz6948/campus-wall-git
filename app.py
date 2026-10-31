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
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps

import bleach
import markdown as md_lib
import pymysql
from pymysql.cursors import DictCursor
from flask import (
    Flask, g, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, Response,
)

# 零日志：werkzeug 默认逐请求打印“IP - - [时间] 请求行”，直接压到 ERROR 级别
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# 配置（数据库连接信息全部来自环境变量，同一份代码在两台机器上跑）
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "campus_wall")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "campus_wall")

ADMIN_STUDENT_ID = "11111111"   # 超级管理员学号（注册时自动 is_admin=1）

POST_MAX_LEN = 2000
REPLY_MAX_LEN = 200
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 单图上限 5MB（MEDIUMBLOB 上限 16MB）
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp"}
IMAGE_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}

# 帖子自动清理：删除 90 天（约 3 个月）前的帖子；每 24 小时最多真正清理一次
POST_RETENTION_DAYS = 90
SWEEP_INTERVAL = timedelta(days=1)
SWEEP_FLAG = os.path.join(BASE_DIR, ".last_sweep")   # 用文件 mtime 记录上次清理时间

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-please-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 文本 + 一张图，留余量

BJT = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 格式校验正则（前后端一致，后端是最终边界）
# ---------------------------------------------------------------------------
RE_STUDENT_ID = re.compile(r"^\d{11}$")              # 学号：11 位纯数字
RE_GRADE = re.compile(r"^\d{1,2}$")                  # 年级：1~2 位数字，如 24
RE_CLASS = re.compile(r"^[一-龥A-Za-z0-9]{1,10}$")   # 班级：如 会计2 / 网络1
RE_NAME = re.compile(r"^[一-龥]{2,4}$")              # 姓名：2~4 个中文字符

# Markdown 渲染白名单：只保留最基础文本排版，任何能发起网络请求的标签/属性都不放行
MD_ALLOWED_TAGS = ["p", "br", "b", "strong", "i", "em", "code"]
MD_ALLOWED_ATTRIBUTES = {}
MD_ALLOWED_PROTOCOLS = []   # 不允许任何协议（无 href/src，无法外发请求）

# ---------------------------------------------------------------------------
# 数据库连接（每请求一个连接，请求结束自动关闭；校园墙量级无需连接池）
# ---------------------------------------------------------------------------
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
        raise RuntimeError(
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
        id        INT PRIMARY KEY AUTO_INCREMENT,
        post_id   INT NOT NULL,
        user_id   INT NOT NULL,
        content   TEXT NOT NULL,
        timestamp DATETIME NOT NULL,
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
]


def init_db():
    """幂等建表 + 首次写入示例题库（python app.py initdb，部署时只执行一次）。"""
    db = connect_db()
    try:
        with db.cursor() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
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


def render_markdown(text):
    """Markdown -> HTML -> bleach 严格清洗（安全边界，务必先转后洗）"""
    if not text:
        return ""
    raw_html = md_lib.markdown(text, extensions=["nl2br"])
    return bleach.clean(
        raw_html,
        tags=MD_ALLOWED_TAGS,
        attributes=MD_ALLOWED_ATTRIBUTES,
        protocols=MD_ALLOWED_PROTOCOLS,
        strip=True,
    )


app.jinja_env.filters["ftime"] = friendly_time
app.jinja_env.filters["md"] = render_markdown


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


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
        return False, "图片大小不能超过 5MB"

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

        # 后端硬性正则校验
        if not RE_STUDENT_ID.fullmatch(student_id):
            flash("学号必须是 11 位纯数字", "danger")
            return render_template("login.html"), 400
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
                flash("登录失败，请检查身份信息是否匹配", "danger")
                return render_template("login.html"), 401
            start_session(user)
            return redirect(url_for("index"))

        # 新学号：暂存待注册信息，跳转答题验证
        session["pending"] = {
            "student_id": student_id, "grade": grade,
            "class_name": class_name, "name": name,
        }
        return redirect(url_for("register_quiz"))

    return render_template("login.html")


@app.route("/register/quiz", methods=["GET", "POST"])
def register_quiz():
    pending = session.get("pending")
    if not pending:
        return redirect(url_for("login"))

    db = get_db()

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
            flash("验证失败：有题目答错，请重新作答", "danger")
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
    posts = db.execute(
        """
        SELECT p.id, p.content, (p.image_data IS NOT NULL) AS has_image,
               p.is_anonymous, p.is_pinned, p.pinned_at, p.timestamp,
               u.grade, u.class_name, u.name
          FROM posts AS p JOIN users AS u ON u.id = p.user_id
         ORDER BY p.is_pinned DESC, p.pinned_at DESC, p.id DESC
        """
    ).fetchall()

    replies_by_post = {}
    if posts:
        post_ids = [p["id"] for p in posts]
        placeholders = ",".join(["%s"] * len(post_ids))
        replies = db.execute(
            f"""
            SELECT r.id, r.post_id, r.content, r.timestamp,
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
        is_admin=bool(session.get("is_admin")),
    )


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

    image_mime, image_data = None, None
    if has_image:
        ok, result = read_uploaded_image(image)
        if not ok:
            return fail(result)
        image_mime, image_data = result

    anonymous = 1 if request.form.get("is_anonymous") in ("1", "on", "true", "yes") else 0
    ts = now_utc_sql()
    db = get_db()
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

    ts = now_utc_sql()
    cur = db.execute(
        "INSERT INTO replies (post_id, user_id, content, timestamp) "
        "VALUES (%s, %s, %s, %s)",
        (post_id, session["user_id"], content, ts),
    )
    db.commit()

    if wants_json():
        return jsonify(ok=True, reply={
            "id": cur.lastrowid,
            "content": content,   # 回复按纯文本展示，前端用 textContent 填充
            "display_name": full_name(session["grade"], session["class_name"], session["name"]),
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
    pin = 1 if request.form.get("action", "pin") == "pin" else 0
    db = get_db()
    post = db.execute(
        "SELECT is_pinned FROM posts WHERE id = %s", (post_id,)
    ).fetchone()
    if post is None:
        if wants_json():
            return jsonify(ok=False, error="帖子不存在或已被删除"), 404
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
    old_posts = db.execute(
        "SELECT id FROM posts WHERE timestamp < %s", (cutoff,)
    ).fetchall()
    if not old_posts:
        return 0

    old_ids = [p["id"] for p in old_posts]
    placeholders = ",".join(["%s"] * len(old_ids))
    db.execute(f"DELETE FROM replies WHERE post_id IN ({placeholders})", old_ids)
    db.execute(f"DELETE FROM posts WHERE id IN ({placeholders})", old_ids)
    db.commit()
    return len(old_ids)


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
    except (pymysql.MySQLError, RuntimeError):
        pass  # 清理失败（含数据库连不上）绝不阻断正常请求
    finally:
        try:
            open(SWEEP_FLAG, "a").close()
            os.utime(SWEEP_FLAG, None)  # 记录本次清理时间
        except OSError:
            pass


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
    return "<h4 style='text-align:center;margin-top:4rem;color:#888;'>403：仅管理员可执行此操作</h4>", 403


@app.errorhandler(404)
def not_found(e):
    return "<h4 style='text-align:center;margin-top:4rem;color:#888;'>404：内容不存在或已被删除</h4>", 404


@app.errorhandler(413)
def too_large(e):
    if wants_json():
        return jsonify(ok=False, error="上传内容过大（图片最大 5MB）"), 413
    flash("上传内容过大（图片最大 5MB）", "danger")
    return redirect(url_for("index"))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "initdb":
        init_db()
        print(f"[ok] 数据库 {DB_NAME}@{DB_HOST}:{DB_PORT} 初始化完成（表已就绪，示例题已播种）。")
    else:
        app.run(host="0.0.0.0", port=5000, debug=True)
