"""
nlparse —— 把一句中文行程需求解析成结构化约束。

这一层就是计划书里「LLM 只负责理解与筛选，不负责求解」的落点：
  * 默认走规则（正则 + 词表），零依赖、可复现、可单测；
  * 接了模型的话直接替换本函数，只要输出同一个 TripConstraints 即可；
  * 求解器永远不碰自然语言，也就无法"幻觉"出一个不存在的景点。

城市也是从这句话里认的：「北京3日游」「去西安玩两天」——认不出来就用默认城市。
"""
from __future__ import annotations

import re

import cities as city_index
from optw_core import DEFAULT_INTERESTS, TripConstraints

CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 关键词 → (标签, 权重)
INTEREST_WORDS = [
    (("自然风光", "自然", "风景", "山水", "爬山", "湖", "湿地", "公园"), "自然风光", 1.30),
    (("历史文化", "历史", "人文", "文化", "古建", "老建筑", "老房子", "老巷", "古镇", "古迹",
      "遗址", "书院", "博物馆", "博物", "寺庙"), "历史文化", 1.30),
    (("美食", "好吃", "小吃", "夜宵", "吃吃吃", "逛吃"), "美食", 1.35),
    (("夜景", "夜游", "晚上", "夜市"), "夜景", 1.25),
    (("亲子", "孩子", "小孩", "带娃", "遛娃"), "亲子", 1.30),
    (("艺术", "展", "美术", "美术馆", "设计", "拍照"), "艺术", 1.20),
    (("网红", "打卡", "出片"), "网红打卡", 1.20),
]

# 城市名后面常跟的字：命中这些说明它在当句子的目的地用，不是碰巧出现在景点名里
_CITY_TAIL = ("游", "玩", "旅", "自", "出", "度", "行")
# 城市名前面常跟的字
_CITY_HEAD = ("去", "到", "在", "游", "逛")


def _days(text: str):
    m = re.search(r"(\d+)\s*[日天]", text)
    if m:
        return int(m.group(1))
    m = re.search(r"([一两二三四五六七八九十])\s*[日天]", text)
    if m and m.group(1) in CN_NUM:
        return CN_NUM[m.group(1)]
    return None


def _budget(text: str):
    for pat in (r"预算\s*(\d+)", r"(\d+)\s*元", r"(\d+)\s*块"):
        m = re.search(pat, text)
        if m:
            return float(m.group(1))
    return None


def _clock(text: str, keyword: str):
    m = re.search(rf"(\d{{1,2}})[:点时]\s*(?:半)?\s*{keyword}", text)
    if not m:
        return None
    h = int(m.group(1))
    if not 0 <= h <= 24:
        return None
    return h * 60 + (30 if "半" in m.group(0) else 0)


# 「我要去故宫」「必去天坛和颐和园」这类句子里的景点名。抽不准没关系，
# 后面会拿去和真实候选点做匹配，匹配不上的当没说过。
MUST_PAT = re.compile(
    r"(?:一定要去|必须去|必去|我要去|我想去|想去|要去|看看)([^\s，,。；;！!？?、]+)")


def _must(text: str) -> list:
    out = []
    for m in MUST_PAT.finditer(text):
        for part in re.split(r"[和跟与及、/]", m.group(1)):
            part = part.strip(" 的了玩")
            if 2 <= len(part) <= 10:
                out.append(part)
    return list(dict.fromkeys(out))


def _city(text: str) -> str | None:
    """
    从一句话里认出城市名。评分：出现在句首、或者后面紧跟「游/玩/旅」的优先，
    这样「长沙世界之窗好玩吗」里的长沙不会被「之窗」盖过，也不会误吃别的词。
    """
    best: tuple[int, str] | None = None
    for name in city_index.known_names():
        pos = text.find(name)
        if pos < 0:
            continue
        score = 1
        if pos <= 2:
            score += 2
        tail = text[pos + len(name): pos + len(name) + 2]
        if tail.startswith(_CITY_TAIL):
            score += 2
        if pos <= 4 and text[:pos].strip("，,。 ").endswith(_CITY_HEAD):
            score += 1
        if best is None or score > best[0]:
            best = (score, name)
    return best[1] if best else None


# 否定：「不想逛博物馆」「别安排爬山」这类，要认成「排除」，不能反向加权。
# 注意 INTEREST_WORDS 里「博物馆」是挂在「历史文化 +1.30」上的，不单独处理就会被理解反。
_AVOID_WORDS = [
    (("爬山", "登山", "爬坡", "徒步", "爬长城"), "climb"),
    (("博物馆",), "museum"),
    (("寺庙", "寺院", "道观", "烧香"), "temple"),
    (("逛街", "购物", "商场", "商业街"), "shopping"),
    (("夜景", "夜游", "夜市"), "night"),
]
_NEG_WORDS = ("不想", "不要", "不去", "不逛", "不玩", "不看", "别去", "别安排", "别",
              "避开", "排除", "算了", "免了")
_AVOID_LABEL = {"climb": "爬山", "museum": "博物馆", "temple": "寺庙",
                "shopping": "逛街", "night": "夜景"}


def _avoid(text: str) -> list:
    """找出句子里被否掉的类型。只看否定词前面 8 个字以内，够覆盖「不想逛博物馆」这种。"""
    out = []
    for words, key in _AVOID_WORDS:
        for w in words:
            hit = False
            for m in re.finditer(re.escape(w), text):
                head = text[max(0, m.start() - 8): m.start()]
                if any(n in head for n in _NEG_WORDS):
                    hit = True
                    break
            if hit:
                if key not in out:
                    out.append(key)
                break
    return out


def parse(text: str, base: TripConstraints | None = None):
    """返回 (约束对象, 解析记录)。解析记录用于在界面上说明"我理解到了什么"。"""
    cons = base or TripConstraints(days=2)
    notes = []

    c = _city(text)
    if c:
        cons.city = c
        notes.append(f"城市 → {c}")

    must = _must(text)
    if must:
        cons.must_visit = tuple(must)
        notes.append("必去 → " + "、".join(must))

    bad = _avoid(text)
    if bad:
        cons.avoid = tuple(bad)
        notes.append("不要 → " + "、".join(_AVOID_LABEL.get(k, k) for k in bad))

    d = _days(text)
    if d:
        cons.days = max(1, min(7, d))
        notes.append(f"天数 → {cons.days} 天")

    b = _budget(text)
    if b:
        cons.budget = b
        notes.append(f"预算 → {b:.0f} 元")

    interests = {k: 0.55 for k in DEFAULT_INTERESTS}
    hit = []
    for words, tag, w in INTEREST_WORDS:
        if any(word in text for word in words):
            interests[tag] = w
            hit.append(tag)
    if hit:
        cons.interests = interests
        notes.append("偏好 → " + "、".join(hit))
    else:
        cons.interests = {k: 1.0 for k in DEFAULT_INTERESTS}
        notes.append("偏好 → 未表态，按均衡偏好处理")

    s = _clock(text, "出发")
    if s:
        cons.day_start = s
        notes.append(f"每天出发 → {s // 60:02d}:{s % 60:02d}")
    e = _clock(text, "回")
    if e:
        cons.day_end = e
        notes.append(f"每天收工 → {e // 60:02d}:{e % 60:02d}")

    if not notes:
        notes.append("没识别到具体条件，使用默认约束（2 天 / 1500 元 / 均衡偏好）")
    return cons, notes
