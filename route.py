"""
环线行程规划（需求文档 F2.1 / F2.2 / F2.3）。

和老的单城 `planner.plan_trip()` 的区别：
  · 行程是「出发地 → 段1 → 段2 → … → 回出发地」，不是在一个城市里排几天
  · 每天有自己的时间窗：第 1 天从到达算、最后一天到返程截止
  · 出行方式由用户逐段指定，本模块只负责按选定的方式排，不替用户选

设计约束：
  · 每段内部仍然复用单城的求解器（同一套 build_day / verify），不另起一套
  · 段内住一家酒店（住宿按段决策）
  · 城际交通只算耗时和钱，不参与景点效用
"""
from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import avoid as avoid_mod
import cities
import intercity
import optw_core as core
import planner
import weather as weather_mod

DEFAULT_DEPART_TIME = "08:00"     # 默认早上 8 点出发
LAST_DAY_BUFFER_MIN = 50          # 最后一天赶车留的进站缓冲
ARRIVE_SETTLE_MIN = 60            # 到站后：出站 + 去酒店 + 放行李办入住（老大 11-11 指出这段没算）
# 车站 → 酒店这段路程（第 57 条）：不再用 ARRIVE_SETTLE_MIN 拍死，
# 按「车站坐标 → 酒店坐标」的实际距离估。20 分钟是出站、等车、办入住的固定开销。
SETTLE_FIXED_MIN = 20
SETTLE_KMH = 25                   # 市区里打车/地铁的平均速度，别按高速算
SETTLE_MIN_MIN = 30               # 再近也不会瞬间到
SETTLE_MAX_MIN = 120              # 再远也顶多两小时，超过说明数据有问题
ON_ROAD_MIN = 4 * 60              # （保留）返程特别长时整段算在路上的参考线
MIN_LAST_DAY_MIN = 3 * 60         # 返程那天扣完还剩不到 3 小时，就算整天在路上，别排碎窗口
# 到得太晚（超过这个点）当天就不排点了，只登记住宿
LATE_ARRIVE_MIN = 19 * 60


def station_coord(city: str):
    """读 data/cities/<城市>/stations.json 里主车站的坐标（GCJ-02）。没有就返回 None。"""
    path = Path(__file__).resolve().parent / "data" / "cities" / city / "stations.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stations = raw.get("stations") or []
    if not stations:
        return None
    st = stations[0]
    try:
        return (float(st["lat"]), float(st["lng"])), st.get("name") or ""
    except (KeyError, TypeError, ValueError):
        return None


def settle_minutes(city: str, hotel_lat: float, hotel_lng: float) -> tuple[int, str]:
    """到站之后多久才能开始玩：出站 + 车站到酒店 + 办入住。

    返回 (分钟, 说明)。没有车站坐标或酒店坐标时退回老的 60 分钟，并说明原因（第 57 条）。
    """
    hit = station_coord(city)
    if hit is None:
        return ARRIVE_SETTLE_MIN, f"没查到 {city} 的车站坐标，先按 {ARRIVE_SETTLE_MIN} 分钟估"
    (st_lat, st_lng), st_name = hit
    if not hotel_lat or not hotel_lng:
        return ARRIVE_SETTLE_MIN, f"没拿到酒店坐标，先按 {ARRIVE_SETTLE_MIN} 分钟估"
    km = haversine_km((st_lat, st_lng), (hotel_lat, hotel_lng)) * 1.35   # 路网绕行
    mins = SETTLE_FIXED_MIN + km / SETTLE_KMH * 60
    mins = int(round(max(SETTLE_MIN_MIN, min(SETTLE_MAX_MIN, mins))))
    return mins, f"{st_name} → 酒店约 {km:.1f} km，出站+路上+入住按 {mins} 分钟算"


