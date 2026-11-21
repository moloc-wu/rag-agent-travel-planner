"""
avoid —— 「这些我不想」的筛选。用户勾了不爬山、不想逛博物馆，规划前先把候选池过一遍。

为什么放在规划前、而不是排完再剔：
求解器的所有取舍都建立在候选池上。池子里只要还留着博物馆，它就会被排进来，
你剔掉一个它换一个，剔不完。所以要在池子这一层解决。

口径（2026-11-09 跟老大定）：
    climb     爬山   —— effort == 费体力（tag_effort.py 标的）
    museum    博物馆 —— 只算 category == 博物馆；纪念馆 / 展览馆 / 科技馆 不算
    temple    寺庙   —— category == 寺庙道观
    shopping  逛街   —— 特色商业街 / 步行街 / 城市广场 / 购物相关场所
    night     夜景   —— tags 里有「夜景」
"""
from __future__ import annotations

OPTIONS = [
    {"key": "climb", "label": "爬山", "note": "山岳、长城、台阶多的地方"},
    {"key": "museum", "label": "博物馆", "note": "只算博物馆本身，纪念馆展览馆不算"},
    {"key": "temple", "label": "寺庙", "note": "寺庙道观"},
    {"key": "shopping", "label": "逛街", "note": "商业街、步行街、广场"},
    {"key": "night", "label": "夜景", "note": "主打晚上看的地方"},
]

KEYS = tuple(o["key"] for o in OPTIONS)

_SHOPPING_CATS = {"特色商业街", "步行街", "城市广场", "购物相关场所", "商场", "百货商场"}


def blocked(poi, keys) -> str | None:
    """
    poi 命中任一 avoid 规则就返回一句原因（给提示用），没命中返回 None。

    keys 为空 / 未知值时一律放行——宁可不出错，也不要因为拼错一个键就把池子清空。
    """
    if not keys:
        return None
    want = {str(k).strip().lower() for k in keys if str(k).strip()}
    if not want:
        return None

    cat = (poi.category or "").strip()
    name = poi.name or ""
    if "climb" in want and getattr(poi, "effort", "") == "费体力":
        return "要爬山"
    # 口径：分类是「博物馆」就算；名字里带「博物馆」的也算——
    # 高德有不少「恭王府博物馆」这种，分类标成旅游景点，但用户看到的就是博物馆。
    # 纪念馆 / 展览馆 / 科技馆 不算（2026-11-09 老大定的）。
    if "museum" in want and (cat == "博物馆" or "博物馆" in name):
        return "不想逛博物馆"
    if "temple" in want and cat == "寺庙道观":
        return "不想去寺庙"
    if "shopping" in want and cat in _SHOPPING_CATS:
        return "不想逛街"
    if "night" in want and "夜景" in (poi.tags or ()):
        return "不想看夜景"
    return None


def filter_pois(pois, keys):
    """
    返回 (留下的点, 被剔掉的名字列表)。被剔的名字用来在结果里说清楚剔了多少。
    """
    kept, dropped = [], []
    for p in pois:
        why = blocked(p, keys)
        if why is None:
            kept.append(p)
        else:
            dropped.append(p.name)
    return kept, dropped
