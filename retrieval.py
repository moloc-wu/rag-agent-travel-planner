"""
retrieval —— M3：候选点的语义召回与重排。

候选池几十个点，直接全丢给求解器也能跑，但"用户说了一句话"这件事就没落地：
说"想爬山、看老建筑、吃点辣的"，池子里一半的点跟这句话没关系，白白占时间预算。

所以中间加一层检索，链路变成：

    自然语言 ──词表扩展──▶ 查询  ──BM25 + 向量──▶ 融合排序 ──Top-K──▶ 求解器

1. **查询扩展**：内置一张轻量领域词表（"爬山"→山/森林/登高，"辣的"→美食/小吃…）。
   有 embedding 服务时这层可以换成 dense retrieval，接口不变。
2. **BM25**：自己实现，中文按二元组切词，k1=1.5 / b=0.75。
3. **向量**：字符 n-gram 的 TF-IDF 余弦（不依赖任何外部模型）。
4. **融合**：0.5×BM25 + 0.3×向量 + 0.2×结构匹配（用户勾选的兴趣标签命中率）。
5. **重排**：融合分再乘一个由评分来的热度系数，取 Top-K。

设计原则和求解器一致：这一层只负责"挑哪些点进池子"，不做路线、不碰硬约束。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# 轻量领域词表：口语 → 检索词（没有 embedding 服务时的语义近似）
LEXICON = {
    "爬山": ["山", "森林", "登高", "风景名胜", "徒步", "景区"],
    "山": ["山", "森林", "风景名胜", "景区"],
    "老建筑": ["古建", "旧址", "书院", "阁", "寺", "历史", "文化"],
    "古建": ["旧址", "书院", "阁", "寺", "历史", "文化"],
    "历史": ["历史", "文化", "博物馆", "旧址", "书院"],
    "文化": ["文化", "博物馆", "历史", "书院"],
    "博物馆": ["博物馆", "博物院", "展览", "陈列"],
    "看展": ["美术馆", "博物馆", "艺术", "展览"],
    "艺术": ["艺术", "美术馆", "设计", "创意"],
    "拍照": ["打卡", "网红", "夜景", "艺术"],
    "出片": ["打卡", "网红", "夜景", "拍照"],
    "夜景": ["夜景", "夜游", "灯光", "江边"],
    "夜": ["夜景", "夜游"],
    "江边": ["江", "洲", "游船", "河"],
    "水": ["湖", "江", "湿地", "公园"],
    "湖": ["湖", "湿地", "公园"],
    "公园": ["公园", "湿地", "绿地", "园林"],
    "散步": ["公园", "湿地", "街区", "老街"],
    "吃": ["美食", "小吃", "街区", "老街", "夜市"],
    "辣的": ["美食", "小吃", "湘菜", "夜市"],
    "小吃": ["美食", "小吃", "街区", "夜市"],
    "逛街": ["街区", "步行街", "广场", "商圈"],
    "买东西": ["商圈", "购物", "步行街", "广场"],
    "带娃": ["亲子", "乐园", "动物园", "海洋馆"],
    "孩子": ["亲子", "乐园", "动物园"],
    "刺激": ["乐园", "游乐", "水上"],
    "安静": ["公园", "湿地", "书院", "美术馆"],
    "寺庙": ["寺", "庙", "祠", "宗教"],
    "湘江": ["江", "洲", "游船"],
}

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[a-zA-Z]+|\d+")


def tokenize(text: str) -> list:
    """中文按二元组 + 单字，英文/数字按整词。"""
    text = text.lower()
    tokens = []
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in cjk_runs:
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
            tokens.extend(run)                       # 单字也留一份，短词才匹配得上
    tokens.extend(_WORD.findall(text))
    return tokens


def expand_query(query: str) -> tuple[list, list]:
    """返回 (扩展后的词, 命中的词表键)。"""
    hits = [k for k in LEXICON if k in query]
    extra = []
    for k in hits:
        extra.extend(LEXICON[k])
    # 原句先给两倍权重，再用扩展词补语义
    return tokenize(query) * 2 + tokenize(" ".join(extra)), hits


class BM25:
    def __init__(self, docs: list[list], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.N = len(docs)
        self.avgdl = sum(len(d) for d in docs) / max(1, self.N)
        self.tf = [Counter(d) for d in docs]
        df = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, query: list, i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in query:
            f = tf.get(t)
            if not f:
                continue
            s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (
                f + self.k1 * (1 - self.b + self.b * dl / max(1, self.avgdl)))
        return s


class Vector:
    """字符 n-gram TF-IDF 向量 + 余弦（无需外部 embedding 服务）。"""

    def __init__(self, docs: list[list]):
        df = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()}
        self.vecs = [self._vec(d) for d in docs]

    def _vec(self, tokens: list) -> dict:
        tf = Counter(tokens)
        v = {t: (1 + math.log(c)) * self.idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def cosine(self, query: list, i: int) -> float:
        q = self._vec(query)
        v = self.vecs[i]
        return sum(w * v.get(t, 0.0) for t, w in q.items())


def _norm(values: list) -> list:
    lo, hi = min(values or [0]), max(values or [0])
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


class Retriever:
    def __init__(self, pois: list):
        self.pois = pois
        # 名称给两倍权重（名字最说明问题），后面接类型和标签
        self.texts = [f"{p.name} {p.name} {p.category} {' '.join(p.tags)}" for p in pois]
        self.docs = [tokenize(t) for t in self.texts]
        self.bm25 = BM25(self.docs)
        self.vec = Vector(self.docs)
        self.base_max = max((p.base for p in pois), default=1.0)

    def rank(self, query: str, interests: dict | None = None, k: int = 20) -> dict:
        q_tokens, hits = expand_query(query)
        interests = interests or {}
        chosen = {t for t, w in interests.items() if w > 0.6}

        bm = [self.bm25.score(q_tokens, i) for i in range(len(self.pois))]
        cs = [self.vec.cosine(q_tokens, i) for i in range(len(self.pois))]
        st = []
        for p in self.pois:
            tags = set(p.tags)
            st.append(len(tags & chosen) / max(1, len(chosen)) if chosen else 0.0)

        bm_n, cs_n = _norm(bm), _norm(cs)
        rows = []
        for i, p in enumerate(self.pois):
            fused = 0.50 * bm_n[i] + 0.30 * cs_n[i] + 0.20 * st[i]
            hot = p.base / self.base_max
            final = fused * (0.75 + 0.25 * hot)      # 重排：同样相关，热度高的靠前
            rows.append({
                "id": p.id, "name": p.name, "tags": list(p.tags),
                "bm25": round(bm[i], 3), "cosine": round(cs[i], 3),
                "struct": round(st[i], 2), "fused": round(fused, 4),
                "final": round(final, 4),
            })
        rows.sort(key=lambda r: (-r["final"], -r["fused"]))
        return {
            "query": query,
            "expanded": hits,
            "pool": len(self.pois),
            "k": min(k, len(self.pois)),
            "ranked": rows,
            "kept_ids": [r["id"] for r in rows[:k]],
        }
