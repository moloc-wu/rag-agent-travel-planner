"""
optw_core —— 多日定向越野问题（Orienteering Problem with Time Windows, OPTW）核心。

模型要点
--------
* 候选点集合 C：每个点有 开放/关闭时间窗、建议游览时长、门票、基础效用。
* 行程：D 天，每天从住宿点出发、回到住宿点，受每日起止时间约束。
* 硬约束：时间窗、每日时长、总预算、点不重复、往返可达。
* 目标：在硬约束可行域内最大化  Σ效用 − λ·Σ交通时间（效用与赶路代价折中）。

算法（对应毕设计划书 M1 / M2）
------------------------------
M1  效用密度贪心插入：每次挑"单位时间收益最高"的点插到最优位置，保证出一个可行解。
M2  局部搜索：relocate（换天/换位）、swap（两点交换）、同日 2-opt，在可行域里爬坡。

设计原则：求解器只优化，不判合规；合规由 Verifier 独立复核（两套代码，避免自证）。
"""
from __future__ import annotations

import datetime
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ==================== 地理 / 时间 ====================

EARTH_R = 6371.0088
DETOUR = 1.35          # 路网绕行系数
SPEED_KMH = 24.0       # 市内平均车速
OVERHEAD_MIN = 5.0     # 停车/步行/找门口
MIN_LEG_MIN = 8.0      # 再近的两点也要走这么久

KM_COST = 2.6          # 市内交通单价 ¥/km（打车与公交混合的经验值）
# 第 63 条：不同市内交通方式的口径完全不一样，分开算
DRIVE_KM_COST = 1.2    # 自驾：油费 + 过路费摊到每公里，按车算，不乘人数
TAXI_BASE_FARE = 13.0  # 打车起步价（大部分城市 10–15 元）
TAXI_KM_COST = 2.5     # 打车超出起步里程后的单价 ¥/km
TRANSIT_FARE = 2.5     # 公交地铁单程票价，按人次算

EPS = 1e-6             # 时间比较容差：分钟是浮点数，别让 179.9999999 判成不够 180

_WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def day_label(start_date: str, index: int):
    """
    第 index 天（0 起）的 (ISO 日期, 中文周几)；没有起始日或日期写错就返回 (None, None)。

    有了它，结果页才可能显示「11月9日 周日」，闭馆日判断也才有依据。
    """
    if not start_date or index is None or index < 0:
        return None, None
    try:
        d0 = datetime.date.fromisoformat(str(start_date)[:10])
    except (ValueError, TypeError):
        return None, None
    d = d0 + datetime.timedelta(days=int(index))
    return d.isoformat(), _WEEK_CN[d.weekday()]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def travel_km(lat1, lng1, lat2, lng2) -> float:
    return haversine_km(lat1, lng1, lat2, lng2) * DETOUR


def travel_min(lat1, lng1, lat2, lng2) -> float:
    d = travel_km(lat1, lng1, lat2, lng2)
    return max(MIN_LEG_MIN, d / SPEED_KMH * 60 + OVERHEAD_MIN)


def hhmm(minutes: float) -> str:
    """分钟（从 00:00 起）→ 'HH:MM'，跨过 24:00 用 24:xx 表示。"""
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ==================== 数据模型 ====================

@dataclass(frozen=True)
class Poi:
    id: str
    name: str
    category: str
    tags: tuple
    lat: float
    lng: float
    open_min: int
    close_min: int
    visit_min: int
    ticket: float
    base: float
    closed_days: tuple = ()      # 闭馆日，0 = 周一；空表示每天都开
    effort: str = "一般"          # 体力强度：轻松 / 一般 / 费体力（tag_effort.py 标的）
    ticket_source: str = "estimated"   # estimated=估算 / official=人工核过（第 26 条）

    @classmethod
    def from_dict(cls, d: dict) -> "Poi":
        return cls(
            id=d["id"],
            name=d["name"],
            category=d["category"],
            tags=tuple(d.get("tags", [])),
            lat=float(d["lat"]),
            lng=float(d["lng"]),
            open_min=parse_hhmm(d["open"]),
            close_min=parse_hhmm(d["close"]),
            visit_min=int(d["visit_min"]),
            ticket=float(d.get("ticket", 0)),
            base=float(d["base"]),
            closed_days=tuple(int(x) for x in (d.get("closed_days") or infer_closed_days(
                d["name"], d.get("category", "")))),
            effort=str(d.get("effort") or "一般"),
            ticket_source=str(d.get("ticket_source") or "estimated"),
        )

    def closed_on(self, weekday: int | None) -> bool:
        """weekday 0 = 周一；None 表示不知道日期，不判。"""
        return weekday is not None and weekday in self.closed_days


# 名称规则兜底：老数据没有 closed_days 字段时按名字推断。
# 国内博物馆/美术馆绝大多数周一闭馆，这条规则比「不知道就全当开放」更接近事实。
MUSEUM_WORDS = ("博物馆", "美术馆", "纪念馆", "展览馆", "科技馆", "艺术馆", "规划馆")


def infer_closed_days(name: str, category: str = "") -> list:
    blob = f"{name} {category}"
    if any(w in blob for w in MUSEUM_WORDS):
        return [0]
    return []


