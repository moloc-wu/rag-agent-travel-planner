"""fetch_trains.py —— 从 12306 抓城际车次，存进 MySQL（optw.train_schedule）。

只查公开的余票查询页，不登录、不提交订单。抓一次存静态，运行时查库。

用法：
  python fetch_trains.py --date 2026-11-15 --sample      # 只抓 3 对，验证链路
  python fetch_trains.py --date 2026-11-15               # 抓全量（21 城双向 420 对）
  python fetch_trains.py --date 2026-11-15 --resume      # 跳过库里已有的，续抓

默认每对间隔 1.2~2.2 秒随机，420 对约 12 分钟。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
import http.cookiejar
from datetime import datetime
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 城市 -> (主站中文名, 车站电报码)。选的是各市高铁主客站。
# 敦煌在 12306 车站表里查不到（无高铁客运站），城际仍走公式估算。
CITY_STATION = {
    "长沙": ("长沙南", "CWQ"),
    "北京": ("北京南", "VNP"),
    "上海": ("上海虹桥", "AOH"),
    "南京": ("南京南", "NKH"),
    "厦门": ("厦门北", "XKS"),
    "呼伦贝尔": ("海拉尔", "HRX"),
    "哈尔滨": ("哈尔滨西", "VAB"),
    "天津": ("天津西", "TXP"),
    "广州": ("广州南", "IZQ"),
    "成都": ("成都东", "ICW"),
    "拉萨": ("拉萨", "LSO"),
    "昆明": ("昆明南", "KOM"),
    "杭州": ("杭州东", "HGH"),
    "武汉": ("武汉", "WHN"),
    "洛阳": ("洛阳龙门", "LLF"),
    "深圳": ("深圳北", "IOQ"),
    "苏州": ("苏州北", "OHH"),
    "西宁": ("西宁", "XNO"),
    "西安": ("西安北", "EAY"),
    "重庆": ("重庆北", "CUW"),
    "青岛": ("青岛北", "QHK"),
}

DDL = """
CREATE TABLE IF NOT EXISTS train_schedule (
  id INT AUTO_INCREMENT PRIMARY KEY,
  query_date DATE NOT NULL COMMENT '查询的乘车日期',
  from_city VARCHAR(24) NOT NULL,
  to_city VARCHAR(24) NOT NULL,
  from_station VARCHAR(24) NOT NULL,
  to_station VARCHAR(24) NOT NULL,
  train_no VARCHAR(16) NOT NULL COMMENT '内部车次号',
  train_code VARCHAR(16) NOT NULL COMMENT '车次号，如 G502',
  train_type CHAR(1) NOT NULL COMMENT 'G/D/C/Z/T/K 等',
  dep_time CHAR(5) NOT NULL COMMENT '发车 HH:MM',
  arr_time CHAR(5) NOT NULL COMMENT '到达 HH:MM',
  duration_min SMALLINT NOT NULL COMMENT '历时（分钟）',
  next_day TINYINT NOT NULL DEFAULT 0 COMMENT '是否次日到达',
  seats_raw VARCHAR(255) NOT NULL DEFAULT '' COMMENT '各席别余票原文',
  price_raw VARCHAR(255) NOT NULL DEFAULT '' COMMENT '票价编码原文（待解析）',
  fetched_at DATETIME NOT NULL,
  UNIQUE KEY uk_route_train (query_date, from_station, to_station, train_no),
  KEY idx_route (from_station, to_station),
  KEY idx_city (from_city, to_city)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def db():
    cfg = json.loads((ROOT / "data" / "db.json").read_text(encoding="utf-8"))
    if cfg.get("driver") != "mysql":
        raise SystemExit("data/db.json 里 driver 不是 mysql，这条脚本只写 MySQL")
    return pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        charset="utf8mb4", autocommit=False,
    )


class Session:
    """一个带 cookie 的会话；12306 不先拿 cookie 会直接拒。"""

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "*/*"),
            ("Referer", "https://kyfw.12306.cn/otn/leftTicket/init"),
        ]

    def get(self, url: str) -> str:
        with self.opener.open(url, timeout=25) as r:
            return r.read().decode("utf-8", "ignore")

    def warmup(self) -> None:
        self.get("https://kyfw.12306.cn/otn/leftTicket/init")


