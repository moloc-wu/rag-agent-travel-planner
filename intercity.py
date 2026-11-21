"""
城际交通估算：把「出发地 → 目的地」的距离变成耗时和票价。

设计原则（来自需求文档 F4）：
  1. 出行方式由用户逐段选，本模块**只算，不选**，也不做推荐。
  2. 三层数据源：本地公式打底 → 手工校准表覆盖 → 第三方 API（暂不接）。
     任何一层出问题都退回公式，功能不许崩。
  3. 自驾只算时间，不算钱 —— 车型不同油耗差太多，估出来是假精确。

三种方式的估算口径：
  高铁  距离 / 280 km/h + 50 分钟（进站、候车、出站）
        二等座 0.45 元/km
  飞机  距离 / 800 km/h + 150 分钟（往返机场、安检、取行李）
        0.7 元/km + 机建燃油 100 元
  自驾  距离 / 90 km/h（高速均速），油费过路费不计
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
GEO_PATH = DATA_DIR / "city_geo.json"

MODES = ("train", "plane", "drive")
MODE_LABEL = {"train": "高铁", "plane": "飞机", "drive": "自驾"}

# ---- 公式参数（集中放这里，调参不用翻全文） ----
TRAIN_SPEED = 280.0        # km/h
TRAIN_BUFFER_MIN = 50      # 进站 + 候车 + 出站
TRAIN_PRICE_PER_KM = 0.45

PLANE_SPEED = 800.0
PLANE_BUFFER_MIN = 150     # 往返机场 + 安检 + 取行李
PLANE_PRICE_PER_KM = 0.7
PLANE_FIXED_FEE = 100.0    # 机建 + 燃油

DRIVE_SPEED = 90.0         # 高速均速
DRIVE_LONG_MIN = 8 * 60    # 单车程超过 8 小时要拆两天

# 短途城际票价明显高于线性估算（比如长沙→武汉 350 km 要 165 元，
# 线性只算 157），用这个下限兜一下
TRAIN_MIN_PRICE = 20.0
PLANE_MIN_PRICE = 250.0


@dataclass
class IntercityLeg:
    """一段城际交通。"""
    from_city: str
    to_city: str
    mode: str
    km: float
    minutes: int
    price: float
    source: str = "formula"       # formula / schedule（真实车次表）/ calibrated
    note: str = ""
    # 真实车次（第 53 条）：source == "schedule" 时这几项有值
    train_code: str = ""          # 选中的那班车次号，如 G318
    dep_time: str = ""            # HH:MM
    arr_time: str = ""            # HH:MM
    train_date: str = ""          # 这个时刻表属于哪一天
    trains: list = field(default_factory=list)   # 候选车次（按用时排好的前几班）
    prices: dict = field(default_factory=dict)   # {席别: 成人单价}

    @property
    def mode_label(self) -> str:
        return MODE_LABEL.get(self.mode, self.mode)

    @property
    def is_long_drive(self) -> bool:
        return self.mode == "drive" and self.minutes >= DRIVE_LONG_MIN

    def to_dict(self) -> dict:
        return {
            "from": self.from_city,
            "to": self.to_city,
            "mode": self.mode,
            "mode_label": self.mode_label,
            "km": round(self.km, 1),
            "minutes": int(self.minutes),
            "duration": f"{self.minutes // 60} 小时 {self.minutes % 60:02d} 分"
                        if self.minutes >= 60 else f"{self.minutes} 分钟",
            "price": round(self.price, 1),
            "source": self.source,
            "long_drive": self.is_long_drive,
            "note": self.note,
            "train_code": self.train_code,
            "dep_time": self.dep_time,
            "arr_time": self.arr_time,
            "train_date": self.train_date,
            "trains": self.trains,
            "prices": self.prices,
        }


def _geo() -> dict:
    try:
        raw = json.loads(GEO_PATH.read_text(encoding="utf-8"))
        return raw.get("cities") or {}
    except Exception:
        return {}


_GEO_CACHE = None


def _geo_data() -> dict:
    global _GEO_CACHE
    if _GEO_CACHE is None:
        try:
            raw = json.loads(GEO_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        _GEO_CACHE = {
            "cities": raw.get("cities") or {},
            "districts": raw.get("districts") or {},
        }
    return _GEO_CACHE


def resolve_city(name: str):
    """
    把用户填的地名解析成「用来算城际交通的城市」和坐标。

    区县没有独立的高铁枢纽，所以填「天河」按「广州」算：
      1. 先查市级表
      2. 查不到就查区县表，用它的上级市
    返回 (city_name, coord) 或 (name, None)。
    """
    name = (name or "").strip()
    if not name:
        return name, None
    data = _geo_data()
    hit = data["cities"].get(name)
    if hit:
        return name, (float(hit[0]), float(hit[1]))
    parent = data["districts"].get(name)
    if parent:
        coord = data["cities"].get(parent)
        if coord:
            return parent, (float(coord[0]), float(coord[1]))
    return name, None


def city_coord(name: str):
    """城市的中心坐标 (lat, lng)；区县会归到上级市。查不到返回 None。"""
    return resolve_city(name)[1]


def haversine_km(a: tuple, b: tuple) -> float:
    """两点球面距离（km）。"""
    lat1, lng1 = a
    lat2, lng2 = b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


SEAT_NAMES = {"9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
              "6": "高级软卧", "4": "软卧", "3": "硬卧", "2": "软座",
              "1": "硬座", "F": "动卧"}
# 报价时优先用哪一档（行程规划按最常说走的那档算）
SEAT_PRIORITY = ("二等座", "硬座", "无座", "一等座", "硬卧", "软卧", "商务座")


def decode_price(raw: str) -> dict:
    """解 12306 的票价串，形如 `9176900010M105700021O058400021O058403066`。

    规则：席别代号 1 位 + 价格 4 位（单位元）+ 5 位附加，一组 10 位。
    已在 2026-11-10 用官方 queryTicketPrice 接口交叉验证过（G502 商务 1769 / 一等 1057 / 二等 584）。
    """
    out = {}
    i = 0
    while i + 4 < len(raw):
        seat = raw[i]
        price = raw[i + 1:i + 5]
        if not price.isdigit():
            i += 1
            continue
        name = SEAT_NAMES.get(seat)
        if name and name not in out and int(price) > 0:
            out[name] = int(price)
        i += 10
    return out


def _db():
    """读 data/db.json 连 MySQL；连不上返回 None（不许因为数据库挂了就排不了行程）。"""
    try:
        import pymysql
        cfg = json.loads((DATA_DIR / "db.json").read_text(encoding="utf-8"))
        if cfg.get("driver") != "mysql":
            return None
        return pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset="utf8mb4", connect_timeout=4,
        )
    except Exception:      # noqa: BLE001 - 数据库不可用就退回公式
        return None


def lookup_schedule(from_city: str, to_city: str, limit: int = 5):
    """
    查 train_schedule 表里这两个城市之间的真实车次（第 53 条）。

    返回 [{train_code, dep_time, arr_time, duration_min, next_day, prices}]，
    按用时从短到长；查不到返回 []。
    """
    conn = _db()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT train_code, dep_time, arr_time, duration_min, next_day, "
                "       price_raw, query_date "
                "FROM train_schedule "
                "WHERE from_city=%s AND to_city=%s AND duration_min > 0 "
                # 12306 对「当天订不到 / 不可售」的车次会返回 24:00 和 99:59，这类是脏数据要滤掉
                "  AND duration_min <= 2880 "
                "  AND dep_time <> '24:00' AND arr_time <> '24:00' "
                "ORDER BY duration_min ASC LIMIT %s",
                (from_city, to_city, int(limit)),
            )
            rows = cur.fetchall()
    except Exception:      # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    out = []
    for code, dep, arr, dur, next_day, price_raw, qdate in rows:
        out.append({
            "train_code": code,
            "dep_time": (dep or "").strip(),
            "arr_time": (arr or "").strip(),
            "duration_min": int(dur),
            "next_day": int(next_day or 0),
            "depart_date": str(qdate),
            "prices": decode_price(price_raw or ""),
        })
    return out


def pick_price(prices: dict) -> float:
    """从各席别里挑一个代表价：优先二等座。"""
    for name in SEAT_PRIORITY:
        if prices.get(name):
            return float(prices[name])
    return 0.0


def _dep_minutes(hhmm: str) -> int:
    """'07:08' → 428。格式不对返回 -1。"""
    try:
        hh, mm = (hhmm or "").strip().split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return -1


# 出发时段偏好（第 56 条）：用户想坐早班还是晚班，别由系统定成 08:00
DEPART_WINDOWS = {
    "morning":   (5 * 60, 10 * 60),      # 早班 05:00–10:00
    "forenoon":  (9 * 60, 12 * 60 + 30),  # 上午 09:00–12:30
    "afternoon": (12 * 60, 18 * 60),     # 下午 12:00–18:00
    "evening":   (17 * 60, 24 * 60),     # 晚班 17:00–24:00
}
DEPART_WINDOW_LABEL = {
    "morning": "早班（5–10 点）",
    "forenoon": "上午（9–12 点半）",
    "afternoon": "下午（12–18 点）",
    "evening": "晚班（17 点后）",
    "": "不指定",
}


def pick_train(trains: list, prefer: str = "") -> tuple[dict, str]:
    """从候选车次里挑一班：按用户偏好时段筛，筛完为空就退回全部。

    挑的规则是「到得最早」而不是「用时最短」——第 56 条：到达时刻直接决定
    第一天还能不能玩，用户关心的是几点到，不是车上坐多久。
    返回 (车次, 说明)。说明用于备注，让用户知道「为什么是这班」。
    """
    if not trains:
        return {}, ""

    def by_arrival(t):
        arr = _dep_minutes(t.get("arr_time"))
        return (int(t.get("next_day") or 0), arr if arr >= 0 else 24 * 60)

    win = DEPART_WINDOWS.get((prefer or "").strip())
    if not win:
        return sorted(trains, key=by_arrival)[0], ""
    hit = [t for t in trains if win[0] <= _dep_minutes(t.get("dep_time")) <= win[1]]
    if hit:
        return (sorted(hit, key=by_arrival)[0],
                f"按你选的{DEPART_WINDOW_LABEL.get(prefer, prefer)}挑的")
    return (sorted(trains, key=by_arrival)[0],
            f"你要的时段（{DEPART_WINDOW_LABEL.get(prefer, prefer)}）没有直达车，改用了当天到得最早的一班")


def estimate(from_city: str, to_city: str, mode: str = "train",
             depart_prefer: str = "") -> IntercityLeg:
    """
    估算一段城际交通。查不到城市坐标会抛 ValueError，由上层给出人话提示。
    """
    mode = (mode or "train").strip()
    if mode not in MODES:
        raise ValueError(f"不认识的出行方式：{mode}（只支持 train / plane / drive）")

    a_city, a = resolve_city(from_city)
    b_city, b = resolve_city(to_city)
    if a is None:
        raise ValueError(f"没有「{from_city}」的坐标，城际交通算不了。往 data/city_geo.json 里加一行就行")
    if b is None:
        raise ValueError(f"没有「{to_city}」的坐标，城际交通算不了。往 data/city_geo.json 里加一行就行")

    # 填的是区县时，按上级市算，并在备注里说清楚
    alias_note = ""
    if a_city != from_city.strip():
        alias_note = f"「{from_city}」按 {a_city} 算"
    if b_city != to_city.strip():
        alias_note = (alias_note + "；" if alias_note else "") + f"「{to_city}」按 {b_city} 算"

    km = haversine_km(a, b)

    # 高铁：先查真实车次表（第 53 条），查得到就用真时刻和真票价，查不到才退回公式
    if mode == "train":
        trains = lookup_schedule(a_city, b_city)
        if trains:
            best, pick_note = pick_train(trains, depart_prefer)
            price = pick_price(best.get("prices") or {})
            if price <= 0:
                price = max(TRAIN_MIN_PRICE, km * 1.2 * TRAIN_PRICE_PER_KM)
            note = (f"{best['train_code']} {best['dep_time']}–{best['arr_time']}"
                    f"（{best['depart_date']} 12306 实查）")
            if len(trains) > 1:
                note += f"，当天有 {len(trains)} 班可选"
            if pick_note:
                note += "；" + pick_note
            if alias_note:
                note = alias_note + "；" + note
            return IntercityLeg(
                from_city=from_city.strip(),
                to_city=to_city.strip(),
                mode="train",
                km=round(km * 1.2, 1),
                minutes=int(best["duration_min"]),
                price=round(price, 1),
                source="schedule",
                note=note,
                train_code=best["train_code"],
                dep_time=best["dep_time"],
                arr_time=best["arr_time"],
                train_date=best["depart_date"],
                trains=trains,
                prices=best.get("prices") or {},
            )

    # 实际铁路/公路里程比直线长，按 1.2 折算（民航航线约 1.05）
    if mode == "train":
        road_km = km * 1.2
        minutes = int(round(road_km / TRAIN_SPEED * 60 + TRAIN_BUFFER_MIN))
        price = max(TRAIN_MIN_PRICE, road_km * TRAIN_PRICE_PER_KM)
        note = ""
    elif mode == "plane":
        air_km = km * 1.05
        minutes = int(round(air_km / PLANE_SPEED * 60 + PLANE_BUFFER_MIN))
        price = max(PLANE_MIN_PRICE, air_km * PLANE_PRICE_PER_KM + PLANE_FIXED_FEE)
        note = ""
    else:  # drive
        road_km = km * 1.2
        minutes = int(round(road_km / DRIVE_SPEED * 60))
        price = 0.0
        note = "油费过路费未计入（视车型而定）"

    if mode == "drive" and minutes >= DRIVE_LONG_MIN:
        note = (note + "；" if note else "") + f"单程 {minutes // 60} 小时，建议拆两天、标「在路上」"

    if alias_note:
        note = alias_note + ("；" + note if note else "")

    return IntercityLeg(
        from_city=from_city.strip(),
        to_city=to_city.strip(),
        mode=mode,
        km=road_km if mode != "plane" else air_km,
        minutes=minutes,
        price=price,
        source="formula",
        note=note,
    )


def plan_legs(depart_from: str, stops: list, home_return: bool = True,
              depart_prefer: str = "") -> list:
    """
    按行程顺序把城际段串起来。
    stops: [(城市, 天数, 出行方式), ...]
    depart_prefer: 出发时段偏好（第 56 条），早班 / 上午 / 下午 / 晚班，空 = 不指定
    返回：[出去 → 段间 → 回程] 的 IntercityLeg 列表
    """
    legs = []
    if not stops:
        return legs
    first_city, _, first_mode = stops[0]
    legs.append(estimate(depart_from, first_city, first_mode, depart_prefer))
    for i in range(1, len(stops)):
        prev_city = stops[i - 1][0]
        city, _, mode = stops[i]
        legs.append(estimate(prev_city, city, mode, depart_prefer))
    if home_return:
        last_city, _, last_mode = stops[-1]
        # 回程默认跟去程同一类方式，除非调用方另给
        legs.append(estimate(last_city, depart_from, last_mode, depart_prefer))
    return legs