def haversine_km(a: tuple, b: tuple) -> float:
    """两点球面距离（km）。a/b 都是 (lat, lng)。"""
    r = 6371.0
    la1, ln1, la2, ln2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((ln2 - ln1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(d))


@dataclass
class Segment:
    city: str
    days: int
    transport: str = "train"

    def to_dict(self) -> dict:
        return {"city": self.city, "days": self.days, "transport": self.transport}


def _date_iter(start_date: str | None, total_days: int):
    """从出发日期开始，返回每天的 (ISO 日期, 周几)。没给日期就只给序号。"""
    if not start_date:
        return [(None, None)] * total_days
    try:
        d0 = datetime.date.fromisoformat(str(start_date)[:10])
    except ValueError:
        return [(None, None)] * total_days
    week = "一二三四五六日"
    out = []
    for i in range(total_days):
        d = d0 + datetime.timedelta(days=i)
        out.append((d.isoformat(), "周" + week[d.weekday()]))
    return out


def _segment_windows(days: int, arrive_min: int, is_last: bool, return_min: int,
                     day_start: int, day_end: int,
                     settle_min: int = ARRIVE_SETTLE_MIN, user_start_min: int = 0):
    """
    给一段里的每一天算可用时间窗。
      · 段内第 1 天：从到达时间起算（太晚就直接空着）
          - settle_min  = 出站 + 车站到酒店 + 办入住，第 57 条改成按距离估
          - user_start_min = 用户自己说「到达当天几点开始玩」，给了就按他说的（更晚才生效）
      · 中间天：全天
      · 整趟的最后一天：要赶回程——
          - 先按「收工 − 返程耗时 − 进站缓冲」算出还能玩到几点
          - 扣完不足 MIN_LAST_DAY_MIN（3 小时）才整天算在路上，免得排出碎窗口
    返回 (windows, 最后一天是否在路上)
    """
    wins = []
    on_road_last = False
    for i in range(days):
        start, end = day_start, day_end
        if i == 0:
            # 到站 ≠ 能开始玩：还得出站、去酒店、放行李 / 办入住，这一段也得给时间
            start = max(day_start, arrive_min + settle_min)
            if user_start_min:
                start = max(start, user_start_min)
            if start > LATE_ARRIVE_MIN:
                # 到得太晚：这天不排点，只落个住宿
                wins.append((LATE_ARRIVE_MIN, LATE_ARRIVE_MIN))
                continue
        if is_last and i == days - 1:
            # 返程那天：收工时间 − 返程耗时 − 进站缓冲 = 还能玩到几点
            end = min(end, day_end - return_min - LAST_DAY_BUFFER_MIN)
            if end - start < MIN_LAST_DAY_MIN:
                # 扣完只剩一小段碎窗口，排出来看着像坏了，不如整天算在路上
                on_road_last = True
                wins.append((LATE_ARRIVE_MIN, LATE_ARRIVE_MIN))
                continue
        wins.append((start, end))
    return wins, on_road_last


def _solve_segment(city: str, days: int, windows: list, budget: float, interests: dict | None,
                   must_visit: list | None, sample: bool, weekdays: list | None = None,
                   ils_rounds: int = 8, avoid: list | None = None, max_stops: int | None = None,
                   with_elderly: bool = False, with_kids: bool = False,
                   adults: int = 1, children: int = 0, children_free: int = 0, seniors: int = 0,
                   students: int = 0, room_beds: int = 0, nights: int = 0,
                   food_per_day: float | None = None, city_mode: str = "",
                   day_rainy: tuple | None = None):
    """单段求解：选酒店 + 跑求解器 + 校验。返回 (payload 片段, notes)。"""
    notes = []
    data_path, matrix_path = planner.pick_dataset(city, sample)
    city_name, pois, hotel = core.load_dataset(str(data_path))

    # 「这些我不想」和「带老人孩子」在每段的候选池上先落一遍，后面求解才干净
    if avoid:
        pois, dropped = avoid_mod.filter_pois(pois, avoid)
        if dropped:
            notes.append(f"{city_name}：按「不想要」剔掉 {len(dropped)} 个点")
        if not pois:
            raise ValueError(f"{city_name} 按你勾的「不想要」筛完一个景点都不剩，这一段排不了")
    if with_elderly or with_kids:
        pois = [dataclasses.replace(p, visit_min=int(round(p.visit_min * 1.2))) for p in pois]

    cons = core.TripConstraints(days=days)
    cons.budget = float(budget)
    cons.city = city_name
    # 出行人数（第 27 条）：每段都带上，段内门票/餐饮/房费才按人头算
    cons.adults = max(1, int(adults))
    cons.children = max(0, int(children))
    cons.children_free = max(0, int(children_free))
    cons.seniors = max(0, int(seniors))
    cons.students = max(0, int(students))
    cons.room_beds = max(0, int(room_beds))
    # 住宿晚数：非最后一段当天到了就要住，住满 days 晚；最后一段返程那天不住。
    cons.nights = max(0, int(nights))
    cons.day_windows = tuple(windows)
    cons.day_weekdays = tuple(weekdays or ())
    cons.day_city = tuple([city_name] * days)
    # 餐标（第 61 条）：用户选的档位，不写死 80
    if food_per_day:
        cons.food_per_day = max(0.0, float(food_per_day))
    # 市内交通方式（第 63 条）
    if city_mode:
        cons.city_mode = str(city_mode).strip()
    # 哪天下雨（第 22 条）：雨天户外点效用打折，室内点照排
    if day_rainy:
        cons.day_rainy = tuple(bool(x) for x in day_rainy)
    if max_stops:
        cons.max_stops_per_day = max(1, min(8, int(max_stops)))
    # 带老人孩子：单点时长 ×1.2 已经在上面做过了，这里不要再动 max_visit_min_per_day
    # （压它会变成偷偷少排点，2026-11-11 老大测出来）

    if interests:
        base = {t: 0.55 for t in planner.TAGS}
        base.update({k: float(v) for k, v in interests.items() if k in planner.TAGS})
        cons.interests = base

    # 必去：只认这一段的城市里有的点
    want = [str(x).strip() for x in (must_visit or []) if str(x).strip()]
    if want:
        ids, names_ok, missed = planner.match_must(want, pois)
        cons.must_ids = tuple(ids)
        cons.must_visit = tuple(names_ok)
        if missed:
            notes.append(f"{city_name}：这些必去点没找到，先忽略（{'、'.join(missed)}）")
        if names_ok:
            notes.append(f"{city_name} 必去 → {'、'.join(names_ok)}")

    matrix = None
    if matrix_path and matrix_path.exists():
        import json as _json
        matrix = _json.loads(matrix_path.read_text(encoding="utf-8"))

    hotels = [] if sample else planner.load_hotels(city_name)
    hotel_rows = []
    if hotels:
        rows = planner.pick_hotel_by_place(cons, pois, hotels, matrix, ils_rounds=ils_rounds)
        best = rows[0]
        h = best["hotel"]
        cons.hotel_lat = float(h["lat"])
        cons.hotel_lng = float(h["lng"])
        cons.hotel_name = h["name"]
        cons.hotel_price = float(h.get("price") or 0.0)
        if cons.room_beds:
            pick = next((r for r in (h.get("rooms") or [])
                         if int(r.get("beds") or 0) == cons.room_beds), None)
            cons.room_price = float(pick["price"]) if pick else cons.hotel_price
        result = best["result"]
        hotel_rows = [
            {"name": r["hotel"]["name"], "level": r["hotel"]["level"],
             "price": r["hotel"]["price"], "rating": r["hotel"]["rating"],
             "km": r["hotel"]["km"], "score": round(r["score"], 1),
             "picked": r is best}
            for r in rows
        ]
        room_bit = ""
        if cons.room_beds:
            room_bit = f"，{cons.beds_per_room} 床 × {cons.rooms_needed} 间（¥{cons.night_price:.0f}/晚）"
        notes.append(f"{city_name} 住宿 → {h['name']}（{h['level']}，¥{h['price']:.0f}/晚{room_bit}）")
    else:
        travel = planner.travel_for(pois, hotel, matrix)
        result = core.Solver(cons, pois, travel).solve(ils_rounds=ils_rounds)
        result["travel_source"] = travel.source
        cons.hotel_name = hotel.get("name", "住宿点")
        cons.hotel_lat = float(hotel.get("lat") or 0)
        cons.hotel_lng = float(hotel.get("lng") or 0)

    pool = {p.id: p for p in pois}
    for key in ("greedy", "local"):
        result[key]["verify"] = core.verify(result[key]["plan"], cons, pool)

    return {
        "city": city_name,
        "cons": cons,
        "result": result,
        "hotel_rows": hotel_rows,
        "hotel": {"name": cons.hotel_name, "lat": cons.hotel_lat, "lng": cons.hotel_lng,
                  "price": cons.hotel_price},
        "pois": pois,
    }, notes


def plan_route(depart_from: str, segments: list, *, start_date=None, budget=None,
               interests=None, must_visit=None, sample=False,
               day_start: str = "09:00", day_end: str = "21:30",
               depart_time: str = DEFAULT_DEPART_TIME,
               avoid=None, max_stops=None, with_elderly: bool = False, with_kids: bool = False,
               adults: int = 1, children: int = 0, children_free: int = 0, seniors: int = 0,
               students: int = 0, room_beds: int = 0, food_per_day: float | None = None,
               arrive_start_at: str = "", depart_prefer: str = "", city_mode: str = "",
               anchor_name: str = "", anchor_day: int = 0, anchor_before: str = ""):
    """
    环线规划主入口。

    depart_from : 出发地城市名
    segments    : [Segment] 或 [{city, days, transport}]
    start_date  : "YYYY-MM-DD"，给了就能算出每天是周几

    返回 (payload, result)：payload 给页面用，result 给命令行/测试用。
    """
    segs = []
    for s in segments or []:
        if isinstance(s, Segment):
            segs.append(s)
        else:
            segs.append(Segment(str(s.get("city", "")).strip(),
                                int(s.get("days") or 1),
                                str(s.get("transport") or "train").strip()))
    if not segs:
        raise ValueError("至少要有一段行程")
    if any(not s.city for s in segs):
        raise ValueError("有段没填城市")
    if any(s.days < 1 for s in segs):
        raise ValueError("每段至少 1 天")

    notes = []
    ds = core.parse_hhmm(day_start)
    de = core.parse_hhmm(day_end)
    # 出发时刻（第 56 条）：有真实车次就用车次时刻（下面逐段取），没有才按这个估。
    # 用户选了出发时段偏好，公式估算也跟着偏好走，别一律 08:00。
    _PREFER_DEPART = {"morning": "07:30", "forenoon": "10:00",
                      "afternoon": "14:00", "evening": "18:00"}
    dt0 = core.parse_hhmm(_PREFER_DEPART.get((depart_prefer or "").strip(), depart_time))

    # ---- 1) 城际交通 ----
    legs = intercity.plan_legs(
        depart_from,
        [(s.city, s.days, s.transport) for s in segs],
        home_return=True,
        depart_prefer=depart_prefer,
    )
    intercity_cost = sum(l.price for l in legs)
    # 城际交通按人头折算（第 54 条）：成人 / 老人全价，儿童半价，学生 75 折，未满 6 岁不占座免票。
    # 自驾段 price = 0（只算时间不算钱），乘了也不影响。
    _ticket_factor = max(0.0, adults + seniors + 0.5 * children + 0.75 * students)
    intercity_cost *= _ticket_factor

    def _leg_dict(l):
        """城际一段的展示口径（第 62 条）：单人价 + 折算后的这一段总价。

        legs[].price 是单人成人价，总账扣的是乘了人群系数的钱，两者不标清就对不上。
        """
        d = l.to_dict()
        d["price_total"] = round(l.price * _ticket_factor, 1)
        d["ticket_factor"] = round(_ticket_factor, 2)
        return d
    # 人数和房间数（第 27 条）：整趟各段一致
    _travelers = max(1, int(adults) + int(children) + int(children_free)
                     + int(seniors) + int(students))
    _rooms = -(-_travelers // max(1, int(room_beds) or 2))
    # 回程也按最后一段的方式算，但回程方式用户可以希望不同 —— 这里先跟去程一致
    if budget:
        remain = max(0.0, float(budget) - intercity_cost)
        notes.append(f"总预算 ¥{budget:.0f} − 城际交通 ¥{intercity_cost:.0f} = 各段可用 ¥{remain:.0f}")
    else:
        remain = None

    total_days = sum(s.days for s in segs)
    dates = _date_iter(start_date, total_days)

    # 每天是周几（0 = 周一）：没有出发日期就算不出来，闭馆日也就不判
    weekdays_all = []
    if start_date:
        try:
            d0 = datetime.date.fromisoformat(str(start_date)[:10])
            weekdays_all = [(d0 + datetime.timedelta(days=i)).weekday() for i in range(total_days)]
        except ValueError:
            weekdays_all = []

    # ---- 时刻锚点（第 19 条，环线版）：先算出「整趟第 N 天」落在哪一段 ----
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
    anchor_seg = -1
    if anchor_name:
        if anchor_day > 0:
            _acc = 0
            for _si, _s in enumerate(segs):
                if _acc < anchor_day <= _acc + _s.days:
                    anchor_seg = _si
                    break
                _acc += _s.days
        else:
            anchor_seg = 0      # 没说第几天就放进第一段

    # ---- 2) 逐段求解 ----
    seg_out = []
    seg_results = []
    day_cursor = 0
    # 用户指定「到达当天几点开始玩」（第 57 条）：留空就按「到站 + 安顿」估
    arrive_start_min = core.parse_hhmm(arrive_start_at) if arrive_start_at else 0

    for si, s in enumerate(segs):
        arrive_leg = legs[si] if si < len(legs) else None
        is_last = si == len(segs) - 1
        return_leg = legs[-1] if len(legs) == len(segs) + 1 else None
        ret_min = return_leg.minutes if (is_last and return_leg) else 0

        arrive_min = dt0 + (arrive_leg.minutes if arrive_leg else 0)
        # 第 56 条：这一段有真实车次就按车次的真实到站时间排，别拿「08:00 + 车程」硬磨
        if arrive_leg and arrive_leg.dep_time and arrive_leg.arr_time:
            _dep_min = core.parse_hhmm(arrive_leg.dep_time)
            _arr_min = core.parse_hhmm(arrive_leg.arr_time)
            if _dep_min and _arr_min:
                arrive_min = _arr_min + (24 * 60 if _arr_min < _dep_min else 0)
        wins, on_road = _segment_windows(s.days, arrive_min, is_last, ret_min, ds, de,
                                         user_start_min=arrive_start_min)

        seg_budget = (remain * s.days / total_days) if remain is not None else 3000.0
        seg_weekdays = weekdays_all[day_cursor: day_cursor + s.days]
        # 锚点所在的那一段，把目标点并进必去（其它段不重复排）
        seg_must = list(must_visit or [])
        if si == anchor_seg and anchor_name and anchor_name not in seg_must:
            seg_must.append(anchor_name)

        # 第 22 条：先查天气——求解前就知道哪天下雨，雨天把户外点往后排、室内点往前排
        wx_items, wx_err = [], ""
        try:
            seg_start = dates[day_cursor][0] if day_cursor < len(dates) else None
            wx = weather_mod.daily(s.city, seg_start, s.days)
            wx_items = wx.get("items") or []
            if not wx.get("available") and wx.get("reason"):
                wx_err = wx["reason"]
        except Exception as exc:      # noqa: BLE001 - 天气挂了不能让行程挂
            wx_err = f"{s.city} 天气没取到：{exc}"
        rainy_flags = tuple(bool(it.get("rainy")) for it in wx_items)

        def _run(windows):
            return _solve_segment(
                s.city, s.days, windows, seg_budget, interests, seg_must, sample,
                weekdays=seg_weekdays, avoid=avoid, max_stops=max_stops,
                with_elderly=with_elderly, with_kids=with_kids,
                adults=adults, children=children, children_free=children_free,
                seniors=seniors, students=students, room_beds=room_beds,
                nights=(s.days - 1 if is_last else s.days),
                food_per_day=food_per_day, city_mode=city_mode,
                day_rainy=rainy_flags)

        seg_payload, seg_notes = _run(wins)

        # 第 57 条：酒店定下来才知道「车站 → 酒店」有多远，安顿时间按距离重算；变了就重排这一段
        # 第 67-4 条：重排可能选到**另一家**酒店，那安顿时间又得跟着改。所以循环到稳定，
        # 最多再校正两轮（再抖下去说明这个城市酒店差异太大，追不动了，记一笔就行）。
        settle, settle_note = ARRIVE_SETTLE_MIN, ""
        for _round in range(3):
            _h = seg_payload.get("hotel") or {}
            settle, settle_note = settle_minutes(s.city, float(_h.get("lat") or 0),
                                                 float(_h.get("lng") or 0))
            wins2, on_road2 = _segment_windows(s.days, arrive_min, is_last, ret_min, ds, de,
                                               settle_min=settle,
                                               user_start_min=arrive_start_min)
            if wins2 == wins:
                break
            wins, on_road = wins2, on_road2
            seg_payload, seg_notes = _run(wins)
        else:
            notes.append(f"{s.city} 换酒店后安顿时间来回变，最后按 {settle} 分钟算（{settle_note}）")
        notes.extend(seg_notes)

        if on_road and return_leg:
            notes.append(f"{s.city} 最后一天整天在路上（返程 {return_leg.minutes // 60} 小时 {return_leg.minutes % 60:02d} 分），不排景点")
        elif si == 0 and arrive_leg:
            notes.append(
                f"到 {s.city} 是 {core.hhmm(arrive_min)}，{settle_note}；"
                f"{core.hhmm(wins[0][0])} 才开始排景点")
        elif is_last and return_leg and wins:
            _end = wins[-1][1]
            notes.append(
                f"{s.city} 最后一天玩到 {_end // 60:02d}:{_end % 60:02d} 收工，"
                f"然后赶返程（{return_leg.minutes // 60} 小时 {return_leg.minutes % 60:02d} 分）"
            )

        plan_local = seg_payload["result"]["local"]["plan"]
        verify = seg_payload["result"]["local"]["verify"]

        days_out = []
        # 天气（第 22 条）：上面求解前已经查过一遍，这里直接复用，不再请求第二次
        wx_map = {it.get("date"): it for it in wx_items}
        if wx_err:
            notes.append(f"{s.city} 天气：{wx_err}")

        # 餐饮单独取（第 4 条清洗后它不在景点池里了），一段取一次
        meal_pool = core.load_meals(seg_payload["city"]) or []
        for di, d in enumerate(plan_local.days):
            w0, w1 = wins[di] if di < len(wins) else (ds, de)
            date, week = dates[day_cursor + di] if day_cursor + di < len(dates) else (None, None)
            meals = planner.pick_meals(d.stops, meal_pool)
            days_out.append({
                "index": day_cursor + di + 1,
                "day_in_segment": di + 1,
                "date": date,
                "weekday": week,
                "window": f"{core.hhmm(w0)}–{core.hhmm(w1)}",
                "window_minutes": max(0, w1 - w0),
                "on_road": bool(on_road and di == s.days - 1),
                "on_road_note": (f"整天在路上（返程 {return_leg.minutes // 60} 小时 {return_leg.minutes % 60:02d} 分）"
                                 if (on_road and di == s.days - 1 and return_leg) else ""),
                # 第 57 条：到了之后为什么到现在才开玩，直接写在当天卡片上
                # 第 56 条：晚班到得太晚时，这天就是不排点，也得说清是「太晚」而不是「坏了」
                "arrive_note": (
                    (f"到站 {core.hhmm(arrive_min)}，{settle_note}" if (w1 - w0) > 0
                     else f"到站 {core.hhmm(arrive_min)}，到得太晚，这天不排点")
                    if di == 0 and arrive_leg else ""),
                "weather": wx_map.get(date),
                "meals": meals,
                "plan": _day_dict(d),
            })
        day_cursor += s.days
        seg_results.append(seg_payload)

        seg_out.append({
            "index": si,
            "city": seg_payload["city"],
            "from": depart_from if si == 0 else segs[si - 1].city,
            "transport": s.transport,
            "transport_label": intercity.MODE_LABEL.get(s.transport, s.transport),
            "arrive_leg": arrive_leg.to_dict() if arrive_leg else None,
            "days": days_out,
            "hotel": seg_payload["hotel"],
            "hotel_rows": seg_payload["hotel_rows"],
            "totals": plan_local.to_dict()["totals"],
            "verify": verify,
            "travel_source": seg_payload["result"].get("travel_source", "estimate"),
            "cons": seg_payload["cons"].summary(),
        })

    # ---- 3) 汇总 ----
    total_stops = sum(seg["totals"]["stops"] for seg in seg_out)
    total_cost = sum(seg["totals"]["total_cost"] for seg in seg_out) + intercity_cost
    total_utility = sum(seg["totals"]["utility"] for seg in seg_out)
    total_km = sum(seg["totals"]["km"] for seg in seg_out) + sum(l.km for l in legs)
    total_travel_min = sum(seg["totals"]["travel_min"] for seg in seg_out) + sum(l.minutes for l in legs)

    drive_legs = [l for l in legs if l.mode == "drive"]
    checks = []
    checks.append({
        "name": "总预算",
        "ok": (total_cost <= float(budget)) if budget else True,
        "detail": (f"含城际 ¥{intercity_cost:.0f}，合计 ¥{total_cost:.0f}"
                   + (f" / 预算 ¥{budget:.0f}" if budget else "")
                   + ("（自驾段油费过路费未计入）" if drive_legs else "")),
    })
    checks.append({
        "name": "城际衔接",
        "ok": True,
        "detail": "、".join(f"{l.from_city}→{l.to_city} {l.mode_label} {l.minutes // 60}h{l.minutes % 60:02d}m"
                          for l in legs) or "无",
    })
    seg_ok = sum(1 for seg in seg_out if seg["verify"]["passed"] == seg["verify"]["total"])
    checks.append({
        "name": "各段约束",
        "ok": seg_ok == len(seg_out),
        "detail": f"{seg_ok}/{len(seg_out)} 段全部通过（各段明细见下）",
    })
    passed = sum(1 for c in checks if c["ok"])

    # ---- 4) 给前端一个「整趟行程」的合并视图（复用老结果页的表格组件）----
    variants = []
    for key, label in (("greedy", "策略 A · 效用密度贪心"),
                       ("local", "策略 B · 贪心 + 局部搜索")):
        flat, tot = [], {
            "stops": 0, "utility": 0.0, "ticket": 0.0, "food": 0.0,
            "transport": 0.0, "hotel": 0.0, "pocket": 0.0, "total_cost": 0.0,
            "budget": float(budget) if budget else 0.0,
            "km": 0.0, "travel_min": 0, "score": 0.0,
        }
        for si, seg in enumerate(seg_out):
            res = seg_results[si]["result"][key]
            sub = res["plan"].to_dict()
            for k in ("stops", "travel_min"):
                tot[k] += sub["totals"].get(k, 0)
            for k in ("utility", "ticket", "food", "transport", "hotel",
                      "pocket", "total_cost", "km", "score"):
                tot[k] += float(sub["totals"].get(k, 0.0))
            for di, d in enumerate(sub["days"]):
                info = seg["days"][di]
                flat.append({
                    "index": info["index"],
                    "date": info["date"],
                    "weekday": info["weekday"],
                    "window": info["window"],
                    "window_minutes": info.get("window_minutes"),
                    "segment_city": seg["city"],
                    "total_km": d["total_km"],
                    "travel_min": d["travel_min"],
                    "stops": d["stops"],
                    # 第 59 条：吃饭那段时间，前端时间轴要显示
                    "rests": d.get("rests") or [],
                    # 以下三项原来只塞在 segments 里，前端按 variants[0].plan 渲染时看不到
                    "weather": info.get("weather"),
                    "meals": info.get("meals"),
                    "on_road": info.get("on_road"),
                    "on_road_note": info.get("on_road_note"),
                    "arrive_note": info.get("arrive_note"),
                })
        tot["total_cost"] += intercity_cost
        tot["intercity_cost"] = round(intercity_cost, 1)
        tot["grand_total"] = round(tot["total_cost"] + tot["pocket"], 1)
        tot["travelers"] = _travelers
        tot["rooms"] = _rooms
        tot["per_person"] = round(tot["total_cost"] / _travelers, 1)
        tot = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in tot.items()}
        variants.append({
            "key": key,
            "label": label,
            "plan": {"days": flat, "totals": tot},
            "verify": {
                "checks": checks,
                "passed": passed,
                "total": len(checks),
                "pass_rate": round(passed / len(checks) * 100, 1),
            },
        })

    # ---- 5) 兼容老结果页需要的字段（pois / meta.cons / meta.hotels）----
    all_pois, seen_pid = [], set()
    for sp in seg_results:
        for p in sp["pois"]:
            pid = f"{sp['city']}/{p.id}"
            if pid in seen_pid:
                continue
            seen_pid.add(pid)
            all_pois.append({
                "id": pid, "name": p.name, "category": p.category,
                "tags": list(p.tags), "lat": p.lat, "lng": p.lng,
                "open": core.hhmm(p.open_min), "close": core.hhmm(p.close_min),
                "base": p.base, "ticket": p.ticket, "visit_min": p.visit_min,
            })
    merged_hotels = []
    for seg in seg_out:
        for row in seg["hotel_rows"]:
            merged_hotels.append({**row, "segment": seg["city"]})

    payload = {
        "meta": {
            "mode": "route",
            "depart_from": depart_from,
            "start_date": start_date,
            "days": total_days,
            "city": " → ".join(seg["city"] for seg in seg_out),
            "query": "",
            "notes": notes,
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "travel_source": "+".join(sorted({seg["travel_source"] for seg in seg_out})),
            "cons": {
                "days": total_days,
                "budget": float(budget) if budget else 0.0,
                "interests": dict(interests or {}),
                "day_start": day_start,
                "day_end": day_end,
                "hotel": "每段一家",
                "city": " → ".join(seg["city"] for seg in seg_out),
                "food_per_day": float(food_per_day) if food_per_day
                else core.TripConstraints(days=total_days).food_per_day,
                "pocket_per_day": core.TripConstraints(days=total_days).pocket_per_day,
                "city_mode": city_mode or "mixed",
                "km_cost": core.KM_COST,
                "max_stops_per_day": core.TripConstraints(days=total_days).max_stops_per_day,
                "max_visit_min_per_day": core.TripConstraints(days=total_days).max_visit_min_per_day,
                "must_visit": [],
                "must_ids": [],
                "hotel_price": 0.0,
                "travelers": _travelers,
                "room_beds": int(room_beds or 0),
                "room_price": 0.0,
                "party": {
                    "adults": int(adults), "children": int(children),
                    "children_free": int(children_free), "seniors": int(seniors),
                },
                "intercity": [ _leg_dict(l) for l in legs],
            },
            "datum": "wgs84",
            "dataset": "amap",
            "lambda_travel": core.Plan.LAMBDA_TRAVEL,
            "evals": {"greedy": 0, "total": 0},
            "data_note": "多城市环线：每段独立求解，城际按用户选定的出行方式估算",
            "recall_k": 0,
            "pool_size": len(all_pois),
            "solved_size": len(all_pois),
            "hotels": merged_hotels,
        },
        "hotel": {"name": "每段一家", "lat": 0.0, "lng": 0.0},
        "pois": all_pois,
        "recall": None,
        "segments": seg_out,
        "legs": [ _leg_dict(l) for l in legs],
        "totals": {
            "days": total_days,
            "segments": len(seg_out),
            "stops": total_stops,
            "utility": round(total_utility, 1),
            "intercity_cost": round(intercity_cost, 1),
            "cost": round(total_cost, 1),
            "travelers": _travelers,
            "rooms": _rooms,
            "per_person": round(total_cost / _travelers, 1),
            "budget": budget,
            "km": round(total_km, 1),
            "travel_min": round(total_travel_min),
            "self_drive": bool(drive_legs),
            "self_drive_note": "本方案含自驾，油费过路费未计入" if drive_legs else "",
        },
        "checks": checks,
        "variants": variants,
        "verify": {"checks": checks, "passed": passed, "total": len(checks),
                   "pass_rate": round(passed / len(checks) * 100, 1)},
    }
    # ---- 时刻锚点核一遍（第 19 条，环线）：排进去了没有、到得早不早 ----
    if anchor_name and anchor_before_min:
        hit = None
        for seg in seg_out:
            for d in seg.get("days") or []:
                for s in ((d.get("plan") or {}).get("stops") or []):
                    if anchor_name in (s.get("name") or ""):
                        hit = (d.get("index"), s.get("arrive_str"), d.get("date"))
                        break
                if hit:
                    break
            if hit:
                break
        if hit is None:
            notes.insert(0, f"「{anchor_name}」没能排进任何一天，所以 {anchor_before} 前到不了")
        else:
            day_no, arrive_str, date = hit
            try:
                _h, _m = (arrive_str or "00:00").split(":")
                arrive_min = int(_h) * 60 + int(_m)
            except ValueError:
                arrive_min = 0
            if anchor_day and day_no != anchor_day:
                notes.insert(0, f"「{anchor_name}」排在第 {day_no} 天，不是你说的第 {anchor_day} 天")
            elif arrive_min <= anchor_before_min:
                notes.insert(0, f"「{anchor_name}」第 {day_no} 天 {arrive_str} 到，赶得上 {anchor_before}")
            else:
                notes.insert(0, f"「{anchor_name}」第 {day_no} 天最快 {arrive_str} 到，"
                                f"赶不上你说的 {anchor_before}")

    return payload, {"segments": seg_out, "legs": legs, "totals": payload["totals"]}


def _day_dict(d_or_plan) -> dict:
    """把一个 DayPlan 转成给页面用的 dict（复用 Plan.to_dict 的单日结构）。"""
    if hasattr(d_or_plan, "total_km"):
        d = d_or_plan
        return {
            "index": d.index + 1,
            "total_km": round(d.total_km, 1),
            "travel_min": round(d.travel_min),
            "rests": [
                {"start": int(round(r["start"])), "end": int(round(r["end"])), "min": r["min"],
                 "start_str": core.hhmm(r["start"]), "end_str": core.hhmm(r["end"])}
                for r in (getattr(d, "rests", []) or [])
            ],
            "stops": [
                {
                    "id": s.poi.id, "name": s.poi.name, "category": s.poi.category,
                    "tags": list(s.poi.tags), "lat": s.poi.lat, "lng": s.poi.lng,
                    "arrive": s.arrive, "leave": s.leave,
                    "arrive_str": core.hhmm(s.arrive), "leave_str": core.hhmm(s.leave),
                    "window": f"{core.hhmm(s.poi.open_min)}–{core.hhmm(s.poi.close_min)}",
                    "visit_min": s.poi.visit_min, "ticket": s.poi.ticket,
                    "utility": round(s.utility, 1),
                    "leg_km": round(s.leg_km, 1), "leg_min": round(s.leg_min),
                    "wait_min": s.wait_min,
                }
                for s in d.stops
            ],
        }
    return {}