@dataclass
class TripConstraints:
    days: int
    day_start: int = 9 * 60            # 09:00 出门
    day_end: int = 21 * 60 + 30        # 21:30 回到住宿
    budget: float = 1500.0
    interests: dict = field(default_factory=lambda: {"自然风光": 1.0, "历史文化": 1.0})
    hotel_lat: float = 0.0            # 住宿点坐标由选中的城市数据集决定，这里不预设任何城市
    hotel_lng: float = 0.0
    hotel_name: str = ""
    hotel_pick: str = ""              # 用户自己点名要住的那家（空 = 让算法挑，见第 68 条）
    city: str = ""                    # 这次规划的城市：数据集 / 自然语言里来的
    max_stops_per_day: int = 5        # 一天最多去几个点：真人一天走五个就很累了
    max_visit_min_per_day: int = 480  # 一天累计游览时长上限（不含路上）
    must_ids: tuple = ()             # 用户点名必去的景点 id（硬约束）
    must_visit: tuple = ()           # 上面对应的名字，用来显示
    hotel_price: float = 0.0         # 每晚房价（估算值），进预算
    food_per_day: float = 80.0         # 餐饮按每人每天 80 估
    # 机动/零花（第 64 条）：出门买水、临时打车、买特产——真实开销，但不该占规划的钱。
    # 它不进求解，只在总账里单列一项，并让"规划花费"和"兜里要带多少"分得开。
    pocket_per_day: float = 25.0       # 每人每天留多少机动；0 = 不留
    km_cost: float = KM_COST
    # 市内交通方式（第 63 条）：mixed=打车公交混合（老口径）/ drive=自驾 / taxi=打车 / transit=公交地铁。
    # 三种口径完全不一样——自驾按车算油费、打车按趟算、公交按人次算，不能一套公式吃所有人。
    city_mode: str = "mixed"
    max_wait_min: int = 120            # 单次等开门上限，超过就换顺序
    # 逐日时间窗：[(开始分钟, 结束分钟), ...]，第 0 项是第 1 天。
    # 空元组表示所有天都用 day_start / day_end（老行为）。
    day_windows: tuple = ()
    # 每天属于哪个城市（只用于展示和校验，不影响求解）
    day_city: tuple = ()
    # 每天是周几（0 = 周一）。有它才能判断景点闭馆日和「排到周一去故宫」这种错。
    day_weekdays: tuple = ()
    # 每天是不是下雨（第 22 条）：空元组表示天气未知，雨天逻辑不生效。
    # 有值那天户外点效用打折，尽量把博物馆/美术馆往雨天排。
    day_rainy: tuple = ()
    # 休息日（第 25 条）：值是天序号（0 起）。旅游不是拉练，有人就想在路上躺一天。
    # rest_full = 整天休（睡到自然醒，不排点、不排早餐）；
    # rest_half = 半休（13:00 以后才出门，早餐顺延、早午餐合成一顿）。
    rest_full: tuple = ()
    rest_half: tuple = ()
    # 出发日期 "YYYY-MM-DD"。只用于算每天是几月几号、周几，不参与求解。
    start_date: str = ""
    # 不想要的类型（climb / museum / temple / shopping / night），来自自然语言的否定句
    avoid: tuple = ()

    # ---- 出行人数（第 27 条，第 54 条补学生）----
    # 成人全价；儿童（满 6 岁不满 14 岁）半价；学生（在校生）车票 75 折、门票常见半价；
    # 未满 6 岁不占座免票（多数景区也免）；老人在铁路没有优惠，景区多数半价。
    adults: int = 1
    children: int = 0
    children_free: int = 0
    seniors: int = 0
    students: int = 0
    # 房型：床位数（1 大床 / 2 标准间 / 3 家庭房）和每晚单价。
    # room_beds = 0 表示用户没选房型，按 2 人一间 + 酒店基准价算（保持老行为）。
    room_beds: int = 0
    room_price: float = 0.0
    # 第 69 条：用户自己分配的房型，((床位数, 间数), ...)，比如 ((1, 1), (2, 1)) = 1 间大床 + 1 间标间。
    # 空表示没分配，还是按人数自动推间数（老行为）。
    room_plan: tuple = ()
    # 各房型单价 {床位数: 单价}，由 planner 按选中的那家酒店填，房费按这个算
    room_prices: dict = field(default_factory=dict)
    # 住宿晚数：0 表示按 days-1 算（单城默认）。环线里只有最后一段是 days-1，
    # 前面几段当天到了是要住的，得住满 days 晚（第 28 条）。
    nights: int = 0
    # 中段休息（第 20 条）：一天里排一次，歇够 rest_min 分钟；0 = 不安排
    rest_min: int = 60
    rest_from_min: int = 11 * 60 + 30      # 11:30
    rest_to_min: int = 13 * 60 + 30        # 13:30

    @property
    def travelers(self) -> int:
        return max(1, self.adults + self.children + self.children_free
                   + self.seniors + self.students)

    @property
    def ticket_factor(self) -> float:
        """景区门票折算：成人 1 张，儿童 / 老人 / 学生半张，未满 6 岁不算。"""
        return self.adults + 0.5 * (self.children + self.seniors + self.students)

    @property
    def train_ticket_factor(self) -> float:
        """火车票折算：成人 / 老人全价（铁路没有老年优惠），儿童半价，学生 75 折，未满 6 岁不占座免票。"""
        return self.adults + self.seniors + 0.5 * self.children + 0.75 * self.students

    @property
    def beds_per_room(self) -> int:
        return self.room_beds or 2

    @property
    def hotel_nights(self) -> int:
        """实际住宿晚数：显式给了 nights 就用它，否则沿用 days-1。"""
        return self.nights if self.nights > 0 else max(0, self.days - 1)

    @property
    def pocket_total(self) -> float:
        """整趟的机动 / 零花：每人每天 × 人数 × 天数。"""
        return self.pocket_per_day * self.travelers * self.days

    @property
    def plan_budget(self) -> float:
        """求解时真正能用的预算（第 64 / 67-7 条）：总预算扣掉机动预留。

        用户填的「预算」是他兜里一共带多少，机动也要花，不能全拿去排景点。
        budget 为 0 表示没设预算，返回 0，调用方按「不限」处理。
        """
        if not self.budget or self.budget <= 0:
            return 0.0
        return max(0.0, self.budget - self.pocket_total)

    @property
    def rooms_needed(self) -> int:
        plan = self.planned_rooms
        if plan:
            return sum(c for _, c in plan)
        return -(-self.travelers // max(1, self.beds_per_room))

    @property
    def planned_rooms(self) -> list:
        """用户自己分配的房型清单 [(床位数, 间数)]；没分配就是空列表。"""
        return [(int(b), int(c)) for b, c in self.room_plan if int(c) > 0]

    def room_price_for(self, beds: int) -> float:
        """某个房型的单间价：填了就用那家的价，没填退回酒店基准价。"""
        return float(self.room_prices.get(int(beds)) or self.hotel_price)

    @property
    def planned_beds(self) -> int:
        return sum(b * c for b, c in self.planned_rooms)

    @property
    def bed_short(self) -> int:
        """按用户自己的分配睡，还差几个床位（0 = 睡得下）。"""
        plan = self.planned_rooms
        if not plan:
            return 0
        return max(0, self.travelers - self.planned_beds)

    @property
    def night_price(self) -> float:
        return self.room_price or self.hotel_price

    def weekday_for(self, index: int):
        if self.day_weekdays and 0 <= index < len(self.day_weekdays):
            return self.day_weekdays[index]
        return None

    def window_for(self, index: int):
        """第 index 天（0 起）的可用时间窗。第 1 天从到达算、最后一天到返程截止，都靠这个表达。"""
        # 第 25 条：休息日窗口长度为 0，任何点都排不进去
        if index in (self.rest_full or ()):
            return self.day_start, self.day_start
        if index in (self.rest_half or ()):
            return max(self.day_start, 13 * 60), self.day_end
        if self.day_windows and 0 <= index < len(self.day_windows):
            return self.day_windows[index]
        return self.day_start, self.day_end

    def is_rest_day(self, index: int) -> bool:
        """整天休息：睡到自然醒，一个点都不排。"""
        return index in (self.rest_full or ())

    def is_half_day(self, index: int) -> bool:
        """半休：13:00 才出门。"""
        return index in (self.rest_half or ())

    def window_str(self, index: int) -> str:
        """给人看的当天可玩窗口，例：09:00–21:30。"""
        w = self.window_for(index)
        return f"{hhmm(w[0])}–{hhmm(w[1])}"

    def summary(self) -> dict:
        return {
            "days": self.days,
            "budget": self.budget,
            "interests": dict(self.interests),
            "day_start": hhmm(self.day_start),
            "day_end": hhmm(self.day_end),
            "hotel": self.hotel_name or "住宿点",
            "city": self.city,
            "food_per_day": self.food_per_day,
            "pocket_per_day": self.pocket_per_day,
            "city_mode": self.city_mode,
            # 第 67-1 条：编辑重排要能还原「哪天下雨」，不然雨天打折白算
            "day_rainy": list(self.day_rainy or ()),
            # 第 25 条：休息日也要能还原，不然编辑一次那天的点全回来了
            "rest_full": list(self.rest_full or ()),
            "rest_half": list(self.rest_half or ()),
            "km_cost": self.km_cost,
            "max_stops_per_day": self.max_stops_per_day,
            "max_visit_min_per_day": self.max_visit_min_per_day,
            "must_visit": list(self.must_visit),
            "must_ids": list(self.must_ids),
            "hotel_price": self.hotel_price,
            "start_date": self.start_date,
            "travelers": self.travelers,
            "room_beds": self.room_beds,
            "room_price": self.room_price,
            # 第 69 条：编辑重排要能还原用户自己分配的房型，不然一编辑就退回自动推
            "room_plan": [{"beds": b, "count": c} for b, c in self.planned_rooms],
            "party": {
                "adults": self.adults,
                "children": self.children,
                "children_free": self.children_free,
                "seniors": self.seniors,
            },
        }


DEFAULT_INTERESTS = {
    "自然风光": 1.0, "历史文化": 1.0, "美食": 1.0,
    "夜景": 1.0, "亲子": 1.0, "艺术": 1.0, "网红打卡": 1.0,
}


def _db_conf():
    """读 data/db.json 的连接信息；不是 mysql 就返回 None。"""
    try:
        cfg = json.loads((Path(__file__).resolve().parent / "data" / "db.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if cfg.get("driver") == "mysql" else None


def _city_from_path(path) -> str:
    """从 data/cities/长沙/pois.json 里取出「长沙」。"""
    p = Path(str(path))
    return p.parent.name if p.parent.name not in ("", "cities") else ""


def _load_from_db(city: str):
    """
    从 MySQL 的 poi / city 表读一个城市（第 4 条第二步）。

    读不到（没库、没表、表里没这个城市、出任何错）都返回 None，由调用方退回读 JSON。
    餐饮和商场在这里就被过滤掉了——`import_data.py` 入库时打了 is_meal / is_mall 标记。
    """
    if not city:
        return None
    cfg = _db_conf()
    if not cfg:
        return None
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset="utf8mb4", connect_timeout=4,
        )
    except Exception:          # noqa: BLE001 - 库连不上不该让规划挂掉
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT poi_id, name, category, tags, lng, lat, open_time, close_time, "
                "       visit_min, ticket, base, effort, closed_days, ticket_source "
                "FROM poi WHERE city=%s AND is_meal=0 AND is_mall=0",
                (city,),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            cur.execute("SELECT lng, lat FROM city WHERE name=%s", (city,))
            crow = cur.fetchone() or (0.0, 0.0)
    except Exception:          # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:      # noqa: BLE001
            pass

    pois = []
    for (pid, name, cat, tags, lng, lat, ot, ct, vmin, ticket, base, effort, closed, tsrc) in rows:
        try:
            tag_list = json.loads(tags) if tags else []
        except (TypeError, ValueError):
            tag_list = []
        closed_days = tuple(int(x) for x in (closed or "").split(",") if x.strip().isdigit())
        try:
            pois.append(Poi.from_dict({
                "id": pid, "name": name, "category": cat or "",
                "tags": tag_list, "lng": lng, "lat": lat,
                "open": (ot or "00:00")[:5], "close": (ct or "23:59")[:5],
                "visit_min": vmin or 60, "ticket": ticket or 0, "base": base or 0,
                "effort": effort or "一般", "closed_days": list(closed_days),
                "ticket_source": tsrc or "estimated",
            }))
        except (KeyError, TypeError, ValueError):
            continue
    if not pois:
        return None
    hotel = {"name": "住宿点", "lat": float(crow[1] or 0.0), "lng": float(crow[0] or 0.0)}
    return city, pois, hotel


def load_meals(city: str) -> list:
    """
    单独取一个城市的餐饮 POI（第 4 条：餐饮不进景点池，但第 24 条要拿它推吃饭）。

    返回 dict 列表（跟 `pick_meals` 兼容）；库不可用就返回空，调用方自己退。
    """
    cfg = _db_conf()
    if not cfg or not city:
        return []
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg["host"], port=int(cfg["port"]), user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            charset="utf8mb4", connect_timeout=4,
        )
    except Exception:          # noqa: BLE001
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT poi_id, name, category, tags, lng, lat, open_time, close_time, "
                "       visit_min, ticket, base "
                "FROM poi WHERE city=%s AND is_meal=1",
                (city,),
            )
            rows = cur.fetchall()
    except Exception:          # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:      # noqa: BLE001
            pass
    out = []
    for (pid, name, cat, tags, lng, lat, ot, ct, vmin, ticket, base) in rows:
        try:
            tag_list = json.loads(tags) if tags else []
        except (TypeError, ValueError):
            tag_list = []
        out.append({"id": pid, "name": name, "category": cat or "", "tags": tag_list,
                    "lng": float(lng or 0), "lat": float(lat or 0),
                    "visit_min": vmin or 60, "ticket": ticket or 0, "base": base or 0})
    return out


