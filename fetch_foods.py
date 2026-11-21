# -*- coding: utf-8 -*-
"""第 60 条第一步：按城市抓一批餐饮 POI，存 data/cities/<城市>/foods.json。

为什么单独抓：原来 22 个城市加起来只有 16 家餐饮点（长沙 0 家），
所以「午饭推荐」几乎不会出现，第 24 / 59 / 60 条都卡在这上面。

口径跟 pois.json 保持一致（GCJ-02、高德来源），字段对齐 poi 表，
`import_data.py` 读到 foods.json 就按 `is_meal=1` 入库。

用法：
    python fetch_foods.py                 # 22 个城市全抓
    python fetch_foods.py --city 长沙      # 只抓一个
    python fetch_foods.py --force          # 已有也重抓
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import amap_client as amap
import cities as city_index
from fetch_pois import km_between

CITY_DIR = ROOT / "data" / "cities"
# 三组关键词，取的不是同一批店：正餐 / 小吃 / 本地菜
KEYWORDS = ["餐厅", "小吃", "美食"]
PER_KEYWORD_PAGES = 2      # 每个词两页（25×2），够了

# 不是饭馆的东西：商场里的美食广场、食品公司、学校食堂……
BAD_WORDS = ("美食城", "美食广场", "食品", "食堂", "批发", "超市", "便利店")


def pick_price_level(poi) -> float:
    """人均价位：高德 biz_ext.cost 有就用，没有按 60 估（普通档）。"""
    biz = poi.get("biz_ext") or {}
    try:
        cost = float(biz.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    return cost if cost > 0 else 60.0


def normalize(poi, city):
    name = (poi.get("name") or "").strip()
    if not name or any(w in name for w in BAD_WORDS):
        return None
    typecode = str(poi.get("typecode") or "")
    if not typecode.startswith("05"):        # 05xxxx = 餐饮服务
        return None
    loc = (poi.get("location") or "").split(",")
    if len(loc) != 2:
        return None
    try:
        lng, lat = float(loc[0]), float(loc[1])
    except ValueError:
        return None
    biz = poi.get("biz_ext") or {}
    try:
        rating = float(biz.get("rating") or 4.3)
    except (TypeError, ValueError):
        rating = 4.3
    # category 取类型串最后一段：中餐厅 / 快餐 / 小吃 / 火锅店……
    category = (poi.get("type") or "").split(";")[-1] or "餐饮"
    return {
        "id": poi.get("id") or name,
        "name": name,
        "category": category,
        "tags": ["美食"],
        "lng": round(lng, 6),
        "lat": round(lat, 6),
        "avg_price": pick_price_level(poi),
        "base": round(max(20.0, min(100.0, 30 + (rating - 4.0) * 55)), 1),
        "_rating": rating,
        "_rank": 0,
    }


def dedupe(items):
    """同名 / 同 id / 挨太近的店只留一个，别让一家店刷满候选。"""
    seen, out = set(), []
    for it in items:
        key = (it["name"], it["id"])
        if key in seen:
            continue
        if any(km_between((it["lng"], it["lat"]), (k["lng"], k["lat"])) < 0.12
               for k in out):
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_city(city: str, limit: int = 40) -> list:
    raw = []
    for kw in KEYWORDS:
        try:
            got = amap.place_text(kw, city, pages=PER_KEYWORD_PAGES)
        except amap.AmapError as exc:
            print(f"    {kw} 抓取失败：{exc}")
            continue
        print(f"    {kw:<4} {len(got):>3} 条")
        raw.extend(got)
        time.sleep(0.2)

    items = []
    for i, p in enumerate(raw):
        it = normalize(p, city)
        if it:
            it["_rank"] = i
            items.append(it)
    items = dedupe(items)
    items.sort(key=lambda x: (-x["_rating"], x["_rank"]))
    kept = items[:limit]
    for i, it in enumerate(kept, 1):
        it["id"] = f"fd{i:02d}"
    return [{k: v for k, v in it.items() if not k.startswith("_")} for it in kept]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=None)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    names = [city_index.canonical(args.city)] if args.city else city_index.known_names()
    names = [n for n in names if n]
    total = 0
    for city in names:
        out_path = CITY_DIR / city / "foods.json"
        if out_path.exists() and not args.force:
            print(f"  = {city} 已有，跳过")
            continue
        print(f"  → {city}")
        items = fetch_city(city, args.limit)
        if not items:
            print(f"    ✗ {city} 一条都没抓到")
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_note": f"{city} 餐饮 POI（GCJ-02，高德 place/text 抓的）。"
                     f"`import_data.py` 会按 is_meal=1 入库，供三餐推荐和第 60 条选店用。",
            "_source": "amap /v3/place/text",
            "_datum": "gcj02",
            "city": city,
            "pois": items,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        total += len(items)
        print(f"    ✓ {len(items)} 家")
    print(f"\n共 {total} 家")
    return 0


if __name__ == "__main__":
    sys.exit(main())
