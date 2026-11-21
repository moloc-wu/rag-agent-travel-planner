"""
tag_effort —— 给景点标「体力强度」，三档：轻松 / 一般 / 费体力。

为什么要有这一档：用户会说「不想爬山」，带老人孩子也会避开费体力的点，
但高德返回的数据里只有分类（公园/博物馆/风景名胜），没有强度这一项。
只按名字里的「山」字判断会误伤：景山公园、中山公园、南山博物馆、关山月美术馆都不用爬。

所以这里用「名字特征 + 分类兜底」跑第一版，再出一份报告给人工过一遍——
规则错了改规则，规则改不了的用 EXCEPTIONS 点名覆盖。

用法：
    python tag_effort.py             # 只出报告，不动数据
    python tag_effort.py --write     # 把 effort / effort_reason 写回 data/cities/*/pois.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
CITIES_DIR = ROOT / "data" / "cities"
REPORT = ROOT / "out" / "effort_report.md"

EASY_CATS = {
    "博物馆", "美术馆", "展览馆", "纪念馆", "科技馆", "图书馆",
    "特色商业街", "步行街", "城市广场", "剧场", "购物中心", "百货商场",
}
MID_CATS = {
    "公园", "植物园", "动物园", "水族馆", "寺庙道观", "游乐场", "教堂",
}

# 名字里出现这些字，才可能是「要爬」的地方
CLIMB_KW = ("山", "峰", "岭", "峡", "长城", "古道", "栈道", "沙山", "冰川", "天池", "雪山")
# 但同时出现这些词，说明是平的 / 是室内 / 是街区，不按爬算
CLIMB_VETO = (
    "公园", "博物馆", "美术馆", "展览馆", "纪念馆", "寺", "庙", "街", "巷",
    "广场", "塔", "楼", "桥", "车站", "站", "观景台", "观景点", "索道", "塘", "园",
)

# 规则判不动的，点名覆盖：(城市, 名字片段) -> (档位, 理由)
EXCEPTIONS = {
    ("南京", "中山陵"): ("费体力", "台阶多，人工标"),
    ("苏州", "上方山"): ("费体力", "要上山，人工标"),
    ("深圳", "笔架山"): ("费体力", "整座山都是台阶，人工标"),
    ("深圳", "塘朗山"): ("费体力", "登山道，人工标"),
    ("深圳", "莲花山公园"): ("一般", "小山坡，走台阶十几分钟"),
    ("青岛", "信号山公园"): ("一般", "小丘，爬升不大"),
    ("长沙", "麓山景区"): ("费体力", "岳麓山上的观景台，人工标"),
    ("拉萨", "药王山"): ("一般", "观景台，爬升不大"),
    # 下面这几个「山」字是虚的，不爬山
    ("厦门", "山海健康步道"): ("一般", "是步道不是山，人工标"),
    ("厦门", "胡里山炮台"): ("一般", "炮台在缓坡上，人工标"),
    ("拉萨", "拉萨山庄"): ("一般", "名字带山庄，不是山，人工标"),
}


def classify(name: str, category: str, city: str = "", rise: float | None = None):
    """返回 (档位, 依据)。规则尽量简单，好解释也好改。

    第 47 条（2026-11-11）：优先用真实高程数据——「景点海拔 − 城市中心海拔」的落差。
    为什么不用海拔绝对值：拉萨全城 3661 米，海拔高不等于要爬山；北京香山才 557 米，
    可城区只有 53 米，从下往上爬 500 米才是真累。所以看的是 rise。
    人工点名（EXCEPTIONS）仍然最优先，其次才是高程，最后才是名字/分类兜底。
    """
    n = name or ""
    for (c, frag), (level, why) in EXCEPTIONS.items():
        if c == city and frag in n:
            return level, why

    # 高程优先：200 米以上的落差基本就是要爬山了，50–200 算走坡
    if rise is not None:
        if rise >= 200:
            return "费体力", f"比城市中心高 {rise:.0f} 米"
        if rise >= 50:
            return "一般", f"比城市中心高 {rise:.0f} 米"

    kw = [k for k in CLIMB_KW if k in n]
    veto = [v for v in CLIMB_VETO if v in n]
    if kw and not veto:
        return "费体力", f"名字含「{'、'.join(kw)}」"
    if category in EASY_CATS:
        return "轻松", f"分类={category}"
    if category in MID_CATS:
        return "一般", f"分类={category}"
    return "一般", f"分类={category or '未知'}，按一般处理"


def load_city(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("pois") or []
    return data, items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="把结果写回 pois.json")
    args = ap.parse_args()

    rows = []          # (城市, 名字, 分类, 档位, 依据)
    for path in sorted(CITIES_DIR.glob("*/pois.json")):
        city = path.parent.name
        data, items = load_city(path)
        # 第 47 条：有真实高程就读进来，按「相对城市中心的落差」判体力
        rise_map = {}
        ef = path.parent / "elevation.json"
        if ef.exists():
            try:
                rise_map = json.loads(ef.read_text(encoding="utf-8")).get("rise") or {}
            except ValueError:
                rise_map = {}
        changed = False
        for p in items:
            rise = rise_map.get(p.get("id") or p.get("name"))
            level, why = classify(p.get("name", ""), p.get("category", ""), city, rise)
            rows.append((city, p.get("name", ""), p.get("category", ""), level, why))
            if p.get("effort") != level or p.get("effort_reason") != why:
                p["effort"] = level
                p["effort_reason"] = why
                changed = True
        if args.write and changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    dist = {}
    for r in rows:
        dist[r[3]] = dist.get(r[3], 0) + 1
    print(f"共 {len(rows)} 个点：" + "，".join(f"{k} {v}" for k, v in sorted(dist.items())))

    hard = [r for r in rows if r[3] == "费体力"]
    print(f"\n费体力 {len(hard)} 条：")
    for city, name, cat, level, why in hard:
        print(f"  {city:5s} {name}  [{cat}]  ← {why}")

    if args.write:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# 景点体力强度标定（第一版，机器规则）", "",
                 f"共 {len(rows)} 个点：" + "，".join(f"{k} {v}" for k, v in sorted(dist.items())),
                 "", "## 费体力（请重点扫这一段，错了告诉我名字）", ""]
        for city, name, cat, level, why in hard:
            lines.append(f"- **{name}**（{city}）· {cat} · {why}")
        lines += ["", f"## 一般（{sum(1 for r in rows if r[3] == '一般')} 个，不逐个列）", "",
                  "公园、寺庙、大型景区这类走走停停的地方。"]
        lines += ["", f"## 轻松（{sum(1 for r in rows if r[3] == '轻松')} 个，不逐个列）", "",
                  "博物馆、美术馆、街区、广场这类基本不走路的地方。",
                  "", "---", "",
                  "规则改不了的点，在 `tag_effort.py` 的 EXCEPTIONS 里加一行就行。"]
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告写到 {REPORT}")
        print("已写回 data/cities/*/pois.json（新增 effort / effort_reason）")


if __name__ == "__main__":
    main()