def load_dataset(path: str, city: str | None = None):
    """优先从 MySQL 读；没库 / 表空 / 出错都退回读 JSON 文件（第 4 条第二步）。"""
    name = city or _city_from_path(path)
    got = _load_from_db(name)
    if got:
        return got
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    pois = [Poi.from_dict(x) for x in raw["pois"]]
    hotel = raw.get("hotel") or {"name": "住宿点", "lat": 0.0, "lng": 0.0}
    return raw.get("city", ""), pois, hotel


MUST_BONUS = 2.5      # 必去景点的效用倍数：宁可多绕点路，也不能把它漏掉

# 第 22 条：下雨天把户外点往后排，室内点不受影响
INDOOR_KEYS = ("博物馆", "美术馆", "展览", "科技馆", "纪念馆", "图书馆",
               "剧院", "大剧院", "艺术馆", "规划馆", "科教文化", "美术馆")
# 这几类是「一定是户外」，哪怕名字里带博物馆（恭王府博物馆其实是王府+花园，不是展馆）
STREET_KEYS = ("步行街", "商业街", "街区", "胡同", "广场", "大栅栏", "王府")
OUTDOOR_KEYS = ("公园", "风景名胜", "国家级景点", "自然风光", "山", "湖",
                "古镇", "寺庙道观", "寺")
