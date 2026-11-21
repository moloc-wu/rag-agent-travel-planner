"""
migrate —— 把 SQLite 里的账号和历史方案搬到 MySQL。

用法：
    python migrate.py            # 目标库为空才搬
    python migrate.py --force    # 允许多次执行（会跳过已存在的用户名）

注意：MySQL 配置读 data/db.json（或环境变量），和 server.py 用的是同一份。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

import store as store_mod

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="目标已有数据也继续搬（跳过重名）")
    args = ap.parse_args()

    cfg = store_mod.load_config()
    if cfg.get("driver") != "mysql":
        print("  data/db.json 里 driver 不是 mysql，没什么可迁的")
        return

    # 先让 Store 把 MySQL 库和表建出来（连不上会抛）
    s = store_mod.Store(cfg)
    if s.kind != "mysql":
        print(f"  {s.note}")
        print("  MySQL 没连上，迁移跳过")
        return

    import pymysql
    src_path = cfg["sqlite_path"]
    with closing(sqlite3.connect(src_path)) as src:
        src.row_factory = sqlite3.Row
        users = [dict(r) for r in src.execute(
            "SELECT id, username, pw_hash, salt, created FROM users").fetchall()]
        hist = [dict(r) for r in src.execute(
            "SELECT id, user_id, title, payload, created FROM history").fetchall()]

    print(f"  SQLite：{len(users)} 个账号 / {len(hist)} 条历史")
    if not users and not hist:
        print("  没东西可搬")
        return

    dst = pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
        database=cfg["database"], charset="utf8mb4", autocommit=True,
    )
    moved_u = moved_h = 0
    try:
        with dst.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            if cur.fetchone()[0] > 0 and not args.force:
                print("  MySQL 里已经有账号了，没搬（要强搬加 --force）")
                return
            for u in users:
                cur.execute(
                    "INSERT IGNORE INTO users(id, username, pw_hash, salt, created) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (u["id"], u["username"], u["pw_hash"], u["salt"], u["created"]),
                )
                moved_u += cur.rowcount
            for h in hist:
                cur.execute(
                    "INSERT IGNORE INTO history(id, user_id, title, payload, created) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (h["id"], h["user_id"], h["title"], h["payload"], h["created"]),
                )
                moved_h += cur.rowcount
    finally:
        dst.close()

    print(f"  搬过去：{moved_u} 个账号 / {moved_h} 条历史")
    print(f"  目标：{cfg['host']}:{cfg['port']}/{cfg['database']}")


if __name__ == "__main__":
    main()
