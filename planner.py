"""
planner —— 一次完整规划的编排：数据集 → 约束 → 求解 → 复核 → 页面 payload。

CLI（run_demo.py）和 HTTP 服务（server.py）共用这一份，避免两边逻辑漂移。

城市不写死：数据从 data/cities/<城市>/ 读，用哪个城市由调用方指定、或者从
自然语言里认出来（「北京3日游」）、或者落到索引里登记的默认城市。
"""
from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
from pathlib import Path

import avoid as avoid_mod
import cities
import nlparse
import optw_core as core
import retrieval
import weather as weather_mod

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DEFAULT_QUERY = "3日游，预算1500元，喜欢自然风光和历史文化，9点出发22点回"
RECALL_K = 20          # 检索层交给求解器的候选点数

TAGS = ["自然风光", "历史文化", "美食", "夜景", "亲子", "艺术", "网红打卡"]


class CityNotFound(ValueError):
    """请求的城市本地还没有数据（或者数据文件被挪走了）。"""


def resolve_city(city: str | None = None) -> str:
    """把用户给的城市名落成索引里真实存在的那个，没有就抛 CityNotFound。"""
    name = cities.canonical(city) if city else cities.default_city()
    if not name:
        raise CityNotFound("还没有任何城市的数据：先跑 python fetch_pois.py --city <城市名>")
    entry = cities.get(name)
    if entry is None:
        have = "、".join(c["name"] for c in cities.available()) or "无"
        raise CityNotFound(f"本地还没有「{name}」的数据（现有：{have}）。"
                           f"想加的话跑 python fetch_pois.py --city {name}")
    pois, _ = cities.paths(name)
    if pois is None:
        raise CityNotFound(f"「{name}」的数据文件不见了，重新跑一遍 fetch_pois.py --city {name}")
    return name


def pick_dataset(city: str | None = None, sample: bool = False):
    """返回 (候选点文件, 距离矩阵文件或 None)。sample 时优先用该城市的示例数据。"""
    if sample:                                          # 示例数据集是教学用的固定数据，跟真实城市解耦
        name = cities.canonical(city) if city else cities.default_city()
        if name:
            sp = cities.city_dir(name) / "sample.json"
            if sp.exists():
                return sp, None
        for other in cities.load()["cities"]:
            sp = cities.city_dir(other) / "sample.json"
            if sp.exists():
                return sp, None
        legacy = DATA / "poi_changsha.json"
        if legacy.exists():
            return legacy, None
        raise CityNotFound("找不到示例数据集 data/poi_changsha.json")

    name = resolve_city(city)
    pois, matrix = cities.paths(name)
    return pois, matrix


def meta_info(city: str | None = None, sample: bool = False) -> dict:
    """给前端的元信息：用哪份数据、有哪些城市、有什么标签、默认约束。"""
    data_path, matrix_path = pick_dataset(city, sample)
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    city_name, pois, hotel = core.load_dataset(str(data_path))
    return {
        "city": city_name,
        "default_city": cities.default_city(),
        "cities": cities.available(),
        "sample": bool(sample),
        "dataset": raw.get("_source", "sample"),
        "datum": raw.get("_datum", "wgs84"),
        "note": raw.get("_note", ""),
        "poi_count": len(pois),
        "hotel": {"name": hotel["name"], "lat": hotel["lat"], "lng": hotel["lng"]},
        "tags": TAGS,
        "travel_source": "amap" if (matrix_path and matrix_path.exists()) else "estimate",
        "defaults": {
            "days": 3,
            "budget": 1500,
            "day_start": "09:00",
            "day_end": "22:00",
            "interests": {t: 1.0 for t in TAGS},
        },
    }


def list_pois(city: str | None = None, sample: bool = False) -> list:
    """候选池清单（不含求解，候选页用）。"""
    data_path, _ = pick_dataset(city, sample)
    _, pois, _ = core.load_dataset(str(data_path))
    return [
        {
            "id": p.id, "name": p.name, "category": p.category, "tags": list(p.tags),
            "lat": p.lat, "lng": p.lng,
            "open": core.hhmm(p.open_min), "close": core.hhmm(p.close_min),
            "base": p.base, "ticket": p.ticket, "visit_min": p.visit_min,
        }
        for p in pois
    ]


def list_hotels(city: str | None = None) -> dict:
    """某城的候选旅馆清单（「自己挑住宿」用）。

    房型价是估出来的：高德免费接口既不返回房价也不返回房型，
    hotels.json 里的 rooms 是拿基准价乘系数生成的，这里如实标出来，
    免得前端显示得像真实报价。
    """
    name = resolve_city(city)
    rows = load_hotels(name)
    return {
        "city": name,
        "count": len(rows),
        "source": "estimated",
        "note": "房价与房型均为估算值（房型按基准价 ×1.00 / ×1.15 / ×1.50 生成），不是真实报价",
        "hotels": [
            {
                "name": h.get("name"), "level": h.get("level"),
                "price": h.get("price"), "rating": h.get("rating"),
                "km": h.get("km"), "lat": h.get("lat"), "lng": h.get("lng"),
                "rooms": h.get("rooms") or [],
            }
            for h in rows
        ],
    }