RAIN_OUTDOOR_FACTOR = 0.2    # 雨天户外点效用打这个折（不是禁掉——用户可能就想雨中逛）


def is_outdoor(poi: Poi) -> bool:
    """这个点下雨天去是不是难受。步行街/广场这类先定死是户外，再看室内词，最后落到户外词。"""
    text = f"{poi.category}{''.join(poi.tags)}{poi.name}"
    if any(k in text for k in STREET_KEYS):
        return True
    if any(k in text for k in INDOOR_KEYS):
        return False
    return any(k in text for k in OUTDOOR_KEYS)


def utility(poi: Poi, interests: dict, must_ids: tuple = (), rainy: bool = False) -> float:
    """
    效用 = 基础热度 × 偏好匹配度。
    匹配度取该点所有标签的偏好权重均值；用户没表态的标签按 0.55 折价（不强推）。
    rainy=True 时户外点打折（第 22 条）：不动「能不能去」，只动「值不值得排这天」。
    """
    weights = [interests.get(t, 0.55) for t in poi.tags]
    if not weights:
        weights = [0.55]
    match = sum(weights) / len(weights)
    u = poi.base * match
    if must_ids and poi.id in must_ids:
        u *= MUST_BONUS          # 必去的点就算下雨也得排，不打折
    elif rainy and is_outdoor(poi):
        u *= RAIN_OUTDOOR_FACTOR
    return u


# ==================== 行程结构 ====================

@dataclass
class Stop:
    poi: Poi
    arrive: int
    leave: int
    leg_km: float
    leg_min: float
    utility: float
    wait_min: int


@dataclass
class DayPlan:
    index: int
    stops: list
    total_km: float
    travel_min: float
    back_min: float
    # 中段吃饭时段（第 59 条）：build_day 插的那 60 分钟以前只改时间不留痕，
    # 页面上看起来就是「下一站怎么到得这么晚」。这里显式记下来，前端才排得出那一行。
    rests: list = field(default_factory=list)   # [{"start": 720, "end": 780, "min": 60}]

    @property
    def end_time(self):
        return self.stops[-1].leave if self.stops else None