def query(sess: Session, date: str, frm: str, to: str, retries: int = 3) -> list:
    url = (
        "https://kyfw.12306.cn/otn/leftTicket/queryZ"
        f"?leftTicketDTO.train_date={date}"
        f"&leftTicketDTO.from_station={frm}"
        f"&leftTicketDTO.to_station={to}"
        "&purpose_codes=ADULT"
    )
    last = None
    for i in range(retries):
        try:
            d = json.loads(sess.get(url))
            data = d.get("data")
            if isinstance(data, dict):
                return data.get("result") or []
            if d.get("status") is False and d.get("messages"):
                # 会话过期之类的软失败，重新 warmup 再来
                sess.warmup()
                last = d.get("messages")
            else:
                return []
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
            try:
                sess.warmup()
            except Exception:  # noqa: BLE001
                pass
    print(f"    放弃 {frm}->{to}：{last}")
    return []


def parse_row(row: str) -> dict | None:
    p = row.split("|")
    if len(p) < 36 or not p[3]:
        return None
    code = p[3]
    dep, arr = p[8], p[9]
    if not dep or not arr:
        return None
    # 12306 对「当天不可售 / 订不到」的车次给的是 24:00 + 99:59，这种别入库
    if dep == "24:00" or arr == "24:00":
        return None
    dur = p[10] or "00:00"
    try:
        hh, mm = dur.split(":")
        minutes = int(hh) * 60 + int(mm)
    except ValueError:
        minutes = 0
    if minutes <= 0 or minutes > 2880:
        return None
    next_day = 0 if p[11] == "Y" else 1
    return {
        "train_no": p[2],
        "train_code": code,
        "train_type": code[0].upper(),
        "dep_time": dep,
        "arr_time": arr,
        "duration_min": minutes,
        "next_day": next_day,
        "seats_raw": "|".join(p[21:34])[:255],
        "price_raw": (p[39] if len(p) > 39 else "")[:255],
    }


def save(conn, date, fcity, fsta, fcode, tcity, tsta, tcode, rows) -> int:
    cur = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = []
    for r in rows:
        rec = parse_row(r)
        if not rec:
            continue
        payload.append((
            date, fcity, tcity, fcode, tcode, rec["train_no"], rec["train_code"],
            rec["train_type"], rec["dep_time"], rec["arr_time"],
            rec["duration_min"], rec["next_day"], rec["seats_raw"],
            rec["price_raw"], now,
        ))
    if not payload:
        return 0
    cur.executemany(
        "INSERT INTO train_schedule "
        "(query_date, from_city, to_city, from_station, to_station, train_no, "
        " train_code, train_type, dep_time, arr_time, duration_min, next_day, "
        " seats_raw, price_raw, fetched_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        " dep_time=VALUES(dep_time), arr_time=VALUES(arr_time), "
        " duration_min=VALUES(duration_min), next_day=VALUES(next_day), "
        " seats_raw=VALUES(seats_raw), price_raw=VALUES(price_raw), "
        " fetched_at=VALUES(fetched_at)",
        payload,
    )
    conn.commit()
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="乘车日期 YYYY-MM-DD，默认取第 7 天")
    ap.add_argument("--sample", action="store_true", help="只抓 3 对")
    ap.add_argument("--resume", action="store_true", help="跳过库里已有的")
    args = ap.parse_args()

    date = args.date
    if not date:
        from datetime import date as _d, timedelta
        date = (_d.today() + timedelta(days=7)).isoformat()
    print(f"乘车日期：{date}")

    conn = db()
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()

    done = set()
    if args.resume:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT from_station, to_station FROM train_schedule "
                "WHERE query_date=%s GROUP BY from_station, to_station",
                (date,),
            )
            done = {(a, b) for a, b in cur.fetchall()}
        print(f"库里已有 {len(done)} 对，跳过")

    cities = list(CITY_STATION.items())
    pairs = [
        (a, b) for a, _ in cities for b, _ in cities if a != b
    ]
    if args.sample:
        pairs = [(a, b) for a, b in pairs if a in ("长沙", "北京")][:3] or pairs[:3]
    print(f"待抓 {len(pairs)} 对")

    sess = Session()
    sess.warmup()
    print("会话就绪")

    total_rows = 0
    t0 = time.time()
    for i, (fc, tc) in enumerate(pairs, 1):
        fsta, fcode = CITY_STATION[fc]
        tsta, tcode = CITY_STATION[tc]
        if (fcode, tcode) in done:
            continue
        rows = query(sess, date, fcode, tcode)
        n = save(conn, date, fc, fsta, fcode, tc, tsta, tcode, rows)
        total_rows += n
        print(f"[{i}/{len(pairs)}] {fc}→{tc}  车次 {len(rows):2d}  入库 {n:2d}，累计 {total_rows}")
        if i < len(pairs):
            time.sleep(random.uniform(1.2, 2.2))

    conn.close()
    cost = time.time() - t0
    print(f"\n完成：{len(pairs)} 对，入库 {total_rows} 条，用时 {cost / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
