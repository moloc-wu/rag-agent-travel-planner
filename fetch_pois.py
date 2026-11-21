"""
fetch_pois —— 从高德拉真实 POI + 真实驾车距离矩阵，落成 demo 能吃的 JSON。

用法：
    python fetch_pois.py                        # 拉默认城市（data/cities.json 里登记的）
    python fetch_pois.py --city 北京
    python fetch_pois.py --city 成都 --hotel 春熙路 --limit 30
    python fetch_pois.py --city 武汉 --no-matrix

产出（每个城市一个目录，互不覆盖）：
    data/cities/<城市>/pois.json     候选点（GCJ-02 坐标，带评分/营业时间）
    data/cities/<城市>/matrix.json   N×N 驾车距离与时长（实测，非直线估算）
    data/cities.json                 索引：有哪些城市、默认是哪个

住宿点不再写死某一个广场：优先用 --hotel 指定的地方，否则拿城市中心点。
门票高德不给，用一张通用对照表兜底；某个城市要补，就写
data/cities/<城市>/tickets.json（{"景点名关键字": 价格}），会覆盖通用表。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import amap_client as amap
import cities as city_index

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HOTEL_ID = "__hotel__"

# 关键词 → 用来把检索结果铺满各类景点
KEYWORDS = ["景点", "博物馆", "美术馆", "公园", "老街", "游乐场", "寺庙"]
KEYWORD_PAGES = {"景点": 2}

# 类型判定：顺序敏感，先匹配到的说了算（越具体越靠前）
TYPE_RULES = [
    (("博物馆", "博物院", "展览馆", "纪念馆", "陈列馆"), ["历史文化"], 150),
    (("美术馆", "艺术馆", "艺术中心", "画廊"), ["艺术"], 90),
    (("游乐", "乐园", "世界之窗", "海底世界", "海洋馆", "海洋王国", "动物园", "水上乐园", "zoolung"), ["亲子"], 180),
    (("寺", "庙", "阁", "书院", "故居", "旧址", "古城", "古镇", "祠", "遗址"), ["历史文化"], 90),
    (("街区", "老街", "步行街", "广场", "商圈", "市集", "巷"), ["美食", "夜景"], 90),
    (("公园", "湿地", "植物园", "湖", "山", "岭", "岛", "景区", "风景名胜", "洲", "森林", "园"), ["自然风光"], 150),
]

# 高德不给门票价，人工补一张通用对照表（按名字关键字匹配，任何城市都适用）
TICKET = {
    # 高德不给票价，这里按名字关键字给个常见价：越具体的写在越前面，先匹配到先算。
    # 想给某个城市补准，写 data/cities/<城市>/tickets.json 覆盖（同样的 关键字: 价格）。
    "故宫": 60, "颐和园": 30, "圆明园": 10, "天坛": 15, "恭王府": 40, "长城": 45,
    "世界之窗": 200, "海底世界": 150, "海洋王国": 180, "海洋馆": 160, "动物园": 120,
    "游船": 128, "漓江": 210,
    "书院": 50, "古城": 60, "古镇": 60, "石窟": 80, "遗址": 40,
    "开福寺": 10, "江阁": 30, "阁": 25, "寺": 20, "庙": 20,
    "陵": 50, "城墙": 60, "塔": 30,
    "博物馆": 0, "纪念馆": 0, "美术馆": 0, "展览馆": 0,
}

# 住宿点怎么定：市中心附近评分最高的酒店。太远的不考虑，太烂的不考虑。
HOTEL_MAX_KM = 4.0
HOTEL_MIN_RATING = 4.5
LODGING_WORDS = ("酒店", "宾馆", "饭店", "客栈", "民宿", "度假村", "公寓式")

BAD_WORDS = ("停车场", "售票", "入口", "出口", "厕所", "服务中心", "派出所", "公交", "地铁站",
             "酒店", "宾馆", "民宿", "客栈", "旅馆", "公寓", "售楼", "地产", "写字楼")


def loc_of(poi):
    raw = poi.get("location") or ""
    if "," not in raw:
        return None
    lng, lat = raw.split(",")[:2]
    return float(lng), float(lat)


def km_between(a, b):
    """a, b 是 (lng, lat)；返回公里。"""
    lng1, lat1 = a
    lng2, lat2 = b
    r1, r2 = math.radians(lat1), math.radians(lat2)
    dp, dl = r2 - r1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def parse_window(biz):
    """从 biz_ext 里抠出 开门/关门 分钟数。有多季/多段营业时间的，取第一段。"""
    raw = f"{biz.get('open_time') or ''} {biz.get('opentime2') or ''}"
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-–—~至]\s*(\d{1,2}):(\d{2})", raw)
    if m:
        o = int(m.group(1)) * 60 + int(m.group(2))
        c = int(m.group(3)) * 60 + int(m.group(4))
        if c <= o:
            c = 1440
        return max(0, min(o, 1439)), max(o + 30, min(c, 1440))
    return 0, 1440          # 查不到就按全天开放


def classify(name):
    for words, tags, visit in TYPE_RULES:
        if any(w in name for w in words):
            return list(tags), visit
    return ["历史文化"], 90


def load_tickets(city: str) -> dict:
    """通用门票表 + 该城市的覆盖表（如果写了 tickets.json）。"""
    table = dict(TICKET)
    path = city_index.city_dir(city) / "tickets.json"
    if path.exists():
        try:
            table.update({str(k): float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()})
            print(f"  门票覆盖表：{path.name}（{len(json.loads(path.read_text(encoding='utf-8')))} 条）")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"  ! tickets.json 读不了，用通用表：{exc}")
    return table


def ticket_of(name, table: dict) -> float:
    for k, v in table.items():
        if k in name:
            return float(v)
    return 0.0


WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 高德 biz_ext.opentime2 里会直接写「周一全天关闭」「周一闭馆」这类话
CLOSED_PAT = re.compile(r"周([一二三四五六日天])(?:全天)?(?:关闭|闭馆|不开放|休息|停馆)")


def parse_closed_days(biz):
    """
    从营业时间文本里抠出「哪几天闭馆」。返回 [0-6]（0 = 周一），没有就是空列表。

    实测原文形如：故宫「旺季 4 月 1 日至 10 月 31 日 周二至周日 08:30-17:00…周一全天关闭」。
    ⚠ 只认「周X + 关闭/闭馆」这种明确说法，「周一至周日 08:00-18:00」是开放不是闭馆，不会误判。
    """
    raw = f"{biz.get('open_time') or ''} {biz.get('opentime2') or ''}"
    days = set()
    for m in CLOSED_PAT.finditer(raw):
        days.add(WEEKDAY_MAP[m.group(1)])
    return sorted(days)


# 名称规则兜底：老的 pois.json 没有闭馆字段，靠名字推断
MUSEUM_WORDS = ("博物馆", "美术馆", "纪念馆", "展览馆", "科技馆", "艺术馆", "规划馆")


def infer_closed_days(name: str, category: str = ""):
    """博物馆类场馆国内绝大多数周一闭馆——数据里查不到时按这条推断。"""
    blob = f"{name} {category}"
    if any(w in blob for w in MUSEUM_WORDS):
        return [0]
    return []


def normalize(poi, city, tickets):
    name = (poi.get("name") or "").strip()
    if not name or any(w in name for w in BAD_WORDS):
        return None
    if (poi.get("typecode") or "").startswith("10"):     # 住宿服务，不是景点
        return None
    ll = loc_of(poi)
    if not ll:
        return None
    biz = poi.get("biz_ext") or {}
    try:
        rating = float(biz.get("rating") or 4.5)
    except (TypeError, ValueError):
        rating = 4.5
    tags, visit = classify(name)
    if rating >= 4.85 and "网红打卡" not in tags:
        tags.append("网红打卡")
    open_min, close_min = parse_window(biz)
    closed = parse_closed_days(biz)
    return {
        "id": poi.get("id") or name,
        "name": name,
        "category": (poi.get("type") or "").split(";")[-1] or "景点",
        "tags": tags,
        "lng": round(ll[0], 6),
        "lat": round(ll[1], 6),
        "open": f"{open_min // 60:02d}:{open_min % 60:02d}",
        "close": f"{close_min // 60:02d}:{close_min % 60:02d}",
        "closed_days": closed,          # 闭馆日（0 = 周一），空数组表示每天都开
        "visit_min": visit,
        "ticket": ticket_of(name, tickets),
        "base": round(max(20.0, min(100.0, 30 + (rating - 4.0) * 55)), 1),
        "_rating": rating,
        "_open_min": open_min,
        "_close_min": close_min,
        "_city": city,
    }


def _prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def dedupe(items):
    """350 米内、或名字共享 3 字以上前缀且 1.5 km 内，视为同一个点，留评分高（同分留检索靠前）的。"""
    items = sorted(items, key=lambda x: (-x["_rating"], x["_rank"]))
    kept = []
    for it in items:
        dup = False
        for k in kept:
            d = km_between((it["lng"], it["lat"]), (k["lng"], k["lat"]))
            short, long_ = (it["name"], k["name"]) if len(it["name"]) <= len(k["name"]) else (k["name"], it["name"])
            if d < 0.35:
                dup = True
                break
            if d < 1.5 and (short in long_ or _prefix_len(it["name"], k["name"]) >= 3):
                dup = True
                break
        if not dup:
            kept.append(it)
    return kept


# 高德免费接口不给房价（lowest_price 和 cost 都是空数组，要付费权限），
# 按酒店类型估一个常见价。以后接真实房价，改这个函数就行。
HOTEL_PRICE_RULES = [
    (("五星", "100102"), 800.0, "高档"),
    (("四星", "100103"), 500.0, "舒适"),
    (("三星", "100104"), 350.0, "舒适"),
    (("经济", "100105"), 180.0, "经济"),
    (("民宿", "100108"), 260.0, "民宿"),
]
HOTEL_DEFAULT_PRICE = 300.0
HOTEL_DEFAULT_LEVEL = "商务"
HOTEL_LEVELS = ("经济", "商务", "舒适", "高档", "民宿")


def hotel_price(p):
    """按类型/类型码估一晚房价和档位。"""
    tag = f"{p.get('type') or ''}|{p.get('typecode') or ''}"
    for words, price, level in HOTEL_PRICE_RULES:
        if any(w in tag for w in words):
            return price, level
    return HOTEL_DEFAULT_PRICE, HOTEL_DEFAULT_LEVEL


def city_center(city: str):
    geo = amap.geocode(city, city=city)
    if geo:
        return (geo[0]["lng"], geo[0]["lat"])
    return None


def fetch_hotels(city: str, limit: int = 15, radius_km: float = 12.0) -> list:
    """
    拉一批候选酒店：离市中心不远、评分不低，按档位铺开，保证「贵/便宜」有得选。
    酒店不是景点，不进候选池（候选池只放玩的地方），但它是每天行程的起点和终点。
    """
    center = city_center(city)
    if not center:
        return []
    raw = list(amap.place_text("酒店", city, pages=3))
    raw += amap.place_text("五星级酒店", city, pages=1)   # 综合排序偏中低价，高档的单独补一次
    cands, seen = [], set()
    for p in raw:
        name = (p.get("name") or "").strip()
        ll, tcode = loc_of(p), (p.get("typecode") or "")
        if not name or not ll or not tcode.startswith("10"):   # 10xxxx 是住宿服务
            continue
        key = name.replace(" ", "")
        if key in seen:
            continue
        seen.add(key)
        d = km_between(ll, center)
        if d > radius_km:
            continue
        try:
            rating = float((p.get("biz_ext") or {}).get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        if rating < 4.0:
            continue
        price, level = hotel_price(p)
        cands.append({
            "name": name,
            "lng": round(ll[0], 6), "lat": round(ll[1], 6),
            "rating": round(rating, 1), "price": price, "level": level,
            "km": round(d, 1),
        })

    # 每档取评分最高的几家（同分取离市中心近的），让"经济/舒适/高档"都有货
    per = max(2, limit // len(HOTEL_LEVELS) + 1)
    out, used = [], set()
    for level in HOTEL_LEVELS:
        group = [c for c in cands if c["level"] == level]
        group.sort(key=lambda x: (-x["rating"], x["km"]))
        for c in group[:per]:
            if c["name"] in used or len(out) >= limit:
                continue
            used.add(c["name"])
            out.append(c)
    return out


def pick_hotel(city: str, want: str | None = None) -> dict:
    """
    选住宿点：--hotel 指定的地方 > 市中心附近评分最高的酒店 > 城市中心。
    酒店不是景点，不进候选池（candidate pool 里只有玩的地方），但它决定每天的起终点。
    """
    if want:
        for p in amap.place_text(want, city, pages=1):
            ll, name = loc_of(p), (p.get("name") or "").strip()
            if ll and name:
                return {"name": f"{name}（住宿点）", "lng": round(ll[0], 6), "lat": round(ll[1], 6)}
        print(f"  ! 高德没搜到「{want}」，改去市中心找酒店")

    center = city_center(city)

    if center:
        best = None
        pool = amap.place_text("酒店", city, pages=2)
        for p in pool:
            name, ll = (p.get("name") or "").strip(), loc_of(p)
            if not name or not ll:
                continue
            if not any(w in name for w in LODGING_WORDS):
                continue
            d = km_between(ll, center)
            if d > HOTEL_MAX_KM:
                continue
            try:
                rating = float((p.get("biz_ext") or {}).get("rating") or 0)
            except (TypeError, ValueError):
                rating = 0.0
            if rating < HOTEL_MIN_RATING:
                continue
            score = rating - 0.25 * d          # 同样评分，离市中心越近越优先
            if best is None or score > best[0]:
                best = (score, rating, d, {"name": f"{name}（住宿点）",
                                           "lng": round(ll[0], 6), "lat": round(ll[1], 6)})
        if best:
            _, rating, d, hotel = best
            print(f"  住宿点挑中酒店：{hotel['name']}（评分 {rating:.1f}，离市中心 {d:.1f} km，"
                  f"共看了 {len(pool)} 家）")
            return hotel
        print(f"  ! 市中心 {HOTEL_MAX_KM:.0f} km 内没有 {HOTEL_MIN_RATING:.1f} 分以上的酒店，退回城市中心")

    if center:
        return {"name": f"{city}市中心（住宿点）", "lng": round(center[0], 6), "lat": round(center[1], 6)}

    for p in amap.place_text("市中心", city, pages=1):
        ll = loc_of(p)
        if ll:
            return {"name": f"{city}市中心（住宿点）", "lng": round(ll[0], 6), "lat": round(ll[1], 6)}

    raise SystemExit(f"没能在「{city}」找到可用的住宿点，用 --hotel 手动指定一个地名吧")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=None, help="城市名，不给就用索引里的默认城市")
    ap.add_argument("--hotel", default=None, help="住宿点地名，不给就用城市中心")
    ap.add_argument("--limit", type=int, default=24)
    ap.add_argument("--radius", type=float, default=20.0, help="离住宿点超过这个公里数就不进候选池")
    ap.add_argument("--no-matrix", action="store_true", help="跳过距离矩阵（省配额）")
    args = ap.parse_args()

    city = city_index.canonical(args.city or city_index.default_city())
    if not city:
        raise SystemExit("没说城市，索引里也没有默认城市：python fetch_pois.py --city 长沙")
    print(f"== 拉 {city} 的 POI ==")

    raw = []
    for kw in KEYWORDS:
        got = amap.place_text(kw, city, pages=KEYWORD_PAGES.get(kw, 1))
        print(f"  {kw:<6} {len(got):>3} 条")
        raw.extend(got)

    tickets = load_tickets(city)
    items = []
    for i, p in enumerate(raw):
        it = normalize(p, city, tickets)
        if it:
            it["_rank"] = i          # 高德检索顺序即相关度，用来给同分的点排序
            items.append(it)
    items = dedupe(items)
    print(f"  去重后 {len(items)} 条")

    prev = city_index.get(city) or {}
    hotel = pick_hotel(city, args.hotel)
    if not args.hotel and (prev.get("hotel") or {}).get("name"):
        old = prev["hotel"]
        if old.get("lat") and old.get("lng"):
            hotel = old                    # 之前定过的住宿点就沿用，不用每次重新猜
            print(f"  沿用上次的住宿点 {old.get('name')}")
    print(f"  住宿点 {hotel['name']} @ {hotel['lng']},{hotel['lat']}")

    # 住宿点本身、离住宿太近的、以及远到不像市内一日游的点，都不进候选池
    def dist_h(it):
        return km_between((it["lng"], it["lat"]), (hotel["lng"], hotel["lat"]))

    before = len(items)
    items = [it for it in items if 0.30 < dist_h(it) <= args.radius]
    print(f"  按半径 {args.radius:.0f} km 过滤：{before} → {len(items)} 条")

    items.sort(key=lambda x: (-x["_rating"], x["_rank"]))
    kept = []
    for it in items:
        if len(kept) >= args.limit:
            break
        if all(km_between((it["lng"], it["lat"]), (k["lng"], k["lat"])) > 0.25 for k in kept):
            kept.append(it)

    pois = []
    for i, it in enumerate(kept, 1):
        clean = {k: v for k, v in it.items() if not k.startswith("_")}
        clean["id"] = f"am{i:02d}"
        pois.append(clean)

    out_dir = city_index.city_dir(city)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "_note": f"高德 Web 服务实时检索（{city}）：坐标 GCJ-02，评分/营业时间来自 biz_ext。"
                 "门票为对照表兜底，游览时长为按类型估算。",
        "_datum": "gcj02",
        "_source": "amap",
        "city": city,
        "hotel": hotel,
        "pois": pois,
    }
    pois_path = out_dir / "pois.json"
    pois_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rel_pois = str(pois_path.relative_to(DATA)).replace("\\", "/")
    print(f"  写入 {rel_pois}（{len(pois)} 个点）")
    for p in pois:
        print(f"    {p['name'][:16]:<18} {p['open']}-{p['close']} 门票{p['ticket']:>4.0f}  热度{p['base']:>5.1f}  {'/'.join(p['tags'])}")

    # 候选酒店单独存一份：它们不进候选池，但决定每天的起终点和住宿费
    hotels = fetch_hotels(city)
    hotels_path = out_dir / "hotels.json"
    if hotels:
        hotels_path.write_text(json.dumps({"_note": "高德检索 + 按类型估算房价（高德免费接口不返回房价）",
                                           "city": city, "hotels": hotels},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  写入 {hotels_path.relative_to(DATA)}（{len(hotels)} 家酒店）")
        for h in hotels:
            print(f"    {h['name'][:20]:<22} {h['level']} ¥{h['price']:>4.0f}/晚  评分{h['rating']}  {h['km']}km")
    else:
        print("  ! 没拉到候选酒店，住宿只能用默认点")

    rel_matrix = ""
    if not args.no_matrix:
        print(f"== 算驾驶距离矩阵（{len(pois) + 1} × {len(pois) + 1}）==")
        points = {HOTEL_ID: f"{hotel['lng']},{hotel['lat']}"}
        points.update({p["id"]: f"{p['lng']},{p['lat']}" for p in pois})
        matrix = amap.distance_matrix(points)
        matrix_path = out_dir / "matrix.json"
        matrix_path.write_text(json.dumps(matrix, ensure_ascii=False), encoding="utf-8")
        rel_matrix = str(matrix_path.relative_to(DATA)).replace("\\", "/")
        n = sum(len(v) for v in matrix.values())
        print(f"  写入 {rel_matrix}（{n} 条 OD）")
    elif (out_dir / "matrix.json").exists():
        rel_matrix = str((out_dir / "matrix.json").relative_to(DATA)).replace("\\", "/")
        print("  保留上一版距离矩阵")

    entry = city_index.register(city, pois_rel=rel_pois, matrix_rel=rel_matrix,
                                hotel=hotel, poi_count=len(pois))
    print(f"  已登记进 data/cities.json：{entry['name']}（共 "
          f"{len(city_index.load()['cities'])} 个城市）")


if __name__ == "__main__":
    main()