@dataclass
class Plan:
    days: list
    cons: TripConstraints

    # ---- 汇总 ----
    @property
    def stops(self):
        return [s for d in self.days for s in d.stops]

    @property
    def total_utility(self) -> float:
        return sum(s.utility for s in self.stops)

    @property
    def ticket(self) -> float:
        """按人群折算：成人 1 张，1.2m 以上儿童 / 老人半张，1.2m 以下免票。"""
        return sum(s.poi.ticket for s in self.stops) * self.cons.ticket_factor

    @property
    def food(self) -> float:
        """每人每天 × 人数 × 天数。"""
        return self.cons.food_per_day * self.cons.travelers * self.cons.days

    @property
    def transport(self) -> float:
        """市内交通（第 63 条）：按用户选的方式算。

        mixed —— 打车与公交混合的经验值，只乘公里数（老口径，不是自驾也不是纯打车）
        drive —— 自驾：公里数 × 油费过路费，按车算，不乘人数
        taxi  —— 打车：每去一个点算一趟（起步价 + 超起步里程），一辆车坐几个人都一样，不乘人数
        transit—— 公交地铁：按人次算，跟公里数关系不大
        """
        km = sum(d.total_km for d in self.days)
        mode = (self.cons.city_mode or "mixed").strip()
        # 第 67-5 条：趟数要算上「酒店 → 第一个点」，不然少一趟起步价
        trips = len(self.stops) + 1
        if mode == "drive":
            return km * DRIVE_KM_COST
        if mode == "taxi":
            return TAXI_BASE_FARE * trips + km * TAXI_KM_COST
        if mode == "transit":
            return TRANSIT_FARE * trips * max(1, self.cons.travelers)
        return km * self.cons.km_cost

    @property
    def hotel_cost(self) -> float:
        """住宿 N 晚（默认 days-1，环线非最后一段住满 days）。

        第 69 条：用户自己分了房型就按他那份算（每种房型各按自己的价）；
        没分配才退回「房间数 = 人数 ÷ 房型床位数，向上取整」。
        """
        plan = self.cons.planned_rooms
        if plan:
            per_night = sum(self.cons.room_price_for(b) * c for b, c in plan)
        else:
            per_night = self.cons.night_price * self.cons.rooms_needed
        return per_night * self.cons.hotel_nights

    @property
    def total_cost(self) -> float:
        return self.ticket + self.food + self.transport + self.hotel_cost

    @property
    def pocket(self) -> float:
        """机动/零花（第 64 条）：买水、临时打车、特产——真金白银，但不进规划。
        单列出来，用户才知道「规划花这么多、兜里还得带这么多」。"""
        return self.cons.pocket_per_day * self.cons.travelers * self.cons.days

    @property
    def per_person(self) -> float:
        return self.total_cost / self.cons.travelers

    @property
    def travel_min(self) -> float:
        return sum(d.travel_min for d in self.days)

    @property
    def km(self) -> float:
        return sum(d.total_km for d in self.days)

    LAMBDA_TRAVEL = 0.8    # 每 1 分钟车程折算掉 0.8 效用：越大越不愿意为了一个点绕远路
    LAMBDA_HOTEL = 0.6     # 每 100 元住宿费折算掉 0.6 效用：贵要有贵的道理
    LAMBDA_RATING = 3.0    # 酒店评分每高 1 分、每晚值 3 点效用

    def score(self) -> float:
        return self.total_utility - self.LAMBDA_TRAVEL * self.travel_min

    def full_score(self, rating: float = 4.0) -> float:
        """算上房费和住得舒不舒服的总分——选酒店用的就是它。"""
        nights = self.cons.hotel_nights
        return (self.score()
                - self.LAMBDA_HOTEL * self.hotel_cost / 100.0
                + self.LAMBDA_RATING * (rating - 4.0) * nights)

    def to_dict(self) -> dict:
        return {
            "days": [
                {
                    "index": d.index + 1,
                    "date": (lab := day_label(self.cons.start_date, d.index))[0],
                    "weekday": lab[1],
                    "window": self.cons.window_str(d.index),
                    # 第 25 条：这一天是不是休息日，前端据此写「休息日（睡到自然醒）」
                    "rest": bool(self.cons.is_rest_day(d.index)),
                    "half": bool(self.cons.is_half_day(d.index)),
                    "total_km": round(d.total_km, 1),
                    "travel_min": round(d.travel_min),
                    "rests": [
                        {
                            "start": int(round(r["start"])), "end": int(round(r["end"])),
                            "min": r["min"],
                            "start_str": hhmm(r["start"]), "end_str": hhmm(r["end"]),
                        }
                        for r in getattr(d, "rests", []) or []
                    ],
                    "stops": [
                        {
                            "id": s.poi.id,
                            "name": s.poi.name,
                            "category": s.poi.category,
                            "tags": list(s.poi.tags),
                            "lat": s.poi.lat,
                            "lng": s.poi.lng,
                            "arrive": s.arrive,
                            "leave": s.leave,
                            "arrive_str": hhmm(s.arrive),
                            "leave_str": hhmm(s.leave),
                            "window": f"{hhmm(s.poi.open_min)}–{hhmm(s.poi.close_min)}",
                            "visit_min": s.poi.visit_min,
                            "ticket": s.poi.ticket,
                            "ticket_source": s.poi.ticket_source,
                            "utility": round(s.utility, 1),
                            "leg_km": round(s.leg_km, 1),
                            "leg_min": round(s.leg_min),
                            "wait_min": s.wait_min,
                        }
                        for s in d.stops
                    ],
                }
                for d in self.days
            ],
            "totals": {
                "stops": len(self.stops),
                "utility": round(self.total_utility, 1),
                "ticket": round(self.ticket, 1),
                "food": round(self.food, 1),
                "transport": round(self.transport, 1),
                "hotel": round(self.hotel_cost, 1),
                "pocket": round(self.pocket, 1),
                "grand_total": round(self.total_cost + self.pocket, 1),
                "total_cost": round(self.total_cost, 1),
                "per_person": round(self.per_person, 1),
                "travelers": self.cons.travelers,
                "rooms": self.cons.rooms_needed,
                "nights": self.cons.hotel_nights,
                "budget": self.cons.budget,
                "km": round(self.km, 1),
                "travel_min": round(self.travel_min),
                "score": round(self.score(), 1),
            },
        }


