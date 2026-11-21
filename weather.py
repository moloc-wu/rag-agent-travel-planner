"""
天气模块：用 **Open-Meteo** 免费预报（无需 key），超出预报范围退回本地历史平均。

为什么换掉和风的方案（2026-11-10）：
  · 和风要买 key，老大生活费还没发；
  · Open-Meteo 免费、不需要申请 key、覆盖全球、有降水概率百分比；
  · 2026-11-10 实测长沙 7 天正常返回。
  条款是「non-commercial 免费」，毕设 demo 够用；真要商用得换。

数据分两段：
  · 出发日起 16 天内：Open-Meteo 真实预报（WMO 天气码 + 降水概率）
  · 16 天以外：data/climate.json 里的历史同期平均，没有就标「未接入」
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
CLIMATE_PATH = DATA_DIR / "climate.json"

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 16          # Open-Meteo 免费档给到 16 天

# WMO 天气码 → 中文（前端直接显示这个）
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴伴冰雹",
}

# 哪些天气算「下雨/下雪」，排程时可以把户外点换室内
RAINY_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOWY_CODES = {71, 73, 75, 77, 85, 86}


def _geo(city: str):
    """城市坐标：复用城际交通那份表（区县会归到上级市）。"""
    import intercity
    return intercity.city_coord(city)


def _climate(city: str, month: int):
    """历史同期气候平均（本地表），没有就返回 None。"""
    try:
        raw = json.loads(CLIMATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    row = (raw.get("cities") or {}).get(city)
    if not row:
        return None
    return (row or {}).get(str(month))


def _open_meteo(lat: float, lng: float) -> dict:
    """拿未来 16 天的预报，返回 {date: {...}}。失败抛异常，由调用方降级。"""
    url = OPEN_METEO + "?" + urllib.parse.urlencode({
        "latitude": f"{lat:.4f}",
        "longitude": f"{lng:.4f}",
        "daily": ("weathercode,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max"),
        "timezone": "Asia/Shanghai",
        "forecast_days": FORECAST_DAYS,
    })
    with urllib.request.urlopen(url, timeout=15) as r:
        raw = json.loads(r.read().decode())

    block = raw.get("daily") or {}
    times = block.get("time") or []
    codes = block.get("weathercode") or []
    hi = block.get("temperature_2m_max") or []
    lo = block.get("temperature_2m_min") or []
    rain = block.get("precipitation_probability_max") or []

    def at(seq, i):
        return seq[i] if i < len(seq) else None

    out = {}
    for i, d in enumerate(times):
        code = at(codes, i)
        code = int(code) if code is not None else -1
        out[d] = {
            "text": WMO_TEXT.get(code, "未知"),
            "temp_high": at(hi, i),
            "temp_low": at(lo, i),
            "precip": at(rain, i),
            "code": code,
            "rainy": code in RAINY_CODES or code in SNOWY_CODES,
        }
    return out


def daily(city: str, start_date: str | None, days: int) -> dict:
    """
    给行程每天配一份天气。

    返回：
      {
        "available": bool,          # 有没有真实预报
        "reason": str,              # 不可用/部分可用的原因（人话）
        "source": str,              # open-meteo / mixed / climate / none
        "items": [ {date, source, text, temp_high, temp_low, precip, rainy} ]
      }
    source 取值：forecast（真实预报）/ climate（历史平均）/ none（都没有）
    """
    dates = []
    if start_date:
        try:
            d0 = datetime.date.fromisoformat(str(start_date)[:10])
            dates = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(days)]
        except ValueError:
            dates = []
    if not dates:
        return {"available": False, "reason": "没给出发日期，天气没法定",
                "source": "none", "items": []}

    coord = _geo(city)
    if coord is None:
        return {"available": False, "reason": f"没有「{city}」的坐标",
                "source": "none", "items": []}

    lat, lng = coord
    forecast = {}
    err = ""
    try:
        forecast = _open_meteo(lat, lng)
    except Exception as exc:      # noqa: BLE001 - 拿不到就降级，别让行程挂掉
        err = f"{type(exc).__name__}: {exc}"

    items = []
    for d in dates:
        hit = forecast.get(d)
        if hit:
            items.append({
                "date": d, "source": "forecast",
                "text": hit["text"],
                "temp_high": hit["temp_high"],
                "temp_low": hit["temp_low"],
                "precip": hit["precip"],
                "rainy": hit["rainy"],
            })
        else:
            month = datetime.date.fromisoformat(d).month
            cli = _climate(city, month)
            items.append({
                "date": d,
                "source": "climate" if cli else "none",
                "text": ((cli or {}).get("text") or ("历史平均" if cli else "还报不到")),
                "temp_high": (cli or {}).get("temp_high"),
                "temp_low": (cli or {}).get("temp_low"),
                "precip": None,
                "rainy": False,
            })

    n_fc = sum(1 for i in items if i["source"] == "forecast")
    if n_fc == len(items):
        src, reason = "open-meteo", ""
    elif n_fc:
        src, reason = "mixed", f"前 {n_fc} 天是真实预报，再远的用历史平均"
    else:
        src = "none"
        reason = err or f"预报只覆盖未来 {FORECAST_DAYS} 天，这几天用历史平均"
    return {"available": n_fc > 0, "reason": reason, "source": src, "items": items}
