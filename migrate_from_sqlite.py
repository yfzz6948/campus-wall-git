# -*- coding: utf-8 -*-
"""
一次性数据迁移脚本：旧版 SQLite（campus_wall.db） -> MariaDB v5

使用前提：
  1. MariaDB 已建好库（utf8mb4），且已在目标库执行过一次：python app.py initdb
  2. 目标库必须是空库（users 表没有数据），避免主键冲突；如已有测试数据请先 DROP DATABASE 重建
  3. 旧版图片文件仍在 static/uploads/ 目录中（脚本会把它们读入数据库）

用法（在任意一台能连上数据库的机器上执行一次）：
  set DB_HOST=...  & set DB_USER=... & set DB_PASSWORD=...   (Windows CMD)
  export DB_HOST=... DB_USER=... DB_PASSWORD=...             (Linux)
  python migrate_from_sqlite.py [旧库路径，默认 ./campus_wall.db]

环境变量与 app.py 完全相同：DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
"""
import os
import sqlite3
import sys

import pymysql
from pymysql.cursors import DictCursor

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def load_env_file(path=None):
    """与 app.py 相同的 env 读取：已存在的环境变量优先，文件只补缺失项。"""
    path = path or os.path.join(BASE_DIR, "campus_wall.env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except OSError:
        pass


load_env_file()

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "campus_wall")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "campus_wall")

MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}


def to_dt(value):
    """SQLite 的 ISO 字符串（带 T）转 MariaDB DATETIME 字符串（空格分隔）。"""
    if value is None:
        return None
    return str(value).replace("T", " ")


def main():
    sqlite_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "campus_wall.db")
    if not os.path.isfile(sqlite_path):
        sys.exit(f"[x] 找不到旧 SQLite 库：{sqlite_path}")

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    # 旧库 posts 可能没有置顶列（更早的版本），按实际列名兼容
    post_cols = {r[1] for r in src.execute("PRAGMA table_info(posts)")}

    dst = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, charset="utf8mb4", cursorclass=DictCursor,
        autocommit=False,
    )

    try:
        with dst.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            if cur.fetchone()["n"] > 0:
                sys.exit("[x] 目标库 users 表已有数据。为避免主键冲突，"
                         "请清空目标库（DROP DATABASE 后重建并重新 initdb）再迁移。")

            # ---- 用户（保留原 id）----
            users = src.execute(
                "SELECT id, student_id, grade, class_name, name, is_admin FROM users"
            ).fetchall()
            n_user = 0
            for u in users:
                cur.execute(
                    "INSERT INTO users (id, student_id, grade, class_name, name, is_admin) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (u["id"], u["student_id"], u["grade"], u["class_name"],
                     u["name"], u["is_admin"]),
                )
                n_user += 1

            # ---- 帖子（图片文件 -> BLOB；置顶列缺失则按默认值）----
            select_cols = "id, user_id, content, image_path, is_anonymous, timestamp"
            if "is_pinned" in post_cols:
                select_cols += ", is_pinned, pinned_at"
            posts = src.execute(f"SELECT {select_cols} FROM posts").fetchall()
            n_post = n_img = 0
            for p in posts:
                image_data, image_mime = None, None
                image_path = p["image_path"]
                if image_path:
                    fp = os.path.join(BASE_DIR, "static", image_path)
                    if os.path.isfile(fp):
                        ext = image_path.rsplit(".", 1)[-1].lower()
                        with open(fp, "rb") as f:
                            image_data = f.read()
                        image_mime = MIME_BY_EXT.get(ext, "image/jpeg")
                        n_img += 1
                    else:
                        print(f"[!] 帖子 {p['id']} 的图片文件缺失，已跳过图片：{fp}")
                cur.execute(
                    "INSERT INTO posts (id, user_id, content, image_data, image_mime, "
                    "is_anonymous, is_pinned, pinned_at, timestamp) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (p["id"], p["user_id"], p["content"], image_data, image_mime,
                     p["is_anonymous"],
                     p["is_pinned"] if "is_pinned" in post_cols else 0,
                     to_dt(p["pinned_at"]) if "pinned_at" in post_cols else None,
                     to_dt(p["timestamp"])),
                )
                n_post += 1

            # ---- 回复 ----
            n_reply = 0
            replies = src.execute(
                "SELECT id, post_id, user_id, content, timestamp FROM replies"
            ).fetchall()
            for r in replies:
                cur.execute(
                    "INSERT INTO replies (id, post_id, user_id, content, timestamp) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (r["id"], r["post_id"], r["user_id"], r["content"],
                     to_dt(r["timestamp"])),
                )
                n_reply += 1

            # ---- 题库：用旧库题目覆盖 initdb 播种的示例题（旧库有题时）----
            n_question = 0
            q_cols = {r[1] for r in src.execute("PRAGMA table_info(questions)")}
            if q_cols:
                questions = src.execute(
                    "SELECT id, question_text, option_a, option_b, option_c, option_d, "
                    "correct_answer FROM questions"
                ).fetchall()
                if questions:
                    cur.execute("DELETE FROM questions")
                    for q in questions:
                        cur.execute(
                            "INSERT INTO questions (id, question_text, option_a, option_b, "
                            "option_c, option_d, correct_answer) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (q["id"], q["question_text"], q["option_a"], q["option_b"],
                             q["option_c"], q["option_d"], q["correct_answer"]),
                        )
                        n_question += 1

        dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()

    print(f"[ok] 迁移完成：用户 {n_user} 人，帖子 {n_post} 条（含图片 {n_img} 张），"
          f"回复 {n_reply} 条，题目 {n_question} 道。")
    print("[提示] 迁移后请检查两台 Web 的环境变量并重启服务，确认无误后再删除旧 SQLite 文件。")


if __name__ == "__main__":
    main()
