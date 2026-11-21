# -*- coding: utf-8 -*-
"""第 47 条第一步：给每个景点查一份海拔，用来算「相对城市中心爬升了多少」。

为什么不是直接拿海拔当体力：
    拉萨全城都在 3650 米，海拔高不代表要爬山；
    北京香山才 557 米，可城区只有 43 米，从下往上爬 500 米，腿是真酸。
    所以真正有意义的是 **景点海拔 − 城市中心海拔** 的落差。

数据源：OpenTopoData（SRTM 90m 全球高程，免 key，1 次/秒、100 点/次）
    https://api.opentopodata.org/v1/srtm90m?locations=lat,lng|lat,lng|...

坐标说明：POI 是 GCJ-02，这里是直接拿去查的——500 米偏移对 90 米分辨率的高程
数据通常只差十几米，够用；真要在悬崖边上抠精度再转 WGS84。

产出：data/cities/<城市>/elevation.json
    {"city": "北京", "city_elev": 43.5, "source": "opentopodata/srtm90m",
     "pois": {"am01": 52.3, ...}, "pois_rise": {"am01": 8.8, ...}}

用法：
    python fetch_elevation.py            # 缺哪个补哪个
    python fetch_elevation.py --force    # 全部重查
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CITIES_DIR = ROOT / "data" / "cities"
GEO = ROOT / "data" / "city_geo.json"
API = "https://api.opentopodata.org/v1/srtm90m"
BATCH = 100          # 一次最多 100 个点
SLEEP = 1.15         # 免费额度 1 次/秒，留点余量


def query(points: list[tuple[float, float]]) -> list[float | None]:
    """一批点查海拔，返回等长列表（查不到的给 None）。"""
    locs = "|".join(f"{lat:.6f},{lng:.6f}" for lat, lng in points)
    url = API + "?locations=" + urllib.parse.quote(locs, safe="|,.-")
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") != "OK":
                raise RuntimeError(data.get("error") or data.get("status"))
            out = []
            for it in data.get("results") or []:
                e = it.get("elevation")
                out.append(float(e) if e is not None else None)
            # 补齐（接口偶尔少给）
            while len(out) < len(points):
                out.append(None)
            return out
        except Exception as exc:      # noqa: BLE001 - 网络抖动就重试
            last = exc
            time.sleep(2 + attempt * 2)
    print(f"    ! 这批查失败：{last}")
    return [None] * len(points)


def city_centers() -> dict:
    raw = json.loads(GEO.read_text(encoding="utf-8"))
    return {k: (float(v[0]), float(v[1])) for k, v in (raw.get("cities") or {}).items()}


def main() -> int:
    force = "--force" in sys.argv
    centers = city_centers()
    total_ok = total_bad = 0
    for city_dir in sorted(CITIES_DIR.iterdir()):
        if not city_dir.is_dir():
            continue
        pf = city_dir / "pois.json"
        if not pf.exists():
            continue
        city = city_dir.name
        out_path = city_dir / "elevation.json"
        if out_path.exists() and not force:
            print(f"  = {city} 已有，跳过")
            continue
        pois = json.loads(pf.read_text(encoding="utf-8")).get("pois") or []
        if not pois:
            continue
        pts = [(float(p["lat"]), float(p["lng"])) for p in pois]
        ids = [p.get("id") or p.get("name") for p in pois]

        elev: dict = {}
        for i in range(0, len(pts), BATCH):
            chunk = pts[i:i + BATCH]
            got = query(chunk)
            for j, e in enumerate(got):
                elev[ids[i + j]] = e
            time.sleep(SLEEP)

        # 城市中心：拿城市中心海拔当「基准线」
        c = centers.get(city)
        city_elev = None
        if c:
            time.sleep(SLEEP)
            got = query([c])
            city_elev = got[0] if got else None
        if city_elev is None:
            vals = [v for v in elev.values() if v is not None]
            city_elev = sorted(vals)[len(vals) // 2] if vals else 0.0   # 兜底：用中位数
            print(f"    ! {city} 城市中心没查到，用本城景点中位数 {city_elev:.0f} m 当基准")

        rise = {k: (round(v - city_elev, 1) if v is not None else None)
                for k, v in elev.items()}
        ok = sum(1 for v in elev.values() if v is not None)
        total_ok += ok
        total_bad += len(elev) - ok
        out_path.write_text(json.dumps({
            "_note": "景点海拔（SRTM 90m）与相对城市中心的爬升。第 47 条：体力看的是 rise，不是 alt。",
            "_source": "opentopodata.org /v1/srtm90m",
            "city": city,
            "city_elev": round(city_elev, 1),
            "alt": elev,
            "rise": rise,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  ✓ {city}：{ok} 个点有海拔，城市基准 {city_elev:.0f} m，"
              f"最大爬升 {max((v for v in rise.values() if v is not None), default=0):.0f} m")
    print(f"\n共查到 {total_ok} 个点，失败 {total_bad} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
