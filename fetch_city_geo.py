"""
把全国城市 + 区县的中心坐标拉下来写进 data/city_geo.json。

为什么要有区县这一层（2026-11-08 老大提的）：
  用户可能填「天河」这种区名，但高铁枢纽都在市辖区中心，区县没有独立枢纽。
  所以存一张「区县 → 所属市」的映射，查坐标时先查市、查不到再查区县，查到区县就用上级市的坐标。

用 subdistrict=2 一次把「省 → 市 → 区县」两层拿下来，34 次请求就够。
"""
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\bishe-demo")
GEO = ROOT / "data" / "city_geo.json"
KEY = (ROOT / "data" / "amap_key.txt").read_text(encoding="utf-8").strip()
BASE = "https://restapi.amap.com/v3/config/district"


def get(params):
    url = BASE + "?" + urllib.parse.urlencode({**params, "key": KEY})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def center_of(node):
    c = (node or {}).get("center") or ""
    if not c or "," not in c:
        return None
    lng, lat = c.split(",")[:2]
    try:
        return [round(float(lat), 4), round(float(lng), 4)]
    except ValueError:
        return None


def norm(name: str) -> str:
    name = (name or "").strip()
    return name[:-1] if name.endswith("市") else name


def strip_suffix(name: str) -> str:
    """「天河区」→「天河」、「昆山市」→「昆山」、「长白县」→「长白」。"""
    name = (name or "").strip()
    for suf in ("区", "县", "市", "旗"):
        if name.endswith(suf) and len(name) > 2:
            return name[:-1]
    return name


print("拉全国省级区划…")
r = get({"keywords": "中国", "subdistrict": "1"})
if r.get("status") != "1":
    raise SystemExit("接口失败：" + str(r.get("info")))
provinces = r["districts"][0].get("districts") or []
print("省级:", len(provinces))

cities, districts = {}, {}
municipalities = {"北京市", "上海市", "天津市", "重庆市"}

for p in provinces:
    pname = p.get("name") or ""
    if pname in municipalities:
        mshort = pname.replace("市", "")
        c = center_of(p)
        if c:
            cities[mshort] = c
        # 直辖市的区县要单独拉（「徐汇」→ 上海）。
        # ⚠ 高德对直辖市多一层「上海城区」，要 subdistrict=2 才拿得到各区。
        try:
            rm = get({"keywords": pname, "subdistrict": "2"})
            time.sleep(0.35)
            for mid in (rm.get("districts") or [{}])[0].get("districts") or []:
                for d in (mid.get("districts") or []):
                    dname = (d.get("name") or "").strip()
                    if not dname:
                        continue
                    districts.setdefault(dname, mshort)
                    short = strip_suffix(dname)
                    if short != dname:
                        districts.setdefault(short, mshort)
        except Exception as e:
            print("  直辖市区县跳过", pname, e)
        continue
    try:
        r2 = get({"keywords": pname, "subdistrict": "2"})
        time.sleep(0.35)
    except Exception as e:
        print("  跳过", pname, e)
        continue
    if r2.get("status") != "1" or not r2.get("districts"):
        continue
    subs = r2["districts"][0].get("districts") or []
    n_dist = 0
    for s in subs:
        cname = (s.get("name") or "").strip()
        cc = center_of(s)
        if cname and cc:
            cities[norm(cname)] = cc
        # 区县层
        for d in (s.get("districts") or []):
            dname = (d.get("name") or "").strip()
            if not dname:
                continue
            parent = strip_suffix(cname) or norm(cname)
            districts.setdefault(dname, parent)          # 「天河区」→ 广州
            short = strip_suffix(dname)                   # 「天河区」→「天河」
            if short and short != dname:
                districts.setdefault(short, parent)       # 「天河」→ 广州
            n_dist += 1
    print(f"  {pname}: {len(subs)} 市 / {n_dist} 区县")

print("市:", len(cities), "| 区县:", len(districts))

old = {}
if GEO.exists():
    try:
        old = json.loads(GEO.read_text(encoding="utf-8"))
    except Exception:
        old = {}
old_cities = old.get("cities") or {}

merged = dict(old_cities)
added = 0
for k, v in cities.items():
    if k in merged:
        continue
    merged[k] = v
    added += 1

out = {
    "_note": "城市中心坐标（WGS84，[纬度, 经度]），只用于城际交通估算。"
             "cities 是市级，districts 是「区县 → 所属市」，填区县时按上级市算。",
    "cities": dict(sorted(merged.items(), key=lambda kv: kv[0])),
    "districts": dict(sorted(districts.items(), key=lambda kv: kv[0])),
}
GEO.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"新增城市 {added} 个，合并后 {len(merged)} 个；区县 {len(districts)} 个 → {GEO}")

for probe in ["天河", "徐汇", "昆山", "敦煌", "徐州"]:
    print(f"  抽样 {probe}: city={merged.get(probe)} district→{districts.get(probe)}")
