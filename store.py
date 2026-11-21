"""
store —— 存储层：账号和历史方案。

默认走 MySQL（本地 3306），连不上会**自动退回 SQLite**，保证答辩现场 MySQL 没起来也能跑。
配置写在 data/db.json：

    {
      "driver": "mysql",
      "host": "127.0.0.1",
      "port": 3306,
      "user": "root",
      "password": "改成你的",
      "database": "optw",
      "sqlite_path": "data/app.db"
    }

也可以用环境变量覆盖：OPTW_DB_HOST / OPTW_DB_PORT / OPTW_DB_USER / OPTW_DB_PASSWORD / OPTW_DB_NAME
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG_FILE = ROOT / "data" / "db.json"

DEFAULTS = {
    "driver": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "optw",
    "sqlite_path": str(ROOT / "data" / "app.db"),
}

MYSQL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(32) NOT NULL UNIQUE,
        pw_hash CHAR(64) NOT NULL,
        salt CHAR(32) NOT NULL,
        role VARCHAR(16) NOT NULL DEFAULT 'user',
        created DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS history (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        user_id INT UNSIGNED NOT NULL,
        title VARCHAR(80) NOT NULL,
        payload MEDIUMTEXT NOT NULL,
        is_public TINYINT NOT NULL DEFAULT 1,
        created DATETIME NOT NULL,
        KEY idx_user (user_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reaction (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        user_id INT UNSIGNED NOT NULL,
        hist_id INT UNSIGNED NOT NULL,
        kind VARCHAR(8) NOT NULL,
        created DATETIME NOT NULL,
        UNIQUE KEY uniq_reaction (user_id, hist_id, kind),
        KEY idx_hist (hist_id, kind)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS comment (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        user_id INT UNSIGNED NOT NULL,
        hist_id INT UNSIGNED NOT NULL,
        body VARCHAR(500) NOT NULL,
        created DATETIME NOT NULL,
        KEY idx_hist (hist_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS doc (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        user_id INT UNSIGNED NOT NULL,
        title VARCHAR(120) NOT NULL,
        source VARCHAR(160) NOT NULL DEFAULT '',
        chars INT UNSIGNED NOT NULL DEFAULT 0,
        chunks INT UNSIGNED NOT NULL DEFAULT 0,
        created DATETIME NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS chunk (
        id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        doc_id INT UNSIGNED NOT NULL,
        ord INT UNSIGNED NOT NULL,
        text TEXT NOT NULL,
        KEY idx_doc (doc_id, ord)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        pw_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        payload TEXT NOT NULL,
        is_public INTEGER NOT NULL DEFAULT 1,
        created TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user ON history(user_id, id)",
    """CREATE TABLE IF NOT EXISTS reaction (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        hist_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        created TEXT NOT NULL,
        UNIQUE (user_id, hist_id, kind)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reaction ON reaction(hist_id, kind)",
    """CREATE TABLE IF NOT EXISTS comment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        hist_id INTEGER NOT NULL,
        body TEXT NOT NULL,
        created TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_comment ON comment(hist_id, id)",
    """CREATE TABLE IF NOT EXISTS doc (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        chars INTEGER NOT NULL DEFAULT 0,
        chunks INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chunk (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL,
        ord INTEGER NOT NULL,
        text TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunk(doc_id, ord)",
]


class DuplicateUser(Exception):
    pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CFG_FILE.exists():
        try:
            cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
        except Exception as exc:                     # noqa: BLE001
            print(f"  [store] data/db.json 解析失败，用默认配置：{exc}")
    for key, env in (("host", "OPTW_DB_HOST"), ("port", "OPTW_DB_PORT"),
                     ("user", "OPTW_DB_USER"), ("password", "OPTW_DB_PASSWORD"),
                     ("database", "OPTW_DB_NAME")):
        if os.getenv(env):
            cfg[key] = os.environ[env]
    cfg["port"] = int(cfg["port"])
    return cfg


class Store:
    """两种后端共用一套接口，SQL 里用 ? 占位，MySQL 侧自动换成 %s。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.kind = self.cfg.get("driver", "mysql")
        self.note = ""
        if self.kind == "mysql":
            try:
                self._ensure_mysql_database()
                self._init_schema()
                self.note = f"MySQL {self.cfg['host']}:{self.cfg['port']}/{self.cfg['database']}"
            except Exception as exc:                  # noqa: BLE001
                self.kind = "sqlite"
                self.note = f"MySQL 连不上（{exc}），已退回 SQLite：{self.cfg['sqlite_path']}"
                self._init_schema()
        else:
            self._init_schema()
            self.note = f"SQLite {self.cfg['sqlite_path']}"
        print(f"  [store] {self.note}")

    # ---------- 连接 ----------
    def _conn(self):
        if self.kind == "mysql":
            import pymysql
            return pymysql.connect(
                host=self.cfg["host"], port=self.cfg["port"],
                user=self.cfg["user"], password=self.cfg["password"],
                database=self.cfg["database"], charset="utf8mb4",
                autocommit=True, cursorclass=pymysql.cursors.DictCursor,
            )
        conn = sqlite3.connect(self.cfg["sqlite_path"])
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_mysql_database(self):
        import pymysql
        name = self.cfg["database"]
        conn = pymysql.connect(
            host=self.cfg["host"], port=self.cfg["port"],
            user=self.cfg["user"], password=self.cfg["password"],
            charset="utf8mb4", autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        finally:
            conn.close()

    def _init_schema(self):
        schema = MYSQL_SCHEMA if self.kind == "mysql" else SQLITE_SCHEMA
        conn = self._conn()
        try:
            cur = conn.cursor()
            for ddl in schema:
                cur.execute(ddl)
            if self.kind != "mysql":
                conn.commit()
        finally:
            conn.close()
        self._migrate()

    def _has_column(self, table: str, col: str) -> bool:
        conn = self._conn()
        try:
            cur = conn.cursor()
            if self.kind == "mysql":
                cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE %s", (col,))
                return cur.fetchone() is not None
            cur.execute(f"PRAGMA table_info({table})")
            return any(r[1] == col for r in cur.fetchall())
        finally:
            conn.close()

    def _migrate(self):
        """老库补列：users.role、history.is_public。CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        todo = []
        if not self._has_column("users", "role"):
            todo.append(("users", "role", "VARCHAR(16) NOT NULL DEFAULT 'user'"
                         if self.kind == "mysql" else "TEXT NOT NULL DEFAULT 'user'"))
        if not self._has_column("history", "is_public"):
            todo.append(("history", "is_public", "TINYINT NOT NULL DEFAULT 1"
                         if self.kind == "mysql" else "INTEGER NOT NULL DEFAULT 1"))
        if not todo:
            return
        conn = self._conn()
        try:
            cur = conn.cursor()
            for table, col, decl in todo:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                print(f"  [store] 迁移：{table} 表补上 {col} 列")
            if self.kind != "mysql":
                conn.commit()
        finally:
            conn.close()

    def bootstrap_admin(self):
        """库里没有任何 admin 时，把最早注册的账号提成管理员，并返回用户名。"""
        row = self._run("SELECT id, username FROM users WHERE role='admin' ORDER BY id LIMIT 1", fetch="one")
        if row:
            return None
        first = self._run("SELECT id, username FROM users ORDER BY id LIMIT 1", fetch="one")
        if not first:
            return None
        self._run("UPDATE users SET role='admin' WHERE id=?", (first["id"],))
        return first["username"]

    def _run(self, sql, args=(), fetch=None):
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace("?", "%s") if self.kind == "mysql" else sql, args)
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row is not None else None
            if fetch == "all":
                return [dict(r) for r in cur.fetchall()]
            if self.kind != "mysql":
                conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    # ---------- 用户 ----------
    def create_user(self, username: str, pw_hash: str, salt: str) -> dict:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            uid = self._run(
                "INSERT INTO users(username, pw_hash, salt, created) VALUES (?,?,?,?)",
                (username, pw_hash, salt, now),
            )
        except Exception as exc:                     # noqa: BLE001
            if "Duplicate" in type(exc).__name__ or "duplicate" in str(exc).lower() \
                    or "UNIQUE" in str(exc).upper():
                raise DuplicateUser(username) from exc
            raise
        return {"id": uid, "username": username}

    def get_user_by_name(self, username: str):
        return self._run("SELECT * FROM users WHERE username=?", (username,), fetch="one")

    def update_username(self, uid: int, username: str) -> None:
        try:
            self._run("UPDATE users SET username=? WHERE id=?", (username, uid))
        except Exception as exc:                     # noqa: BLE001
            if "Duplicate" in type(exc).__name__ or "duplicate" in str(exc).lower() \
                    or "UNIQUE" in str(exc).upper():
                raise DuplicateUser(username) from exc
            raise

    def update_password(self, uid: int, pw_hash: str, salt: str) -> None:
        self._run("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (pw_hash, salt, uid))

    def get_user_by_id(self, uid: int):
        return self._run("SELECT id, username, role, created FROM users WHERE id=?", (uid,), fetch="one")

    # ---------- 历史方案 ----------
    def add_history(self, uid: int, title: str, payload_json: str, public: bool = True) -> int:
        return self._run(
            "INSERT INTO history(user_id, title, payload, is_public, created) VALUES (?,?,?,?,?)",
            (uid, title, payload_json, 1 if public else 0, time.strftime("%Y-%m-%d %H:%M:%S")),
        )

    def list_history(self, uid: int, limit: int = 50):
        return self._run(
            "SELECT id, title, is_public, created FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (uid, limit), fetch="all",
        )

    def get_history(self, uid: int, hid: int):
        return self._run(
            "SELECT id, title, payload, is_public, created FROM history WHERE id=? AND user_id=?",
            (hid, uid), fetch="one",
        )

    def set_public(self, hid: int, public: bool) -> None:
        self._run("UPDATE history SET is_public=? WHERE id=?", (1 if public else 0, hid))

    def delete_history(self, uid: int, hid: int) -> bool:
        conn = self._conn()
        try:
            cur = conn.cursor()
            sql = "DELETE FROM history WHERE id=? AND user_id=?"
            cur.execute(sql.replace("?", "%s") if self.kind == "mysql" else sql, (hid, uid))
            if self.kind != "mysql":
                conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def count(self, table: str) -> int:
        row = self._run(f"SELECT COUNT(*) AS n FROM {table}", fetch="one")
        return int(row["n"]) if row else 0

    # ---------- 用户（管理端） ----------
    def list_users(self):
        return self._run(
            """SELECT u.id, u.username, u.role, u.created,
                      (SELECT COUNT(*) FROM history h WHERE h.user_id = u.id) AS plans
               FROM users u ORDER BY u.id""",
            fetch="all",
        )

    def set_role(self, uid: int, role: str) -> bool:
        if role not in ("user", "admin"):
            raise ValueError(role)
        self._run("UPDATE users SET role=? WHERE id=?", (role, uid))
        return True

    # ---------- 历史方案（管理端） ----------
    def list_all_history(self, limit: int = 200):
        return self._run(
            """SELECT h.id, h.title, h.is_public, h.created, u.username
               FROM history h LEFT JOIN users u ON u.id = h.user_id
               ORDER BY h.id DESC LIMIT ?""",
            (limit,), fetch="all",
        )

    def get_history_admin(self, hid: int):
        return self._run(
            "SELECT id, user_id, title, payload, is_public, created FROM history WHERE id=?",
            (hid,), fetch="one")

    def list_public_history(self, limit: int = 60):
        """社区流：只列公开的方案 + 赞/藏/评论数，按赞数排。"""
        return self._run(
            """SELECT h.id, h.title, h.created, u.username,
                      (SELECT COUNT(*) FROM reaction r WHERE r.hist_id=h.id AND r.kind='like') AS likes,
                      (SELECT COUNT(*) FROM reaction r WHERE r.hist_id=h.id AND r.kind='fav') AS favs,
                      (SELECT COUNT(*) FROM comment c WHERE c.hist_id=h.id) AS comments
               FROM history h LEFT JOIN users u ON u.id = h.user_id
               WHERE h.is_public = 1
               ORDER BY likes DESC, h.id DESC LIMIT ?""",
            (limit,), fetch="all")

    def delete_history_admin(self, hid: int) -> bool:
        conn = self._conn()
        try:
            cur = conn.cursor()
            sql = "DELETE FROM history WHERE id=?"
            cur.execute(sql.replace("?", "%s") if self.kind == "mysql" else sql, (hid,))
            if self.kind != "mysql":
                conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def stats(self) -> dict:
        return {
            "users": self.count("users"),
            "admins": int((self._run("SELECT COUNT(*) AS n FROM users WHERE role='admin'", fetch="one") or {"n": 0})["n"]),
            "history": self.count("history"),
            "likes": int((self._run("SELECT COUNT(*) AS n FROM reaction WHERE kind='like'", fetch="one") or {"n": 0})["n"]),
            "favs": int((self._run("SELECT COUNT(*) AS n FROM reaction WHERE kind='fav'", fetch="one") or {"n": 0})["n"]),
            "comments": self.count("comment"),
        }

    # ---------- 收藏 / 点赞 / 评论 ----------
    def toggle_reaction(self, uid: int, hid: int, kind: str) -> dict:
        """同一用户对同一方案同一动作点一次加、再点一次取消。"""
        if kind not in ("like", "fav"):
            raise ValueError(kind)
        row = self._run(
            "SELECT id FROM reaction WHERE user_id=? AND hist_id=? AND kind=?",
            (uid, hid, kind), fetch="one")
        if row:
            self._run("DELETE FROM reaction WHERE id=?", (row["id"],))
            on = False
        else:
            self._run(
                "INSERT INTO reaction(user_id, hist_id, kind, created) VALUES (?,?,?,?)",
                (uid, hid, kind, time.strftime("%Y-%m-%d %H:%M:%S")))
            on = True
        n = self._run("SELECT COUNT(*) AS n FROM reaction WHERE hist_id=? AND kind=?", (hid, kind), fetch="one")
        return {"on": on, "count": int(n["n"]) if n else 0}

    def reaction_state(self, uid: int, hid: int) -> dict:
        def one(kind):
            r = self._run("SELECT id FROM reaction WHERE user_id=? AND hist_id=? AND kind=?",
                          (uid, hid, kind), fetch="one")
            n = self._run("SELECT COUNT(*) AS n FROM reaction WHERE hist_id=? AND kind=?",
                          (hid, kind), fetch="one")
            return {"on": r is not None, "count": int(n["n"]) if n else 0}
        return {"like": one("like"), "fav": one("fav")}

    def add_comment(self, uid: int, hid: int, body: str) -> int:
        return self._run(
            "INSERT INTO comment(user_id, hist_id, body, created) VALUES (?,?,?,?)",
            (uid, hid, body, time.strftime("%Y-%m-%d %H:%M:%S")))

    def list_comments(self, hid: int):
        return self._run(
            """SELECT c.id, c.body, c.created, u.username
               FROM comment c LEFT JOIN users u ON u.id = c.user_id
               WHERE c.hist_id=? ORDER BY c.id""",
            (hid,), fetch="all")

    # ---------- 知识库 ----------
    def add_doc(self, uid: int, title: str, source: str, chunks: list) -> int:
        """一次事务写入文档和它的所有片段。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn = self._conn()
        try:
            cur = conn.cursor()
            ph = "%s" if self.kind == "mysql" else "?"
            sql = (f"INSERT INTO doc(user_id, title, source, chars, chunks, created) "
                   f"VALUES ({','.join([ph] * 6)})")
            cur.execute(sql, (uid, title, source, sum(len(c) for c in chunks), len(chunks), now))
            doc_id = cur.lastrowid
            for i, text in enumerate(chunks):
                cur.execute(
                    f"INSERT INTO chunk(doc_id, ord, text) VALUES ({ph},{ph},{ph})",
                    (doc_id, i, text))
            if self.kind != "mysql":
                conn.commit()
            return doc_id
        finally:
            conn.close()

    def list_docs(self):
        return self._run(
            """SELECT d.id, d.title, d.source, d.chars, d.chunks, d.created, u.username
               FROM doc d LEFT JOIN users u ON u.id = d.user_id
               ORDER BY d.id DESC""", fetch="all")

    def delete_doc(self, doc_id: int) -> bool:
        conn = self._conn()
        try:
            cur = conn.cursor()
            ph = "%s" if self.kind == "mysql" else "?"
            cur.execute(f"DELETE FROM chunk WHERE doc_id={ph}", (doc_id,))
            cur.execute(f"DELETE FROM doc WHERE id={ph}", (doc_id,))
            n = cur.rowcount
            if self.kind != "mysql":
                conn.commit()
            return n > 0
        finally:
            conn.close()

    def all_chunks(self):
        return self._run(
            """SELECT c.id, c.doc_id, c.ord, c.text, d.title
               FROM chunk c LEFT JOIN doc d ON d.id = c.doc_id
               ORDER BY c.doc_id, c.ord""", fetch="all")

    # ---------- 热门榜 ----------
    def poi_pick_counts(self) -> dict:
        """统计每个景点名在所有历史方案里被排进行程的次数。数据量小，直接 Python 里数。"""
        rows = self._run("SELECT payload FROM history", fetch="all") or []
        counts: dict[str, int] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except Exception:                          # noqa: BLE001
                continue
            variants = payload.get("variants") or []
            plan = None
            for v in variants:                         # 主方案是 local，没有就取第一个
                if v.get("key") == "local":
                    plan = v.get("plan")
                    break
            if plan is None and variants:
                plan = variants[0].get("plan")
            for day in (plan or {}).get("days", []):
                for s in day.get("stops", []):
                    name = s.get("name")
                    if name:
                        counts[name] = counts.get(name, 0) + 1
        return counts
