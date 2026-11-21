"""
knowledge —— 攻略知识库：清洗去重 → 切分 → 检索。

和 retrieval.py 的分工：
- retrieval.py 负责"从候选景点里挑哪几个点"（对象是 POI）；
- 这里负责"从攻略文本里找相关段落"（对象是文档片段），两者共用 BM25 / 向量两套打分。

切分策略（不引第三方库，纯标准库）：
1. 按空行拆段，段内再按中文句号/问号/感叹号切开；
2. **去重**：每段取 md5 指纹，重复的直接丢（攻略转载、复制粘贴很常见）；
3. **合并**：小句攒到 CHUNK_CHARS 左右成一块，块之间留 OVERLAP 字重叠，避免答案被切断。
"""
from __future__ import annotations

import hashlib
import re

import retrieval

CHUNK_CHARS = 140        # 每块目标字数
OVERLAP = 30             # 段内超长时相邻块的重叠字数
MIN_CHUNK = 15           # 太碎的块不要

_SENT = re.compile(r"(?<=[。！？!?；;])")


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str) -> tuple[list, int]:
    """返回 (片段列表, 被去重的重复句数)。

    去重按**句子**做，不按段落：攻略里同一句"必吃：臭豆腐"经常出现在不同小标题下面，
    行级指纹抓不到，句级才抓得到。
    """
    text = clean(text)
    if not text:
        return [], 0

    seen = set()
    dup = 0
    chunks = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        # 段落内先按句切开、去掉重复句
        sents = []
        for s in _SENT.split(para):
            s = s.strip()
            if not s:
                continue
            if len(s) >= 8:
                fp = hashlib.md5(s.encode("utf-8")).hexdigest()
                if fp in seen:
                    dup += 1
                    continue
                seen.add(fp)
            sents.append(s)
        if not sents:
            continue
        # 段落整段装得下就整段成块（保留段落语义），装不下才在段内切
        if len("".join(sents)) <= CHUNK_CHARS:
            chunks.append("".join(sents))
            continue
        buf = ""
        for s in sents:
            if not buf:
                buf = s
            elif len(buf) + len(s) <= CHUNK_CHARS:
                buf += s
            else:
                tail = buf[-OVERLAP:] if len(buf) > OVERLAP else ""
                chunks.append(buf)
                buf = tail + s if tail else s
        if buf:
            chunks.append(buf)

    chunks = [c.strip() for c in chunks if len(c.strip()) >= MIN_CHUNK]
    return chunks, dup


class Index:
    """对当前库里所有片段建内存索引；文档一改就重建（数据量小，几毫秒）。"""

    def __init__(self, chunks: list):
        self.chunks = chunks
        self.tokens = [retrieval.tokenize(c["text"]) for c in chunks]
        self.bm25 = retrieval.BM25(self.tokens) if self.tokens else None
        self.vector = retrieval.Vector(self.tokens) if self.tokens else None

    def __len__(self):
        return len(self.chunks)

    def search(self, query: str, k: int = 5) -> dict:
        if not query.strip() or not self.chunks:
            return {"query": query, "expanded": [], "k": k, "total": len(self.chunks), "hits": []}
        q_tokens, hits = retrieval.expand_query(query)
        raw_bm = [self.bm25.score(q_tokens, i) for i in range(len(self.chunks))]
        raw_cos = [self.vector.cosine(q_tokens, i) for i in range(len(self.chunks))]
        bm = retrieval._norm(raw_bm)
        cos = retrieval._norm(raw_cos)
        scored = []
        for i, c in enumerate(self.chunks):
            fused = 0.65 * bm[i] + 0.35 * cos[i]
            if fused <= 0:
                continue
            scored.append({
                "doc_id": c["doc_id"],
                "title": c.get("title") or "",
                "ord": c.get("ord", 0),
                "text": c["text"],
                "bm25": round(raw_bm[i], 3),
                "cosine": round(raw_cos[i], 4),
                "score": round(fused, 4),
            })
        scored.sort(key=lambda x: -x["score"])
        return {
            "query": query,
            "expanded": hits,
            "k": k,
            "total": len(self.chunks),
            "hits": scored[:k],
        }
