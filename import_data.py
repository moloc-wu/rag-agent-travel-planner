"""
import_data.py —— 把 data/cities 下的 JSON 清洗后导进 MySQL（第 4 条，第一步）。

这一步**只写库，不改任何读取逻辑**：跑完页面还是读 JSON，库里的数据用来做校验和下一步切换。
清洗口径：
  · 餐饮类单独打标记 is_meal=1（餐厅不删，第 24 条要用它推荐吃饭），排景点时按它过滤
  · 商场类不入景点池（写库时 is_mall=1，同样靠查询过滤）
  · 其余字段原样搬

用法：
  python import_data.py            # 建表 + 全量导入
  python import_data.py --stat     # 只看统计，不导
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pymysql

from optw_core import infer_closed_days

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CITIES = DATA / "cities"

# 餐饮 / 商场判定（跟 planner.is_meal 一套口径，写库前先算好）
MEAL_KEYS = ("餐", "饭", "火锅", "小吃", "面馆", "菜", "食府", "茶", "咖啡", "烧烤", "酒家", "食堂")
MALL_KEYS = ("商场", "购物中心", "百货", "广场购物", "商业广场")

DDL = [
    """CREATE TABLE IF NOT EXISTS city (
        name VARCHAR(24) PRIMARY KEY COMMENT '城市名',
        lng DOUBLE COMMENT '默认住宿点经度',
        lat DOUBLE COMMENT '默认住宿点纬度',
        poi_count INT DEFAULT 0,
        hotel_count INT DEFAULT 0,
        has_matrix TINYINT DEFAULT 0 COMMENT '有没有实测驾车矩阵',
        datum VARCHAR(8) DEFAULT '' COMMENT 'gcj02 / wgs84',
        source VARCHAR(16) DEFAULT '' COMMENT 'amap 等',
        note VARCHAR(255) DEFAULT '',
        updated_at DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS poi (
        id INT AUTO_INCREMENT PRIMARY KEY,
        city VARCHAR(24) NOT NULL,
        poi_id VARCHAR(24) NOT NULL COMMENT '原 JSON 里的 id，如 am01',
        name VARCHAR(160) NOT NULL,
        category VARCHAR(48) DEFAULT '',
        tags JSON,
        lng DOUBLE, lat DOUBLE,
        open_time CHAR(5) DEFAULT '', close_time CHAR(5) DEFAULT '',
        visit_min SMALLINT DEFAULT 0,
        ticket DECIMAL(9,1) DEFAULT 0,
        base DECIMAL(9,2) DEFAULT 0 COMMENT '热度基数（检索排名折出来的）',
        effort VARCHAR(8) DEFAULT '' COMMENT '体力强度',
        effort_reason VARCHAR(160) DEFAULT '',
        is_meal TINYINT DEFAULT 0 COMMENT '1=餐饮，排景点时该剔掉',
        is_mall TINYINT DEFAULT 0 COMMENT '1=商场，不进景点池',
        closed_days VARCHAR(16) DEFAULT '' COMMENT '闭馆日，0=周一；空=每天都开（第 2 条）',
        ticket_source VARCHAR(12) DEFAULT 'estimated' COMMENT 'estimated=估算 / official=人工核过（第 26 条）',
        UNIQUE KEY uk_city_poi (city, poi_id),
        KEY idx_city (city),
        KEY idx_cat (city, category),
        KEY idx_kind (city, is_meal, is_mall)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS hotel (
        id INT AUTO_INCREMENT PRIMARY KEY,
        city VARCHAR(24) NOT NULL,
        name VARCHAR(200) NOT NULL,
        lng DOUBLE, lat DOUBLE,
        rating DECIMAL(3,1) DEFAULT 0,
        price DECIMAL(9,1) DEFAULT 0 COMMENT '基准价（估算）',
        level VARCHAR(8) DEFAULT '',
        km DECIMAL(6,1) DEFAULT 0 COMMENT '离默认住宿点多远',
        rooms JSON COMMENT '房型 [{type,beds,price}]',
        UNIQUE KEY uk_city_name (city, name),
        KEY idx_city (city)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    """CREATE TABLE IF NOT EXISTS travel_matrix (
        from_city VARCHAR(24) NOT NULL,
        from_id VARCHAR(24) NOT NULL,
        to_id VARCHAR(24) NOT NULL,
        km DECIMAL(8,2) DEFAULT 0,
        minutes DECIMAL(8,1) DEFAULT 0,
        PRIMARY KEY (from_city, from_id, to_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='高德实测驾车矩阵，目前只有长沙'""",

    """CREATE TABLE IF NOT EXISTS intercity (
        id INT AUTO_INCREMENT PRIMARY KEY,
        from_city VARCHAR(24) NOT NULL,
        to_city VARCHAR(24) NOT NULL,
        mode VARCHAR(8) NOT NULL COMMENT 'train/plane/drive',
        km DECIMAL(8,2) DEFAULT 0,
        minutes INT DEFAULT 0,
        price DECIMAL(9,1) DEFAULT 0,
        source VARCHAR(16) DEFAULT 'calibrated' COMMENT 'calibrated=人工校准',
        note VARCHAR(255) DEFAULT '',
        updated_at DATETIME,
        UNIQUE KEY uk_leg (from_city, to_city, mode)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='城际校准表；真实车次在 train_schedule'""",

    """CREATE TABLE IF NOT EXISTS climate (
        city VARCHAR(24) NOT NULL,
        month TINYINT NOT NULL,
        text VARCHAR(40) DEFAULT '',
        temp_high DECIMAL(4,1) DEFAULT NULL,
        temp_low DECIMAL(4,1) DEFAULT NULL,
        precip DECIMAL(4,1) DEFAULT NULL COMMENT '降水概率 %',
        PRIMARY KEY (city, month)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='超出预报范围用的历史同期气候平均（空表待填）'""",

    """CREATE TABLE IF NOT EXISTS holiday (
        day DATE PRIMARY KEY,
        name VARCHAR(40) DEFAULT '',
        is_holiday TINYINT DEFAULT 1
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='法定节假日（空表待填）'""",
]


def connect():
    cfg = json.loads((DATA / "db.json").read_text(encoding="utf-8"))
    if cfg.get("driver") != "mysql":
        raise SystemExit("data/db.json 里 driver 不是 mysql")
    return pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        charset="utf8mb4", autocommit=False,
    )


def kind_of(poi: dict) -> tuple[int, int]:
    # 餐饮是单独抓的（第 60 条）：foods.json 里来的直接算餐饮，不再靠关键词猜
    if poi.get("_is_meal"):
        return 1, 0
    text = f"{poi.get('category') or ''}{poi.get('name') or ''}"
    is_meal = 1 if any(k in text for k in MEAL_KEYS) else 0
    is_mall = 1 if any(k in text for k in MALL_KEYS) else 0
    return is_meal, is_mall


def load_city(city_dir: Path):
    pf = city_dir / "pois.json"
    if not pf.exists():
        # 只有 foods.json 没有 pois.json 的目录（第 60 条抓餐饮时建出来的）不参与入库：
        # 没有景点数据，进去也没意义，别让整个导入崩在这一步
        return None, None, {}
    pj = json.loads(pf.read_text(encoding="utf-8"))
    hj = json.loads((city_dir / "hotels.json").read_text(encoding="utf-8"))
    matrix = {}
    mf = city_dir / "matrix.json"
    if mf.exists():
        matrix = json.loads(mf.read_text(encoding="utf-8"))
    # 餐饮（第 60 条）：单独抓的 foods.json 并进候选池一起入库，
    # 但打上 _is_meal 标记，排景点时照旧剔掉，只有三餐推荐和选店用它
    ff = city_dir / "foods.json"
    if ff.exists():
        try:
            foods = json.loads(ff.read_text(encoding="utf-8")).get("pois") or []
        except ValueError:
            foods = []
        pool = pj.setdefault("pois", [])
        exist = {p.get("id") for p in pool}
        for f in foods:
            if f.get("id") in exist:
                continue
            g = dict(f)
            g["_is_meal"] = 1
            pool.append(g)
    return pj, hj, matrix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stat", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    dirs = sorted([d for d in CITIES.iterdir() if d.is_dir()])
    print(f"城市目录：{len(dirs)} 个")

    n_poi = n_hotel = n_meal = n_mall = 0
    cats = Counter()
    for d in dirs:
        pj, hj, _ = load_city(d)
        if pj is None:
            continue
        for p in pj.get("pois") or []:
            n_poi += 1
            cats[p.get("category") or "?"] += 1
            m, mall = kind_of(p)
            n_meal += m
            n_mall += mall
        n_hotel += len(hj.get("hotels") or [])

    print(f"景点 {n_poi} 条（其中餐饮 {n_meal}、商场 {n_mall}）｜酒店 {n_hotel} 条")
    print("分类分布前 15：", cats.most_common(15))
    if args.stat:
        return 0

    conn = connect()
    cur = conn.cursor()
    for ddl in DDL:
        cur.execute(ddl)
    # 老表可能还没有的列（第 2 条闭馆日 / 第 26 条票价来源），有就跳过
    for stmt in (
        "ALTER TABLE poi ADD COLUMN closed_days VARCHAR(16) NOT NULL DEFAULT '' "
        "COMMENT '闭馆日，0=周一；空=每天都开'",
        "ALTER TABLE poi ADD COLUMN ticket_source VARCHAR(12) NOT NULL DEFAULT 'estimated' "
        "COMMENT 'estimated=估算 / official=人工核过'",
    ):
        try:
            cur.execute(stmt)
        except pymysql.err.OperationalError:
            pass
    conn.commit()
    print("表建好了（7 张）")

    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 人工核过的门票（第 26 条）：有就覆盖，并标 official
    official = {}
    tf = DATA / "tickets_official.json"
    if tf.exists():
        try:
            official = (json.loads(tf.read_text(encoding="utf-8")).get("cities") or {})
        except ValueError:
            official = {}
    n_official = sum(len(v) for v in official.values())
    print(f"人工核过的门票：{n_official} 条（来自 tickets_official.json）")

    n_p = n_h = n_m = 0
    n_official_used = 0
    for d in dirs:
        pj, hj, matrix = load_city(d)
        if pj is None:
            continue
        city = pj.get("city") or d.name
        base_hotel = pj.get("hotel") or {}

        pois = pj.get("pois") or []
        hotels = hj.get("hotels") or []

        for p in pois:
            m, mall = kind_of(p)
            # 闭馆日（第 2 条）：入库时算好存下来，运行时直接读，不用每次现推
            closed = p.get("closed_days")
            if closed is None:
                closed = infer_closed_days(p.get("name") or "", p.get("category") or "")
            closed_str = ",".join(str(int(x)) for x in (closed or []))
            # 门票：人工核过的优先，标 official；其余保持 estimated
            ticket = float(p.get("ticket") or 0)
            tsrc = "estimated"
            hit = (official.get(city) or {}).get(p.get("name") or "")
            if isinstance(hit, dict) and hit.get("ticket") is not None:
                ticket = float(hit["ticket"])
                tsrc = "official"
            cur.execute(
                "INSERT INTO poi (city, poi_id, name, category, tags, lng, lat, "
                " open_time, close_time, visit_min, ticket, base, effort, effort_reason, "
                " is_meal, is_mall, closed_days, ticket_source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), category=VALUES(category), "
                " tags=VALUES(tags), lng=VALUES(lng), lat=VALUES(lat), "
                " open_time=VALUES(open_time), close_time=VALUES(close_time), "
                " visit_min=VALUES(visit_min), ticket=VALUES(ticket), base=VALUES(base), "
                " effort=VALUES(effort), effort_reason=VALUES(effort_reason), "
                " is_meal=VALUES(is_meal), is_mall=VALUES(is_mall), "
                " closed_days=VALUES(closed_days), ticket_source=VALUES(ticket_source)",
                (city, str(p.get("id") or ""), p.get("name") or "", p.get("category") or "",
                 json.dumps(p.get("tags") or [], ensure_ascii=False),
                 p.get("lng"), p.get("lat"),
                 (p.get("open") or "")[:5], (p.get("close") or "")[:5],
                 int(p.get("visit_min") or 0), ticket,
                 float(p.get("base") or 0), p.get("effort") or "",
                 p.get("effort_reason") or "", m, mall, closed_str, tsrc),
            )
            n_p += 1
            if tsrc == "official":
                n_official_used += 1

        for h in hotels:
            cur.execute(
                "INSERT INTO hotel (city, name, lng, lat, rating, price, level, km, rooms) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE lng=VALUES(lng), lat=VALUES(lat), "
                " rating=VALUES(rating), price=VALUES(price), level=VALUES(level), "
                " km=VALUES(km), rooms=VALUES(rooms)",
                (city, h.get("name") or "", h.get("lng"), h.get("lat"),
                 float(h.get("rating") or 0), float(h.get("price") or 0),
                 h.get("level") or "", float(h.get("km") or 0),
                 json.dumps(h.get("rooms") or [], ensure_ascii=False)),
            )
            n_h += 1

        for frm, dests in (matrix or {}).items():
            if not isinstance(dests, dict):
                continue
            for to, val in dests.items():
                if not isinstance(val, (list, tuple)) or len(val) < 2:
                    continue
                cur.execute(
                    "INSERT INTO travel_matrix (from_city, from_id, to_id, km, minutes) "
                    "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE km=VALUES(km), minutes=VALUES(minutes)",
                    (city, frm, to, float(val[0]), float(val[1])),
                )
                n_m += 1

        cur.execute(
            "INSERT INTO city (name, lng, lat, poi_count, hotel_count, has_matrix, "
            " datum, source, note, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE lng=VALUES(lng), lat=VALUES(lat), "
            " poi_count=VALUES(poi_count), hotel_count=VALUES(hotel_count), "
            " has_matrix=VALUES(has_matrix), datum=VALUES(datum), source=VALUES(source), "
            " note=VALUES(note), updated_at=VALUES(updated_at)",
            (city, base_hotel.get("lng"), base_hotel.get("lat"), len(pois), len(hotels),
             1 if matrix else 0, pj.get("_datum") or "", pj.get("_source") or "",
             (pj.get("_note") or "")[:255], now),
        )
        conn.commit()
        print(f"  {city:6s} 景点 {len(pois):3d}  酒店 {len(hotels):3d}  矩阵 {'有' if matrix else '无'}")

    # 收尾统计
    print(f"\n导入完成：poi {n_p}、hotel {n_h}、matrix {n_m} 条")
    for t in ("city", "poi", "hotel", "travel_matrix", "intercity", "climate", "holiday"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t:14s} {cur.fetchone()[0]:>6} 行")

    cur.execute("SELECT city, COUNT(*) FROM poi WHERE is_meal=1 GROUP BY city ORDER BY 2 DESC LIMIT 5")
    print("\n餐饮 POI 最多的城市：", cur.fetchall())
    cur.execute("SELECT city, COUNT(*) FROM poi WHERE is_mall=1 GROUP BY city ORDER BY 2 DESC LIMIT 5")
    print("商场 POI 最多的城市：", cur.fetchall())
    cur.execute("SELECT ticket_source, COUNT(*) FROM poi GROUP BY ticket_source")
    print("\n票价来源：", dict(cur.fetchall()), f"（本次覆盖 {n_official_used} 条 official）")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
