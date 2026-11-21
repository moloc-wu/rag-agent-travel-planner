"""amap_client —— 高德 Web 服务封装（只读查询）。key 从 data/amap_key.txt 或环境变量 AMAP_KEY 读。"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEYFILE = ROOT / "data" / "amap_key.txt"
BASE = "https://restapi.amap.com"


class AmapError(RuntimeError):
    pass


def load_key() -> str:
    k = os.getenv("AMAP_KEY", "").strip()
    if k:
        return k
    if KEYFILE.exists():
        return KEYFILE.read_text(encoding="utf-8").strip()
    raise AmapError("没找到高德 key：设环境变量 AMAP_KEY，或写进 data/amap_key.txt")


def _get(path: str, retries: int = 3, **params):
    key = load_key()
    params["key"] = key
    qs = urllib.parse.urlencode(params, safe="|,", encoding="utf-8")
    url = f"{BASE}{path}?{qs}"
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            if str(data.get("status")) != "1":
                raise AmapError(f"{path} 返回错误：{data.get('info')}（{data.get('infocode')}）")
            return data
        except Exception as exc:      # noqa: BLE001 - 网络抖动就重试
            last = exc
            time.sleep(0.6 * (i + 1))
    raise AmapError(f"请求失败：{path} -> {last}")


def place_text(keywords: str, city: str, pages: int = 1, offset: int = 25) -> list:
    """按关键词搜 POI，返回原始记录列表（extensions=all，带 biz_ext 评分/营业时间）。"""
    out = []
    for page in range(1, pages + 1):
        d = _get(
            "/v3/place/text",
            keywords=keywords,
            city=city,
            citylimit="true",
            offset=offset,
            page=page,
            extensions="all",
        )
        pois = d.get("pois") or []
        if not pois:
            break
        out.extend(pois)
        time.sleep(0.15)
    return out


def geocode(address: str, city: str | None = None) -> list:
    """地名 → 坐标。返回 [{"lng","lat","level","formatted_address"}]，空的表示没查到。"""
    params = {"address": address}
    if city:
        params["city"] = city
    d = _get("/v3/geocode/geo", **params)
    out = []
    for g in d.get("geocodes") or []:
        loc = (g.get("location") or "").split(",")
        if len(loc) != 2:
            continue
        try:
            out.append({
                "lng": float(loc[0]),
                "lat": float(loc[1]),
                "level": g.get("level") or "",
                "formatted_address": g.get("formatted_address") or "",
            })
        except ValueError:
            continue
    return out


def distance_matrix(points: dict, mode: int = 1) -> dict:
    """
    批量算驾车距离/时间。points: {id: "lng,lat"}，返回 {from_id: {to_id: [km, min]}}。
    高德 /v3/distance 一次只能一个终点，所以按终点轮询。
    """
    ids = list(points.keys())
    matrix = {a: {} for a in ids}
    origins = "|".join(points[i] for i in ids)
    for k, dest in enumerate(ids):
        d = _get("/v3/distance", origins=origins, destination=points[dest], type=mode)
        results = d.get("results") or []
        for r in results:
            i = int(r["origin_id"]) - 1
            km = round(float(r["distance"]) / 1000.0, 2)
            minutes = float(r["duration"]) / 60.0
            matrix[ids[i]][dest] = [km, minutes]
        time.sleep(0.12)
        if (k + 1) % 8 == 0:
            print(f"    距离矩阵 {k + 1}/{len(ids)}")
    # 对角线补零
    for a in ids:
        matrix[a][a] = [0.0, 0.0]
    return matrix