def load_hotels(city: str) -> list:
    """读这个城市的候选酒店；没有就返回空列表（退回"只有一个住宿点"的老模式）。"""
    path = cities.city_dir(city) / "hotels.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw.get("hotels") or []


def travel_for(pois, hotel: dict, matrix: dict | None):
    """给某家酒店建交通模型：景点之间的实测矩阵保留，酒店那两条边退回直线估算。"""
    coords = {p.id: (p.lat, p.lng) for p in pois}
    coords[core.Travel.HOTEL] = (float(hotel["lat"]), float(hotel["lng"]))
    m = None
    if matrix:
        H = core.Travel.HOTEL
        m = {a: {b: v for b, v in row.items() if a != H and b != H}
             for a, row in matrix.items() if a != H}
    return core.Travel(coords, m)


def pick_hotel_by_place(cons, pois, hotels, matrix, ils_rounds: int = 8):
    """
    对每家候选酒店各跑一遍完整行程，比总分（效用 − 交通 − 房费 + 评分），挑最高的。
    返回按分数从高到低排好的行，每行带 hotel / result / plan / score。
    """
    rows = []
    for h in hotels:
        c = copy.deepcopy(cons)
        c.hotel_lat = float(h["lat"])
        c.hotel_lng = float(h["lng"])
        c.hotel_name = h["name"]
        c.hotel_price = float(h.get("price") or 0.0)
        # 用户选了房型（room_beds > 0）就按这家酒店的对应房型价；没选就沿用基准价 + 2 人一间。
        if c.room_beds:
            pick = next((r for r in (h.get("rooms") or [])
                         if int(r.get("beds") or 0) == c.room_beds), None)
            c.room_price = float(pick["price"]) if pick else c.hotel_price
        # 第 69 条：把这家酒店的每档房型价装进去，房费按用户自己分配的那几间算
        c.room_prices = {int(r.get("beds") or 0): float(r.get("price") or 0.0)
                         for r in (h.get("rooms") or [])}
        plan_pick = c.planned_rooms
        if plan_pick:
            c.room_price = max((c.room_price_for(b) for b, _ in plan_pick),
                               default=c.hotel_price)
        travel = travel_for(pois, h, matrix)
        res = core.Solver(c, pois, travel).solve(ils_rounds=ils_rounds)
        res["travel_source"] = travel.source
        plan = res["local"]["plan"]
        rows.append({
            "hotel": h, "result": res, "plan": plan,
            "score": plan.full_score(float(h.get("rating") or 4.0)),
        })
    rows.sort(key=lambda x: -x["score"])
    return rows


def match_must(names, pois):
    """把用户说的景点名对到候选池里的点。返回 (ids, 标准名, 没找到的)。"""
    hits, missed = [], []
    for n in names:
        cand = [p for p in pois if n == p.name or n in p.name or p.name in n]
        if not cand:
            missed.append(n)
            continue
        cand.sort(key=lambda p: (p.name != n, len(p.name)))   # 全等最先，其次最具体的
        hits.append(cand[0])
    ids, names_ok, seen = [], [], set()
    for p in hits:
        if p.id in seen:
            continue
        seen.add(p.id)
        ids.append(p.id)
        names_ok.append(p.name)
    return ids, names_ok, missed


# ==================== 三餐推荐（第 24 条）====================
MEAL_KEYS = ("餐", "饭", "火锅", "小吃", "面馆", "菜", "食府", "茶", "咖啡", "烧烤", "酒家", "食堂")


def _g(poi, key):
    """Poi 对象或 dict 都取得到字段。"""
    if isinstance(poi, dict):
        return poi.get(key)
    return getattr(poi, key, None)


def is_meal(poi) -> bool:
    """是不是吃饭的地方：按分类 + 名字里带的关键词认，不追求精准。"""
    text = f"{_g(poi, 'category') or ''}{_g(poi, 'name') or ''}"
    return any(k in text for k in MEAL_KEYS)


def _km_between(a, b) -> float:
    import math
    return math.hypot(a[0] - b[0], a[1] - b[1]) * 111.0