# ==================== 交通模型 ====================

class Travel:
    """
    两点之间的 距离/时间。
    优先用高德实测驾车矩阵（matrix），缺的边退回直线估算（haversine + 绕行系数）。
    """
    HOTEL = "__hotel__"

    def __init__(self, coords: dict, matrix: dict | None = None):
        self.coords = coords      # id -> (lat, lng)，含 "__hotel__"
        self.matrix = matrix or {}

    @property
    def source(self) -> str:
        return "amap" if self.matrix else "estimate"

    def get(self, a: str, b: str):
        row = self.matrix.get(a) or {}
        hit = row.get(b)
        if hit:
            return float(hit[0]), float(hit[1])
        la1, ln1 = self.coords[a]
        la2, ln2 = self.coords[b]
        return travel_km(la1, ln1, la2, ln2), travel_min(la1, ln1, la2, ln2)


# ==================== 单日排程（可行性判断的唯一出处） ====================

def build_day(ids: Iterable[str], cons: TripConstraints, pool: dict, travel: Travel, index: int = 0):
    """按给定访问顺序排一天的时间轴。不可行返回 None。
    时间窗按 index 取（逐日），不是所有天都用同一个 09:00-21:30。"""
    ids = list(ids)
    # 第 25 条：休息日一个点都不排——求解器往里塞点也不会生效
    if cons.is_rest_day(index):
        ids = []
        return DayPlan(index, [], 0.0, 0.0, 0.0, [])
    if len(ids) > cons.max_stops_per_day:
        return None
    if sum(pool[i].visit_min for i in ids if i in pool) > cons.max_visit_min_per_day:
        return None
    start_t, end_t = cons.window_for(index)
    weekday = cons.weekday_for(index)
    stops = []
    t = start_t
    prev = Travel.HOTEL
    total_km = 0.0
    total_travel = 0.0
    rest_done = False
    rests = []      # 第 59 条：把吃饭那段时间记下来，前端时间轴要显示

    for pid in ids:
        p = pool[pid]
        if p.closed_on(weekday):        # 这天闭馆，排了也去不成
            return None
        leg_km, leg_min = travel.get(prev, pid)
        arrive = t + leg_min
        # 中段休息（第 20 条 / 第 66 条）：以前只看「到站那一刻恰好落在 11:30–13:30」，
        # 于是 10:55 到、13:25 走的那一天整天没有午饭。现在分两种情况：
        #   ① 到站时人已经在饭点里 → 先吃再玩
        #   ② 这一站从饭点前玩到饭点后（跨过 11:30–13:30）→ 出门再吃（见循环末尾）
        if (not rest_done and cons.rest_min > 0
                and cons.rest_from_min <= arrive <= cons.rest_to_min):
            rests.append({"start": t, "end": t + cons.rest_min, "min": cons.rest_min})
            t += cons.rest_min
            arrive = t + leg_min
            rest_done = True
        wait = 0
        if arrive < p.open_min:
            wait = p.open_min - arrive
            if wait > cons.max_wait_min:
                return None
            arrive = p.open_min
        if arrive > p.close_min + EPS:
            return None
        leave = arrive + p.visit_min
        if leave > p.close_min + EPS or leave > end_t + EPS:
            return None
        stops.append(Stop(p, arrive, leave, leg_km, leg_min, 0.0, int(round(wait))))
        t = leave
        total_km += leg_km
        total_travel += leg_min
        prev = pid
        # 第 66 条 ②：这一站从饭点前玩到饭点后，那就出了门再吃饭（下一站从吃完算起）
        if (not rest_done and cons.rest_min > 0
                and arrive < cons.rest_to_min and leave > cons.rest_from_min):
            rests.append({"start": t, "end": t + cons.rest_min, "min": cons.rest_min})
            t += cons.rest_min
            rest_done = True

    back_km, back_min = travel.get(prev, Travel.HOTEL)
    if stops and t + back_min > end_t + EPS:
        return None
    total_km += back_km
    total_travel += back_min
    return DayPlan(index, stops, total_km, total_travel, back_min, rests)


# ==================== 求解器 ====================

