"""
run_demo —— 命令行入口（浏览器端的服务见 server.py）。

用法：
    python run_demo.py
    python run_demo.py "北京3日游 预算2000 喜欢自然风光和历史文化"
    python run_demo.py "3日游 预算1500 爱吃" --city 成都 --days 2
    python run_demo.py --sample          # 用手工示例数据，不联网

产出：
    out\\demo.html   单文件可视化页面
    out\\plan.json   完整求解结果
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import optw_core as core
import planner
from render_html import render

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
BAR = "─" * 66


def print_report(payload, result):
    meta = payload["meta"]
    cons = meta["cons"]
    a = result["greedy"]["plan"]
    b = result["local"]["plan"]

    print()
    print(f"  {meta['city']} · {cons['days']} 天行程规划"
          f"　（数据：{meta['dataset']} / 交通：{meta['travel_source']}）")
    print(f"  需求：{meta['query'] or '（表单输入）'}")
    print("  理解：" + "；".join(meta["notes"]))
    print(BAR)
    print("  策略对比")
    for tag, plan in (("A 效用密度贪心", a), ("B 贪心+局部搜索", b)):
        print(f"    {tag:<14} 综合得分 {plan.score():7.1f} | 效用 {plan.total_utility:7.1f} | "
              f"{len(plan.stops):2d} 个点 | ¥{plan.total_cost:6.0f} | "
              f"{plan.km:5.1f} km | 路上 {plan.travel_min:4.0f} 分钟")
    gain = (b.score() - a.score()) / a.score() * 100 if a.score() else 0.0
    print(f"    局部搜索：综合得分 {gain:+.1f}%（效用 {b.total_utility - a.total_utility:+.1f}，"
          f"少跑 {a.km - b.km:.1f} km）")
    print(BAR)
    for d in b.days:
        if not d.stops:
            print(f"  第 {d.index + 1} 天：无安排")
            continue
        print(f"  第 {d.index + 1} 天（{core.hhmm(d.stops[0].arrive)} 起，"
              f"{core.hhmm(d.stops[-1].leave)} 结束，{d.total_km:.1f} km）")
        for s in d.stops:
            wait = f" 等{s.wait_min}分" if s.wait_min else ""
            print(f"    {core.hhmm(s.arrive)}–{core.hhmm(s.leave)}  {s.poi.name:<14}"
                  f" 门票¥{s.poi.ticket:>4.0f}  效用{s.utility:5.1f}{wait}")

    rep = result["local"]["verify"]
    print(BAR)
    print(f"  硬约束复核：{rep['passed']}/{rep['total']} 通过（{rep['pass_rate']}%）")
    for c in rep["checks"]:
        print(f"    {'✓' if c['ok'] else '✗'} {c['name']:<8} {c['detail']}")
    print(BAR)


def main():
    ap = argparse.ArgumentParser(description="多日行程规划 demo")
    ap.add_argument("query", nargs="?", default=planner.DEFAULT_QUERY, help="自然语言需求")
    ap.add_argument("--days", type=int, help="覆盖天数")
    ap.add_argument("--budget", type=float, help="覆盖预算")
    ap.add_argument("--sample", action="store_true", help="用手工示例数据，不读高德那套")
    ap.add_argument("--city", help="城市名；不给就看自然语言里有没有，再没有用默认城市")
    ap.add_argument("--no-html", action="store_true", help="只打印，不生成页面")
    args = ap.parse_args()

    payload, result = planner.plan_trip(
        args.query, city=args.city, days=args.days, budget=args.budget, sample=args.sample
    )
    print_report(payload, result)

    if not args.no_html:
        OUT.mkdir(exist_ok=True)
        (OUT / "plan.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / "demo.html").write_text(render(payload), encoding="utf-8")
        print(f"  页面：{OUT / 'demo.html'}")
        print(f"  数据：{OUT / 'plan.json'}")
        print()


if __name__ == "__main__":
    main()
