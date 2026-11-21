# -*- coding: utf-8 -*-
"""第 58 条：给每个城市的主车站查一份坐标，存 data/cities/<城市>/stations.json。

站名来自 fetch_trains.CITY_STATION——跟抓 12306 车次时用的是同一批站，
所以「到站 → 酒店」这条链上的站名和坐标是同一套口径。

坐标走高德 /v3/place/text，返回的是 GCJ-02，跟城市里的 pois.json / hotel 一致，
可以直接用 travel 矩阵那套算距离（city_geo.json 的城市中心是 WGS84，别混用）。

用法：
    python fetch_stations.py            # 缺哪个补哪个
    python fetch_stations.py --force    # 全部重查
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import amap_client
from fetch_trains import CITY_STATION

CITY_DIR = ROOT / "data" / "cities"


def _pick(pois: list, station: str):
    """从高德返回里挑一条：优先火车站/交通枢纽类型，其次第一条有坐标的。"""
    fallback = None
    for p in pois:
        loc = (p.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        ptype = p.get("type") or ""
        name = p.get("name") or ""
        if station.translate(str.maketrans("", "")) in name and ("火车站" in ptype or "交通" in ptype):
            return p
        if ("火车站" in ptype or "交通" in ptype) and fallback is None:
            fallback = p
        elif fallback is None:
            fallback = p
    return fallback


def fetch_one(city: str, station: str, code: str) -> tuple[dict | None, str]:
    try:
        pois = amap_client.place_text(station, city, pages=1, offset=10)
    except amap_client.AmapError as exc:
        return None, f"{city} {station}：高德查询失败（{exc}）"
    picked = _pick(pois, station)
    if picked is None:
        return None, f"{city} {station}：高德没查到"
    lng, lat = (float(x) for x in picked["location"].split(","))
    return {
        "name": picked.get("name") or station,
        "query": station,
        "code": code,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "address": picked.get("address") or "",
        "source": "amap",
        "datum": "gcj02",
    }, ""


def main() -> int:
    force = "--force" in sys.argv
    done = skipped = failed = 0
    for city, (station, code) in CITY_STATION.items():
        out_dir = CITY_DIR / city
        out_path = out_dir / "stations.json"
        if out_path.exists() and not force:
            skipped += 1
            continue
        rec, err = fetch_one(city, station, code)
        time.sleep(0.25)
        if rec is None:
            failed += 1
            print("  ✗", err)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "_note": f"{city}主要车站坐标（GCJ-02，来自高德 place/text）。"
                     f"「到站 → 酒店」要算路程时用这个，别拿城市中心坐标。",
            "_source": "amap /v3/place/text",
            "_datum": "gcj02",
            "city": city,
            "stations": [rec],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        done += 1
        print(f"  ✓ {city}：{rec['name']} ({rec['lng']}, {rec['lat']}) {rec['address']}")
    print(f"\n新写 {done} 个，跳过已有 {skipped} 个，失败 {failed} 个")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