class Solver:
    def __init__(self, cons: TripConstraints, pois: list, travel: Travel | None = None):
        self.cons = cons
        self.pois = pois
        self.pool = {p.id: p for p in pois}
        if travel is None:
            coords = {p.id: (p.lat, p.lng) for p in pois}
            coords[Travel.HOTEL] = (cons.hotel_lat, cons.hotel_lng)
            travel = Travel(coords)
        self.travel = travel
        self._day_cache = {}
        self._evals = 0

    # ---- 带缓存的组装：一次移动通常只重算一天 ----
    def day(self, ids, index: int = 0):
        # ⚠ 缓存的 key 必须带 index：逐日时间窗下，同一串点放在第 1 天和
        #   放在最后一天，可行性是不一样的，不能只按内容缓存。
        key = (index, tuple(ids))
        if key not in self._day_cache:
            d = build_day(key[1], self.cons, self.pool, self.travel, index)
            if d is not None:
                rainy = (self.cons.day_rainy[index]
                         if index < len(self.cons.day_rainy) else False)
                for s in d.stops:
                    s.utility = utility(s.poi, self.cons.interests,
                                        self.cons.must_ids, rainy=rainy)
            self._day_cache[key] = d
        return self._day_cache[key]

    def assemble(self, day_lists):
        days = []
        for i, ids in enumerate(day_lists):
            d = self.day(ids, i)
            if d is None:
                return None
            days.append(DayPlan(i, d.stops, d.total_km, d.travel_min, d.back_min, d.rests))
        return Plan(days, self.cons)

    # ---- M1：效用密度贪心（也可用于"补点"） ----
    def _fill(self, day_lists, cur):
        used = {pid for l in day_lists for pid in l}
        while True:
            base_score = cur.score()
            best = None  # (density, cand, pid, plan)
            for p in self.pois:
                if p.id in used:
                    continue
                for di in range(self.cons.days):
                    for pos in range(len(day_lists[di]) + 1):
                        cand = [l[:] for l in day_lists]
                        cand[di].insert(pos, p.id)
                        plan = self.assemble(cand)
                        self._evals += 1
                        if plan is None or (self.cons.plan_budget and plan.total_cost > self.cons.plan_budget):
                            continue
                        gain = plan.score() - base_score
                        if gain <= 1e-9:
                            continue
                        density = gain / max(1.0, p.visit_min + 6.0)
                        if best is None or density > best[0]:
                            best = (density, cand, p.id, plan)
            if best is None:
                break
            _, day_lists, pid, cur = best
            used.add(pid)
        return day_lists, cur

    def greedy(self):
        lists = [[] for _ in range(self.cons.days)]
        return self._fill(lists, self.assemble(lists))

    # ---- 邻域：relocate / swap / 同日 2-opt ----
    # 第 7 条：跨天搬点/换点只动「相邻一两天」。全组合是天数平方，14 天时会卡到十几秒；
    # 而实际有用的移动本来就是「这天的点挪到前后一两天」，隔三四天挪过去既少见又要重新排一堆单日。
    NWIN = 1

    def _neighbors(self, cur):
        n = len(cur)
        for d1 in range(n):
            for i in range(len(cur[d1])):
                pid = cur[d1][i]
                for d2 in range(max(0, d1 - self.NWIN), min(n, d1 + self.NWIN + 1)):
                    for j in range(len(cur[d2]) + 1):
                        if d1 == d2 and (j == i or j == i + 1):
                            continue
                        cand = [l[:] for l in cur]
                        cand[d1].pop(i)
                        cand[d2].insert(j, pid)
                        yield cand
        for d1 in range(n):
            for i in range(len(cur[d1])):
                for d2 in range(d1, min(n, d1 + self.NWIN + 1)):
                    start = i + 1 if d2 == d1 else 0
                    for j in range(start, len(cur[d2])):
                        cand = [l[:] for l in cur]
                        cand[d1][i], cand[d2][j] = cand[d2][j], cand[d1][i]
                        yield cand
        for d in range(n):
            L = len(cur[d])
            for i in range(L):
                for j in range(i + 2, L + 1):
                    cand = [l[:] for l in cur]
                    cand[d][i:j] = list(reversed(cand[d][i:j]))
                    yield cand

    # ---- M2：局部搜索（首次改进，可行域内爬坡） ----
    def _climb(self, day_lists, plan, max_moves=60, max_evals=120000):
        cur = [l[:] for l in day_lists]
        cur_plan = plan
        cur_score = plan.score()
        moves = 0
        while moves < max_moves and self._evals < max_evals:
            accepted = False
            for cand in self._neighbors(cur):
                p = self.assemble(cand)
                self._evals += 1
                if p is None or (self.cons.plan_budget and p.total_cost > self.cons.plan_budget):
                    continue
                sc = p.score()
                if sc > cur_score + 1e-9:
                    cur, cur_plan, cur_score = cand, p, sc
                    accepted = True
                    moves += 1
                    break
                if self._evals >= max_evals:
                    break
            if not accepted:
                break
        return cur, cur_plan

    # ---- 破坏-重建：爬不动了就随机拆掉几个点，再补回来 ----
    def _ruin(self, day_lists, rng):
        out = [l[:] for l in day_lists]
        slots = [(d, i) for d, l in enumerate(out) for i in range(len(l))]
        if len(slots) <= 4:
            return out
        hi = max(3, min(6, len(slots) // 3))
        q = rng.randint(2, hi)
        gone = {out[d][i] for d, i in rng.sample(slots, q)}
        return [[pid for pid in l if pid not in gone] for l in out]

    def solve(self, ils_rounds: int = 8, seed: int = 20261107):
        lists, g_plan = self.greedy()
        greedy_evals = self._evals

        best_lists, best_plan = self._climb(lists, g_plan)
        rng = random.Random(seed)
        stale = 0
        for _ in range(ils_rounds):
            cand = self._ruin(best_lists, rng)
            cand_plan = self.assemble(cand)
            if cand_plan is None:
                continue
            cand, cand_plan = self._fill(cand, cand_plan)      # 重建
            cand, cand_plan = self._climb(cand, cand_plan)     # 再爬
            if cand_plan.score() > best_plan.score() + 1e-9:
                best_lists, best_plan = cand, cand_plan
                stale = 0
            else:
                stale += 1
                if stale >= 3:      # 连续几轮没进展就收手
                    break

        return {
            "greedy": {"lists": lists, "plan": g_plan, "evals": greedy_evals},
            "local": {"lists": best_lists, "plan": best_plan, "evals": self._evals},
            "total_evals": self._evals,
        }


# ==================== 硬约束校验器（与求解器独立实现） ====================

def verify(plan: Plan, cons: TripConstraints, pool: dict) -> dict:
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    stops = plan.stops

    # 1 时间窗
    bad = [f"{s.poi.name} 到 {hhmm(s.arrive)} 超出 {hhmm(s.poi.open_min)}–{hhmm(s.poi.close_min)}"
           for s in stops
           if s.arrive < s.poi.open_min - EPS or s.leave > s.poi.close_min + EPS]
    add("景点时间窗", not bad, "全部在开放时段内" if not bad else "；".join(bad[:4]))

    # 2 每日时长（含返程）——按当天自己的时间窗判
    bad = []
    last_back = cons.day_start
    for d in plan.days:
        if not d.stops:
            continue
        _, end_t = cons.window_for(d.index)
        back_at = d.stops[-1].leave + d.back_min
        if back_at > end_t + EPS:
            bad.append(f"第{d.index + 1}天 {hhmm(back_at)} 才回住宿（限 {hhmm(end_t)}）")
        if back_at > last_back:
            last_back = back_at
    add("每日时长上限", not bad,
        f"最晚一次回住宿 {hhmm(last_back)}" if not bad else "；".join(bad))

    # 3 预算（第 67-7 条：预算含机动，所以要拿「规划 + 机动」去比）
    _pocket = plan.pocket
    _grand = plan.total_cost + _pocket
    add("总预算", (not cons.budget) or _grand <= cons.budget + EPS,
        (f"规划 {plan.total_cost:.0f} + 机动 {_pocket:.0f} = {_grand:.0f} / 预算 {cons.budget:.0f} 元"
         + ("" if (not cons.budget or _grand <= cons.budget + EPS)
            else f"（超 {_grand - cons.budget:.0f}）"))
        if cons.budget else f"预计 {_grand:.0f} 元（没设预算）")

    # 4 不重复
    ids = [s.poi.id for s in stops]
    add("景点不重复", len(ids) == len(set(ids)),
        f"{len(ids)} 个停留，去重后 {len(set(ids))} 个")

    # 5 单点游览时长
    bad = [s.poi.name for s in stops if s.leave - s.arrive < s.poi.visit_min - EPS]
    add("游览时长充足", not bad, "每个点都留够了建议时长" if not bad else "不足：" + "、".join(bad))

    # 6 等待上限
    bad = [f"{s.poi.name} 干等 {s.wait_min} 分钟" for s in stops if s.wait_min > cons.max_wait_min]
    longest = max([s.wait_min for s in stops] or [0])
    add("等待不超限", not bad,
        f"单次最长等 {longest} 分钟（上限 {cons.max_wait_min}）" if not bad else "；".join(bad))

    # 7 每天回到住宿点
    if cons.day_windows:
        detail = "；".join(
            f"第{i + 1}天 {hhmm(cons.window_for(i)[0])}–{hhmm(cons.window_for(i)[1])}"
            + (f"（{cons.day_city[i]}）" if i < len(cons.day_city) else "")
            for i in range(len(cons.day_windows))
        )
    else:
        detail = f"每天 {hhmm(cons.day_start)} 从{cons.hotel_name or '住宿点'}出发，末站后返程"
    add("每日往返住宿", True, detail)

    # 8 每日体力：点数和累计游览时长
    bad = []
    for d in plan.days:
        if not d.stops:
            continue
        mins = sum(s.poi.visit_min for s in d.stops)
        if len(d.stops) > cons.max_stops_per_day:
            bad.append(f"第{d.index + 1}天 {len(d.stops)} 个点（上限 {cons.max_stops_per_day}）")
        elif mins > cons.max_visit_min_per_day:
            bad.append(f"第{d.index + 1}天玩了 {mins} 分钟（上限 {cons.max_visit_min_per_day}）")
    add("每日体力", not bad,
        f"每天不超过 {cons.max_stops_per_day} 个点、{cons.max_visit_min_per_day} 分钟" if not bad else "；".join(bad))

    # 9 必去景点一个都不能少
    got = {s.poi.id for s in stops}
    missing = [n for i, n in zip(cons.must_ids, cons.must_visit) if i not in got]
    add("必去景点", not missing,
        f"{len(cons.must_ids)} 个必去点都排进去了" if not missing and cons.must_ids
        else ("都排进去了" if not missing else "没排进：" + "、".join(missing)))

    # 10 闭馆日（有日期的时候才判）
    wk_cn = "一二三四五六日"
    bad = []
    for d in plan.days:
        wd = cons.weekday_for(d.index)
        if wd is None:
            continue
        for s in d.stops:
            if s.poi.closed_on(wd):
                bad.append(f"第{d.index + 1}天周{wk_cn[wd]}排了 {s.poi.name}（当天闭馆）")
    has_weekday = any(cons.weekday_for(d.index) is not None for d in plan.days)
    add("闭馆日", not bad,
        ("没有景点的闭馆日与日期冲突" if not bad else "；".join(bad[:4])) if has_weekday
        else "没给出发日期，闭馆日没判")

    # 11 逐日时间窗：第 1 天不早于到达、最后一天不晚于返程
    bad = []
    for d in plan.days:
        if not d.stops:
            continue
        w0, w1 = cons.window_for(d.index)
        first = d.stops[0]
        if first.arrive < w0 - EPS:
            bad.append(f"第{d.index + 1}天 {hhmm(first.arrive)} 就出发了（窗口 {hhmm(w0)} 起）")
        back_at = d.stops[-1].leave + d.back_min
        if back_at > w1 + EPS:
            bad.append(f"第{d.index + 1}天 {hhmm(back_at)} 才回（窗口 {hhmm(w1)} 止）")
    add("逐日时间窗", not bad,
        "每天的首末时间都落在自己的窗口里" if not bad else "；".join(bad[:4]))

    passed = sum(1 for c in checks if c["ok"])
    return {
        "checks": checks,
        "passed": passed,
        "total": len(checks),
        "pass_rate": round(passed / len(checks) * 100, 1),
    }
