# -*- coding: utf-8 -*-
"""给 22 个城市的 hotels.json 补房型（TODO 第 27 条）。

背景：高德免费接口只返回酒店名/坐标/评分，不返回房价也不返回房型。
现在 hotels.json 里的 `price` 是按 hotel 类型估出来的（见 _note）。
这一版补的是**结构**——让「几个人 → 选什么房型 → 要几间房 → 花多少钱」能算对，
不是把估算变成真价。

房型按基准价乘系数生成：
    大床房  1 床   ×1.00
    标准间  2 床   ×1.15
    家庭房  3 床   ×1.50

用法：
    python add_rooms.py          # 写入
    python add_rooms.py --dry    # 只看会改多少，不落盘
"""
import json
import sys
from pathlib import Path

CITIES = Path(__file__).parent / "data" / "cities"

ROOM_TYPES = [
    ("大床房", 1, 1.00),
    ("标准间", 2, 1.15),
    ("家庭房", 3, 1.50),
]

NOTE = ("高德检索 + 按类型估算房价（高德免费接口不返回房价）；"
        "房型按基准价乘系数生成（大床 1.0 / 标间 1.15 / 家庭房 1.5），同样是估算")


def rooms_for(base: float) -> list:
    """base 是这家酒店的基准房价（一般按 2 人标准间估的）。"""
    out = []
    for name, beds, ratio in ROOM_TYPES:
        price = round(base * ratio / 10) * 10
        if price < base * 0.5:          # 防极端值
            price = round(base)
        out.append({"type": name, "beds": beds, "price": float(price)})
    return out


def main(dry: bool = False) -> None:
    if not CITIES.is_dir():
        print(f"找不到 {CITIES}")
        return

    total_city = 0
    total_hotel = 0
    for city_dir in sorted(CITIES.iterdir()):
        if not city_dir.is_dir():
            continue
        path = city_dir / "hotels.json"
        if not path.exists():
            continue

        data = json.loads(path.read_text(encoding="utf-8"))
        hotels = data.get("hotels") or []
        if not hotels:
            continue

        for h in hotels:
            base = float(h.get("price") or 0)
            if base <= 0:
                continue
            h["rooms"] = rooms_for(base)

        data["_note"] = NOTE
        total_city += 1
        total_hotel += len(hotels)
        print(f"{city_dir.name}: {len(hotels)} 家")

        if not dry:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    print(f"\n{'[dry] 会改' if dry else '已改'} {total_city} 个城市 / {total_hotel} 家酒店"
          f" / {total_hotel * len(ROOM_TYPES)} 条房型")


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