def pick_meals(stops, pois, k: int = 3) -> dict:
    """
    给一天配午饭 / 晚饭：按「离中午所在那站最近」和「离最后一站最近」挑几家。

    只做推荐，不排进时间轴（时间轴里已经由中段休息占住了 11:30–13:30 那个窗口）。
    数据里餐厅不多时，宁可空着也不重复推同一家。
    """
    meals = [p for p in pois
             if is_meal(p) and _g(p, "lat") is not None and _g(p, "lng") is not None]
    if not meals or not stops:
        return {}

    def near(lat: float, lng: float) -> list:
        ranked = sorted(meals, key=lambda p: _km_between((lat, lng), (_g(p, "lat"), _g(p, "lng"))))[:k]
        return [{"name": _g(p, "name"), "category": _g(p, "category"),
                 "km": round(_km_between((lat, lng), (_g(p, "lat"), _g(p, "lng"))), 1)}
                for p in ranked]

    mid = stops[min(len(stops) - 1, len(stops) // 2)]
    last = stops[-1]
    lunch = near(mid.poi.lat, mid.poi.lng)
    dinner = near(last.poi.lat, last.poi.lng)

    # 午饭和晚饭别推同一家；只剩一家的话晚饭就不推了
    lname = {m["name"] for m in lunch}
    dinner = [m for m in dinner if m["name"] not in lname]
    return {"lunch": lunch, "dinner": dinner}


def plan_trip(query: str | None = None, *, city: str | None = None, days=None, budget=None,
              interests=None, day_start=None, day_end=None, sample=False, must_visit=None,
              start_date=None, avoid=None, max_stops=None, with_elderly=False, with_kids=False,
              adults=1, children=0, children_free=0, seniors=0, students=0, room_beds=0,
              food_per_day=None, city_mode=None, anchor_name="", anchor_day=0, anchor_before="",
              hotel_pick="", room_plan=None, rest_full=None, rest_half=None):
    """
    返回 (payload, result)：
      payload —— 给页面的完整数据（含两份计划、校验结果、POI 池、底图信息）
      result  —— 求解器原始结果（CLI 打印用）

    城市优先级：显式传的 city > 自然语言里认出来的 > 索引里的默认城市。
    """
    if query and query.strip():
        cons, notes = nlparse.parse(query)
    else:
        cons, notes = core.TripConstraints(days=3), ["按表单条件"]

    want = cities.canonical(city) if city else ""
    if not want and cons.city:
        want = cons.city
        if cities.get(want) is None and not sample:
            # query 里认出来一个小地方、但本地没数据：不硬报错，落回默认城市并说清楚
            notes.append(f"「{want}」本地还没有数据，这次先按 {cities.default_city()} 算")
            want = ""

    data_path, matrix_path = pick_dataset(want or None, sample)
    raw_meta = json.loads(data_path.read_text(encoding="utf-8"))
    city_name, pois, hotel = core.load_dataset(str(data_path))
    datum = raw_meta.get("_datum", "wgs84")
    warnings = []          # 给用户看的提醒：不是错误，但会影响行程质量
    wx_items = []          # 第 22 条：天气，求解前拿到，后面塞进 day 里展示

    # ---- 出行人数（第 27 条）：门票/餐饮按人头，住宿按房间数 ----
    cons.adults = max(1, int(adults))
    cons.children = max(0, int(children))
    cons.children_free = max(0, int(children_free))
    cons.seniors = max(0, int(seniors))
    cons.students = max(0, int(students))
    cons.room_beds = max(0, int(room_beds))
    # ---- 第 69 条：用户自己分配房型（几间大床 + 几间标间），空 = 还是按人数自动推 ----
    plan_pairs = []
    for item in (room_plan or []):
        try:
            if isinstance(item, dict):
                beds, cnt = int(item.get("beds") or 0), int(item.get("count") or 0)
            else:
                beds, cnt = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if beds in (1, 2, 3) and cnt > 0:
            plan_pairs.append((beds, min(cnt, 9)))
    cons.room_plan = tuple(plan_pairs)
    if cons.bed_short:
        warnings.append(
            f"你订的这几间一共 {cons.planned_beds} 个床位，{cons.travelers} 个人睡不下；"
            f"房费还是照你订的算，要不要加一间你自己定")
    # ---- 用户自己点名要住哪家（第 68 条）：空 = 让算法挑 ----
    cons.hotel_pick = (hotel_pick or "").strip()
    # ---- 餐标（第 61 条）：用户选的档位，不写死 80 ----
    if food_per_day:
        cons.food_per_day = max(0.0, float(food_per_day))
        notes.append(f"餐饮按 ¥{cons.food_per_day:.0f}/人/天 估")
    # ---- 市内交通方式（第 63 条）：自驾 / 打车 / 公交地铁 / 混合 ----
    if city_mode:
        cons.city_mode = str(city_mode).strip()
        _cm = {"drive": "自驾（油费按车算）", "taxi": "打车（按趟算）",
               "transit": "公交地铁（按人次算）"}.get(cons.city_mode)
        if _cm:
            notes.append(f"市内交通按{_cm}")
    if cons.travelers > 1:
        notes.append(f"出行 {cons.travelers} 人（成人 {cons.adults}"
                     f"{'、儿童 ' + str(cons.children) if cons.children else ''}"
                     f"{'、1.2m 以下 ' + str(cons.children_free) if cons.children_free else ''}"
                     f"{'、老人 ' + str(cons.seniors) if cons.seniors else ''}"
                     f"{'、学生 ' + str(cons.students) if cons.students else ''}）")

    # ---- 「这些我不想」：在候选池这一层把点剔掉，别等排完再一个个换 ----
    # 表单勾的和自然语言里认出来的合并，别各算一套
    avoid_tags = [str(k) for k in (avoid or ()) if str(k).strip()]
    for k in (cons.avoid or ()):
        if k not in avoid_tags:
            avoid_tags.append(k)
    if avoid_tags:
        pois, dropped = avoid_mod.filter_pois(pois, avoid_tags)
        if dropped:
            head = "、".join(dropped[:6]) + ("等" if len(dropped) > 6 else "")
            notes.append(f"按「不想要」剔掉 {len(dropped)} 个点：{head}")
        if not pois:
            raise CityNotFound("按你勾的「不想要」筛完，这个城市一个景点都不剩了，换个勾法试试")
    # ---- 一天几个点：用户说了算，系统不替他推断 ----
    if max_stops:
        cons.max_stops_per_day = max(1, min(8, int(max_stops)))
        notes.append(f"每天最多 {cons.max_stops_per_day} 个点")

    # ---- 带老人孩子：不压点数（那个用户自己定了），只把单点时长放大 ----
    # 注意别拿 max_visit_min_per_day 去压——那会变成「偷偷少排几个点」，
    # 用户明明说了每天三个，结果只给他排两个（2026-11-11 老大测出来的）。
    if with_elderly or with_kids:
        pois = [dataclasses.replace(p, visit_min=int(round(p.visit_min * 1.2))) for p in pois]
        who = ("老人孩子" if (with_elderly and with_kids)
               else "老人" if with_elderly else "孩子")
        notes.append(f"带{who}：单个景点按 1.2 倍时长算（走得慢、多歇一会儿），"
                     f"每天几个景点还是按你定的 {cons.max_stops_per_day} 个")

    if days:
        cons.days = max(1, min(14, int(days)))
        notes.append(f"天数 → {cons.days} 天")
    if start_date:
        # 有了出发日期才能算出每天周几：闭馆日和「第 1 天是几月几号」全靠它
        cons.start_date = str(start_date)[:10]
        try:
            d0 = dt.date.fromisoformat(cons.start_date)
        except ValueError:
            cons.start_date = ""
            notes.append(f"日期「{start_date}」没看懂，这次不按日期算")
        else:
            cons.day_weekdays = tuple((d0 + dt.timedelta(days=i)).weekday()
                                      for i in range(cons.days))
            d1 = d0 + dt.timedelta(days=cons.days - 1)
            notes.append(f"出发 {cons.start_date}（周{'一二三四五六日'[d0.weekday()]}），"
                         f"最后一天 {d1.isoformat()}")
    if budget:
        cons.budget = float(budget)
        notes.append(f"预算 → {cons.budget:.0f} 元")
    # 第 22 条：求解前先查天气——雨天户外点效用打折，博物馆/美术馆往前排
    if cons.start_date:
        try:
            _wx = weather_mod.daily(city_name, cons.start_date, cons.days)
            wx_items = _wx.get("items") or []
            cons.day_rainy = tuple(bool(it.get("rainy")) for it in wx_items)
            _rainy = [it.get("date") for it in wx_items if it.get("rainy")]
            if _rainy:
                notes.append(f"{city_name} 这几天有雨（{'、'.join(_rainy)}），"
                             f"雨天优先排室内，户外点往后放")
            elif not _wx.get("available") and _wx.get("reason"):
                notes.append(f"{city_name} 天气：{_wx['reason']}")
        except Exception as exc:      # noqa: BLE001 - 天气挂了不能拖垮规划
            notes.append(f"{city_name} 天气没取到：{exc}")
    if day_start:
        cons.day_start = core.parse_hhmm(day_start)
        notes.append(f"每天出发 → {day_start}")
    if day_end:
        cons.day_end = core.parse_hhmm(day_end)
        notes.append(f"每天收工 → {day_end}")
    # 第 25 条：休息日。值用「第几天」（1 起），越界的丢掉。
    def _day_idx(items):
        out = set()
        for x in items or ():
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= v <= cons.days:
                out.add(v - 1)
        return tuple(sorted(out))

    cons.rest_full = _day_idx(rest_full)
    cons.rest_half = tuple(i for i in _day_idx(rest_half) if i not in cons.rest_full)
    if cons.rest_full:
        notes.append("休息日：" + "、".join(f"第{i + 1}天" for i in cons.rest_full)
                     + "（睡到自然醒，不排景点）")
    if cons.rest_half:
        notes.append("半休：" + "、".join(f"第{i + 1}天" for i in cons.rest_half)
                     + "（13:00 以后出门）")
    if interests:
        base = {t: 0.55 for t in TAGS}
        base.update({k: float(v) for k, v in interests.items() if k in TAGS})
        cons.interests = base
        picked = [k for k, v in interests.items() if v > 0.6]
        notes.append("偏好 → " + ("、".join(picked) if picked else "均衡"))

    # ---- 时刻锚点（第 19 条）：先解析参数，稍后并进必去 ----
    anchor_name = (anchor_name or "").strip()
    anchor_before = (anchor_before or "").strip()
    anchor_day = int(anchor_day or 0)
    anchor_before_min = 0
    if anchor_before:
        try:
            _hh, _mm = anchor_before.split(":")
            anchor_before_min = int(_hh) * 60 + int(_mm)
        except ValueError:
            anchor_before_min = 0

    # ---- 必去景点：用户点名的必须进最终行程，进不去要说清楚为什么 ----
    want_must = [str(x).strip() for x in (must_visit or []) if str(x).strip()]
    if anchor_name and anchor_name not in want_must:
        want_must.append(anchor_name)      # 锚点一定得排进去，不然「几点前到」无从谈起
    if not want_must and cons.must_visit:
        want_must = list(cons.must_visit)
    if want_must:
        ids, names_ok, missed = match_must(want_must, pois)
        cons.must_ids = tuple(ids)
        cons.must_visit = tuple(names_ok)
        if names_ok:
            notes.append("必去景点 → " + "、".join(names_ok))
        if missed:
            notes.append("这些在候选池里没找到，先忽略：" + "、".join(missed))

    # ---- 剔完还剩多少、够不够排：宁可提前说，也别排出一份空荡荡的行程 ----
    need = max(1, cons.days) * max(1, cons.max_stops_per_day)
    if len(pois) < need:
        max_days = max(1, len(pois) // max(1, cons.max_stops_per_day))
        warnings.append(
            f"剔完只剩 {len(pois)} 个可选景点：按每天 {cons.max_stops_per_day} 个、{cons.days} 天算"
            f"要 {need} 个，差 {need - len(pois)} 个，最多只排得满 {max_days} 天。"
            f"要么少勾几项「不想要」，要么把天数降到 {max_days} 天。")
    elif len(pois) < need * 1.2:
        warnings.append(f"可选景点只剩 {len(pois)} 个：排得下，但替代方案少，行程会比较紧。")

    cons.city = city_name
    cons.hotel_lat = float(hotel["lat"])
    cons.hotel_lng = float(hotel["lng"])
    cons.hotel_name = hotel["name"]

    matrix = None
    if matrix_path and matrix_path.exists():
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    coords = {p.id: (p.lat, p.lng) for p in pois}
    coords[core.Travel.HOTEL] = (cons.hotel_lat, cons.hotel_lng)
    travel = core.Travel(coords, matrix)

    # ---- M3：有自然语言就走检索，只把最相关的 K 个点交给求解器 ----
    recall = None
    used_pois = pois
    if query and query.strip():
        recall = retrieval.Retriever(pois).rank(query, cons.interests, k=RECALL_K)
        keep = set(recall["kept_ids"])
        used_pois = [p for p in pois if p.id in keep] or pois
        # 必去点不能被召回挡在门外
        if cons.must_ids:
            have = {p.id for p in used_pois}
            for pid in cons.must_ids:
                if pid not in have:
                    used_pois.append(pool[pid])
        notes.append(f"语义召回 {len(used_pois)}/{len(pois)} 个候选点")
        if recall["expanded"]:
            notes.append("查询扩展 → " + "、".join(recall["expanded"]))

    # ---- 住宿是算出来的，不是输入的：每家候选酒店各跑一遍，比总分 ----
    hotels = [] if sample else load_hotels(city_name)
    hotel_rows = []
    hotel_pick_info = None
    room_desc = ""
    if hotels:
        rows = pick_hotel_by_place(cons, used_pois, hotels, matrix)
        best = rows[0]
        algo_best = rows[0]
        # ---- 第 68 条：用户自己点了某一家就住那家，行程跟着那家的坐标重排 ----
        if cons.hotel_pick:
            hit = next((r for r in rows if r["hotel"]["name"] == cons.hotel_pick), None)
            if hit is not None:
                best = hit
            else:
                warnings.append(f"没找到旅馆「{cons.hotel_pick}」，这次还是按算法挑的那家来")
        h = best["hotel"]
        cons.hotel_lat = float(h["lat"])
        cons.hotel_lng = float(h["lng"])
        cons.hotel_name = h["name"]
        cons.hotel_price = float(h.get("price") or 0.0)
        if cons.room_beds:
            pick = next((r for r in (h.get("rooms") or [])
                         if int(r.get("beds") or 0) == cons.room_beds), None)
            cons.room_price = float(pick["price"]) if pick else cons.hotel_price
        # 第 69 条：这家的每档房型价单独装好，房费按用户自己分配的那几间算
        cons.room_prices = {int(r.get("beds") or 0): float(r.get("price") or 0.0)
                            for r in (h.get("rooms") or [])}
        if cons.planned_rooms:
            cons.room_price = max((cons.room_price_for(b) for b, _ in cons.planned_rooms),
                                  default=cons.hotel_price)
        result = best["result"]
        room_bit = ""
        if cons.planned_rooms:
            bits = []
            for beds, cnt in cons.planned_rooms:
                label = {1: "大床房", 2: "标准间", 3: "家庭房"}.get(beds, f"{beds}床房")
                bits.append(f"{label}×{cnt}（¥{cons.room_price_for(beds):.0f}）")
            room_bit = "，" + " + ".join(bits) + f"，合计 ¥{sum(cons.room_price_for(b) * c for b, c in cons.planned_rooms):.0f}/晚"
        elif cons.room_beds:
            room_bit = f"，{cons.beds_per_room} 床 × {cons.rooms_needed} 间（¥{cons.night_price:.0f}/晚）"
        if cons.planned_rooms:
            label_map = {1: "大床房", 2: "标准间", 3: "家庭房"}
            room_desc = " + ".join(
                f"{label_map.get(b, f'{b}床房')} × {c} 间（¥{cons.room_price_for(b):.0f}/间/晚）"
                for b, c in cons.planned_rooms)
            room_desc += (f" = ¥{sum(cons.room_price_for(b) * c for b, c in cons.planned_rooms):.0f}/晚"
                          f" × {cons.hotel_nights} 晚")
        chosen_bit = "，你点的" if best is not algo_best else ""
        notes.append(f"住宿 → {h['name']}（{h['level']}，¥{h['price']:.0f}/晚{room_bit}，评分 {h['rating']}，{h['km']} km{chosen_bit}）")
        hotel_rows = [
            {"name": r["hotel"]["name"], "level": r["hotel"]["level"],
             "price": r["hotel"]["price"], "rating": r["hotel"]["rating"],
             "km": r["hotel"]["km"], "score": round(r["score"], 1),
             "rooms": r["hotel"].get("rooms") or [],
             "picked": r is best}
            for r in rows
        ]
        # 第 68 条：用户自己点的那家 vs 算法首选，差多少分、排第几，留给结果页做对比
        if best is not algo_best:
            hotel_pick_info = {
                "picked": best["hotel"]["name"],
                "picked_score": round(best["score"], 1),
                "best": algo_best["hotel"]["name"],
                "best_score": round(algo_best["score"], 1),
                "rank": next((i + 1 for i, r in enumerate(rows) if r is best), None),
                "total": len(rows),
                "gap": round(algo_best["score"] - best["score"], 1),
                "price_gap": round(float(algo_best["hotel"].get("price") or 0)
                                   - float(best["hotel"].get("price") or 0), 1),
                "km_gap": round(float(algo_best["hotel"].get("km") or 0)
                                - float(best["hotel"].get("km") or 0), 1),
            }
    else:
        solver = core.Solver(cons, used_pois, travel)
        result = solver.solve()
        result["travel_source"] = travel.source

    pool = {p.id: p for p in pois}
    for key in ("greedy", "local"):
        result[key]["verify"] = core.verify(result[key]["plan"], cons, pool)

    # 同一个条件可能被说两遍（自然语言 + 表单），只留最后生效的那条，别再出现
    # 「预算 → 2000」紧跟着「预算 → 1500」这种自相矛盾
    latest = {}
    for n in notes:
        latest[n.split(" → ")[0] if " → " in n else n] = n
    notes = list(latest.values())

    payload = {
        "meta": {
            "city": city_name,
            "query": query or "",
            "notes": notes,
            "cons": cons.summary(),
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "datum": datum,
            "dataset": raw_meta.get("_source", "sample"),
            "travel_source": travel.source,
            "lambda_travel": core.Plan.LAMBDA_TRAVEL,
            "evals": {"greedy": result["greedy"]["evals"], "total": result["total_evals"]},
            "data_note": raw_meta.get("_note", "示例数据"),
            "recall_k": RECALL_K,
            "pool_size": len(pois),
            "solved_size": len(used_pois),
            "warnings": warnings,
            "hotels": hotel_rows,
            "hotel_pick": hotel_pick_info,
            "room_desc": room_desc,
        },
        "hotel": {"name": cons.hotel_name, "lat": cons.hotel_lat, "lng": cons.hotel_lng},
        "pois": [
            {
                "id": p.id, "name": p.name, "category": p.category, "tags": list(p.tags),
                "lat": p.lat, "lng": p.lng,
                "open": core.hhmm(p.open_min), "close": core.hhmm(p.close_min),
                "base": p.base, "ticket": p.ticket, "visit_min": p.visit_min,
            }
            for p in pois
        ],
        "recall": recall,
        "variants": [
            {
                "key": key,
                "label": label,
                "plan": result[key]["plan"].to_dict(),
                "verify": result[key]["verify"],
            }
            for key, label in (
                ("greedy", "策略 A · 效用密度贪心"),
                ("local", "策略 B · 贪心 + 局部搜索"),
            )
        ],
    }
    # 第 19 条：时刻锚点核一遍——到底排进去没有、到得早不早
    if anchor_name and anchor_before_min:
        hit = None
        for d in result["local"]["plan"].days:
            for s in d.stops:
                if anchor_name in s.poi.name:
                    hit = (d.index + 1, int(round(s.arrive)))
                    break
            if hit:
                break
        if hit is None:
            notes.insert(0, f"「{anchor_name}」没能排进任何一天，所以 {anchor_before} 前到不了")
        else:
            day_no, arrive_min = hit
            if anchor_day and day_no != anchor_day:
                notes.insert(0, f"「{anchor_name}」排在第 {day_no} 天，不是你说的第 {anchor_day} 天")
            elif arrive_min <= anchor_before_min:
                notes.insert(0, f"「{anchor_name}」第 {day_no} 天 {core.hhmm(arrive_min)} 到，赶得上 {anchor_before}")
            else:
                notes.insert(0, f"「{anchor_name}」第 {day_no} 天最快 {core.hhmm(arrive_min)} 到，"
                                f"赶不上你说的 {anchor_before}")

    # 第 24 条：每天配午饭 / 晚饭推荐（不进时间轴，只在当天卡片上列出来）
    # 餐饮已经不在景点池里了（第 4 条清洗），所以单独从库里取一份
    try:
        meal_pool = core.load_meals(want) or []
        if not meal_pool:
            meal_pool = [p for p in pois if is_meal(p)]
        for v in payload["variants"]:
            plan_obj = result[v["key"]]["plan"]
            for d, day_plan in zip(v["plan"]["days"], plan_obj.days):
                meals = pick_meals(day_plan.stops, meal_pool)
                if meals:
                    d["meals"] = meals
    except (KeyError, AttributeError, TypeError):
        pass

    # 第 22 条：把天气塞进每天的 day，页面上要显示；雨天那天的排程已经在求解时让过路了
    if wx_items:
        by_date = {it.get("date"): it for it in wx_items}
        for v in payload["variants"]:
            for d in v["plan"]["days"]:
                d["weather"] = by_date.get(d.get("date"))

    # 第 43 条：预算跟方案对不上时得说出来——超了要说超多少，快用完要说还剩什么没排进去。
    # 2026-11-11：从 notes 挪到 warnings——notes 只在「算法细节」里，用户看不到；
    # 预算这件事是他最该当场知道的，得在结果页顶上黄条里。
    try:
        best_plan = result["local"]["plan"]
        if cons.budget and cons.budget > 0:
            spent = best_plan.total_cost
            # 第 67-7 条：预算含机动，判断要用「规划 + 机动」
            pocket = best_plan.pocket
            grand = spent + pocket
            chosen = {s.poi.id for s in best_plan.stops}
            pool_ids = {p.id for p in used_pois}
            paid_missed = [p for p in pois
                           if p.id in pool_ids and p.id not in chosen and (p.ticket or 0) > 0]
            if grand > cons.budget:
                if not best_plan.stops:
                    # 一个点都排不出来，说明光住宿 + 餐饮 + 机动就超了，劝他放宽预算比「少点景点」有用
                    warnings.append(
                        f"这点预算排不了景点：光住宿 ¥{best_plan.hotel_cost:.0f}"
                        f" + 餐饮 ¥{best_plan.food:.0f}"
                        f" + 机动 ¥{pocket:.0f} = ¥{grand:.0f}，"
                        f"已经超了你给的 ¥{cons.budget:.0f}——至少加到约 ¥{grand:.0f} 才谈得上排点")
                else:
                    warnings.append(
                        f"这个方案规划 ¥{spent:.0f} + 机动 ¥{pocket:.0f} = ¥{grand:.0f}，"
                        f"超出你给的预算 ¥{cons.budget:.0f} 共 ¥{grand - cons.budget:.0f}——"
                        f"要么放宽预算，要么少排一天、少点收费景点")
            elif grand >= 0.9 * cons.budget and paid_missed:
                # 把被挤掉那几家的门票加起来，估一个「提到多少大概能排进去」
                extra = sum((p.ticket or 0) for p in paid_missed) * cons.ticket_factor
                need = grand + extra
                head = "、".join(f"{p.name} ¥{p.ticket:.0f}" for p in paid_missed[:3])
                if len(paid_missed) > 3:
                    head += " 等"
                warnings.append(
                    f"预算偏紧：规划 ¥{spent:.0f} + 机动 ¥{pocket:.0f} 已经用掉 ¥{grand:.0f}"
                    f"（{grand / cons.budget:.0%}），"
                    f"候选里 {len(paid_missed)} 个收费景点没排进去（{head}）；"
                    f"预算提到约 ¥{need:.0f} 就能把它们排进来")
    except (KeyError, AttributeError, TypeError):
        pass

    # 第 22 条：雨天到底换掉了几成户外点，要当场说出来。
    # 下雨那天户外点效用打两折（RAIN_OUTDOOR_FACTOR=0.2），求解器自然会往室内靠；
    # 但「必去的」「附近实在没有室内替代」的还是会排进来，这时候不能装作没下雨。
    try:
        if any(cons.day_rainy):
            best = result["local"]["plan"]
            for d in best.days:
                idx = d.index
                if idx >= len(cons.day_rainy) or not cons.day_rainy[idx]:
                    continue
                out_n = [s.poi.name for s in d.stops if core.is_outdoor(s.poi)]
                if not d.stops:
                    warnings.append(f"第 {idx + 1} 天有雨，这天没排景点——正好空出来")
                elif not out_n:
                    warnings.append(f"第 {idx + 1} 天有雨，{len(d.stops)} 个点全换成室内了")
                else:
                    warnings.append(
                        f"第 {idx + 1} 天有雨，{len(out_n)} 个户外点没避开（"
                        + "、".join(out_n[:3]) + ("等" if len(out_n) > 3 else "")
                        + "）——要么是必去的，要么附近没有室内可换；实在不想淋雨就把它们点掉")
    except (KeyError, AttributeError, TypeError, IndexError):
        pass

    return payload, result


def constraints_from_payload(payload: dict):
    """把 payload.meta.cons 还原成 TripConstraints（编辑重排时用）。"""
    c = payload.get("meta", {}).get("cons", {})
    cons = core.TripConstraints(days=int(c.get("days", 3)))
    cons.budget = float(c.get("budget", 1500))
    cons.day_start = core.parse_hhmm(c.get("day_start", "09:00"))
    cons.day_end = core.parse_hhmm(c.get("day_end", "22:00"))
    cons.interests = dict(c.get("interests") or {})
    cons.food_per_day = float(c.get("food_per_day", 80))
    # 第 67-1 条：这三项是 11-11 新加的，编辑重排时不还原就等于「改一次顺序，设置全打回默认」
    cons.pocket_per_day = float(c.get("pocket_per_day", 25))
    cons.city_mode = str(c.get("city_mode") or "mixed")
    cons.day_rainy = tuple(bool(x) for x in (c.get("day_rainy") or ()))
    # 第 25 条：休息日同样要还原，不然编辑一次顺序，那天又排满点了
    cons.rest_full = tuple(int(x) for x in (c.get("rest_full") or ()))
    cons.rest_half = tuple(int(x) for x in (c.get("rest_half") or ()))
    cons.km_cost = float(c.get("km_cost", core.KM_COST))
    cons.city = c.get("city") or (payload.get("meta") or {}).get("city") or ""
    cons.max_stops_per_day = int(c.get("max_stops_per_day", 5))
    cons.max_visit_min_per_day = int(c.get("max_visit_min_per_day", 480))
    cons.must_ids = tuple(c.get("must_ids") or ())
    cons.hotel_price = float(c.get("hotel_price") or 0.0)
    # 人数和房型（第 27 条）：编辑重排时也得还原，不然票价房费会打回单人口径
    party = c.get("party") or {}
    cons.adults = int(party.get("adults", 1))
    cons.children = int(party.get("children", 0))
    cons.children_free = int(party.get("children_free", 0))
    cons.seniors = int(party.get("seniors", 0))
    cons.students = int(party.get("students", 0))
    cons.room_beds = int(c.get("room_beds") or 0)
    cons.room_price = float(c.get("room_price") or 0.0)
    # 第 69 条：用户当初自己分配的房型，编辑重排时照旧，别退回自动推
    cons.room_plan = tuple(
        (int(x.get("beds") or 0), int(x.get("count") or 0))
        for x in (c.get("room_plan") or []) if int(x.get("count") or 0) > 0)
    cons.must_visit = tuple(c.get("must_visit") or ())
    hotel = payload.get("hotel") or {}
    cons.hotel_lat = float(hotel.get("lat") or 0.0)
    cons.hotel_lng = float(hotel.get("lng") or 0.0)
    cons.hotel_name = hotel.get("name") or c.get("hotel") or "住宿点"
    # 第 68 条：当初用户自己点的那家，编辑重排时还得算数，不能被算法首选顶掉
    cons.hotel_pick = str(((payload.get("meta") or {}).get("hotel_pick") or {}).get("picked") or "")
    # 日期也跟着还原：编辑重排时每天还得知道自己是周几、会不会撞闭馆
    cons.start_date = str(c.get("start_date") or "")
    if cons.start_date:
        try:
            d0 = dt.date.fromisoformat(cons.start_date)
            cons.day_weekdays = tuple((d0 + dt.timedelta(days=i)).weekday()
                                      for i in range(cons.days))
        except ValueError:
            cons.start_date = ""
    return cons


def rebuild(payload: dict, day_lists: list):
    """
    按用户给定的逐日顺序重排（行程编辑用），返回新的 payload。
    任意一天排不出来就抛 ValueError，把原因说清楚。
    """
    cons = constraints_from_payload(payload)
    pois = [core.Poi.from_dict(x) for x in payload.get("pois", [])]
    if not pois:
        raise ValueError("这份方案里没有候选池数据，改不了")
    pool = {p.id: p for p in pois}

    matrix = None
    if payload.get("meta", {}).get("travel_source") == "amap":
        _, matrix_path = cities.paths(cons.city)
        if matrix_path and matrix_path.exists():
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    # 第 67-8 条：别直接拿整个矩阵——里面的 `__hotel__` 那两行是**建矩阵时那家酒店**的边，
    # 换酒店就不对了。原求解走的是 `travel_for()`，它会把酒店相关的边剔掉、退回直线估算，
    # 这里必须用同一套；否则「同一个顺序」重排时会算出不一样的路程（长沙差 3 分钟，
    # 直接让第 2 天超了国画馆 18:00 的关门）。
    travel = travel_for(pois, {"lat": cons.hotel_lat, "lng": cons.hotel_lng}, matrix)

    days = []
    for i, ids in enumerate(day_lists):
        ids = [x for x in ids if x in pool]
        d = core.build_day(ids, cons, pool, travel, index=i)
        if d is None:
            names = "、".join(pool[x].name for x in ids[:3]) or "空"
            raise ValueError(f"第 {i + 1} 天这个顺序走不通（{names}…）：会撞上闭馆时间或者超过收工时间")
        for s in d.stops:                          # build_day 不填效用，跟 Solver.day 一样补上
            # 第 22 条：重排也要按那天的天气算，不然编辑一下雨天就白算了
            rainy = cons.day_rainy[i] if i < len(cons.day_rainy) else False
            s.utility = core.utility(s.poi, cons.interests, cons.must_ids, rainy=rainy)
        days.append(d)

    plan = core.Plan(days=days, cons=cons)
    verify = core.verify(plan, cons, pool)

    out = json.loads(json.dumps(payload, ensure_ascii=False))   # 深拷贝，不动原 payload
    out["meta"]["generated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out["meta"]["edited"] = True
    out["meta"]["notes"] = list(out["meta"].get("notes") or []) + ["手动调整过顺序，已重新校验"]
    for v in out.get("variants", []):
        if v.get("key") == "local":
            v["plan"] = plan.to_dict()
            v["verify"] = verify
            v["label"] = (v.get("label") or "策略 B") + "（手动调整）"
    out["verify_local"] = verify
    return out
