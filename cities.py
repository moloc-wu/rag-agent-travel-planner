"""
cities —— 城市索引。这个系统不绑死任何一个城市。

布局：
    data/cities.json                 索引（默认城市 + 每个城市的数据在哪、住宿点在哪）
    data/cities/<城市>/pois.json     候选点（GCJ-02 坐标，带评分/营业时间/门票）
    data/cities/<城市>/matrix.json   N×N 驾车距离矩阵（可选，没有就用直线估算）

之所以要这层，是因为「先拉数据、再求解」这件事天然绑在某个城市上：
只有把城市当参数传下去，换城市才不用改代码，也不会覆盖掉上一个城市的数据。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CITIES_DIR = DATA / "cities"
INDEX = DATA / "cities.json"

# 老布局（单城市、文件在 data 根下）也能读：索引不存在时拿它当唯一城市
LEGACY_POIS = DATA / "poi_amap.json"
LEGACY_MATRIX = DATA / "travel_matrix.json"
LEGACY_SAMPLE = DATA / "poi_changsha.json"

# 直辖市 + 省会 / 首府 + 特别行政区
PROVINCIAL = [
    "北京", "上海", "天津", "重庆", "石家庄", "太原", "呼和浩特", "沈阳", "长春", "哈尔滨",
    "南京", "杭州", "合肥", "福州", "南昌", "济南", "郑州", "武汉", "长沙", "广州", "南宁",
    "海口", "成都", "贵阳", "昆明", "拉萨", "西安", "兰州", "西宁", "银川", "乌鲁木齐",
    "香港", "澳门", "台北",
]

# 常见旅游城市：用来在自然语言里认城市名，不要求本地已经有数据
TOURIST = [
    "苏州", "无锡", "常州", "扬州", "镇江", "徐州", "南通", "盐城", "泰州",
    "宁波", "温州", "绍兴", "嘉兴", "湖州", "金华", "台州", "舟山", "丽水",
    "青岛", "烟台", "威海", "日照", "临沂", "潍坊", "淄博", "济宁", "泰安",
    "大连", "鞍山", "丹东", "锦州", "秦皇岛", "唐山", "保定", "承德", "张家口",
    "洛阳", "开封", "安阳", "新乡", "南阳", "商丘", "平顶山",
    "宜昌", "襄阳", "荆州", "十堰", "恩施",
    "张家界", "岳阳", "常德", "衡阳", "郴州", "株洲", "湘潭", "吉首", "凤凰",
    "三亚", "海口", "琼海", "万宁",
    "桂林", "柳州", "北海", "阳朔", "梧州", "防城港",
    "大理", "丽江", "香格里拉", "西双版纳", "腾冲", "普洱",
    "九寨沟", "稻城", "康定", "乐山", "峨眉山", "都江堰", "绵阳", "宜宾", "泸州",
    "遵义", "安顺", "凯里", "铜仁",
    "黄山", "芜湖", "安庆", "蚌埠", "池州", "宣城",
    "泉州", "厦门", "漳州", "莆田", "龙岩", "武夷山",
    "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "汕头", "湛江", "肇庆",
    "潮州", "梅州", "江门", "茂名",
    "赣州", "景德镇", "上饶", "婺源", "九江", "吉安",
    "敦煌", "嘉峪关", "张掖", "酒泉", "天水", "平遥", "大同",
]

COMMON_CITIES = tuple(dict.fromkeys(PROVINCIAL + TOURIST))

# 「长沙市」「湘西州」「乌鲁木齐市」这类写法归一化时剥掉的后缀
_SUFFIX = ("特别行政区", "自治区", "自治州", "地区", "城市", "市")


def canonical(name: str) -> str:
    """把「长沙市」「北京 市」归成索引里的写法。"""
    s = (name or "").strip().replace(" ", "")
    if not s:
        return ""
    if s.endswith(("省",)):
        s = s[:-1]
    for suf in _SUFFIX:
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            break
    return s


def _legacy_index() -> dict:
    """索引还没建时的兼容读法：把 data 根下那份数据当成唯一城市。"""
    if not (LEGACY_POIS.exists() or LEGACY_SAMPLE.exists()):
        return {"default": "", "cities": {}}
    path = LEGACY_POIS if LEGACY_POIS.exists() else LEGACY_SAMPLE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default": "", "cities": {}}
    name = canonical(raw.get("city") or "")
    if not name:
        return {"default": "", "cities": {}}
    matrix = LEGACY_MATRIX if LEGACY_MATRIX.exists() else None
    return {
        "default": name,
        "cities": {
            name: {
                "name": name,
                "pois": str(path.relative_to(DATA)).replace("\\", "/"),
                "matrix": (str(matrix.relative_to(DATA)).replace("\\", "/") if matrix else ""),
                "hotel": raw.get("hotel") or {},
                "poi_count": len(raw.get("pois") or []),
                "updated": "",
            }
        },
    }


def load() -> dict:
    if INDEX.exists():
        try:
            raw = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(raw.get("cities"), dict) and raw["cities"]:
                return raw
        except (OSError, json.JSONDecodeError):
            pass
    return _legacy_index()


def save(index: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve(rel: str | None) -> Path | None:
    """索引里存的是相对 data/ 的路径，解析成绝对路径并确认存在。"""
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = DATA / p
    return p if p.exists() else None


def get(city: str) -> dict | None:
    """取某个城市的索引条目；名字做归一化，「长沙市」和「长沙」算一个。"""
    key = canonical(city)
    if not key:
        return None
    idx = load()
    entry = idx["cities"].get(key)
    if entry:
        return entry
    for name, e in idx["cities"].items():
        if canonical(name) == key:
            return e
    return None


def default_city() -> str:
    idx = load()
    d = canonical(idx.get("default") or "")
    if d and get(d):
        return d
    return canonical(next(iter(idx["cities"]), ""))


def paths(city: str | None = None) -> tuple[Path | None, Path | None]:
    """返回 (候选点文件, 距离矩阵文件)。城市不存在时两个都是 None。"""
    entry = get(city) if city else get(default_city())
    if not entry:
        return None, None
    return _resolve(entry.get("pois")), _resolve(entry.get("matrix"))


def available() -> list[dict]:
    """给前端的城市清单：有哪些城市、各多少点、要不要重拉。"""
    idx = load()
    out = []
    for name, e in idx["cities"].items():
        pois = _resolve(e.get("pois"))
        matrix = _resolve(e.get("matrix"))
        out.append({
            "name": canonical(e.get("name") or name),
            "poi_count": int(e.get("poi_count") or 0),
            "hotel": e.get("hotel") or {},
            "travel_source": "amap" if matrix else "estimate",
            "updated": e.get("updated") or "",
            "ready": pois is not None,
        })
    out.sort(key=lambda x: (-x["poi_count"], x["name"]))
    return out


def known_names() -> list[str]:
    """所有认得的城市名：本地已有数据的 + 常见旅游城市。按长度倒序，避免「京」先于「北京」命中。"""
    names = set(canonical(k) for k in load()["cities"].keys())
    names |= set(COMMON_CITIES)
    return sorted((n for n in names if n), key=len, reverse=True)


def register(city: str, *, pois_rel: str, matrix_rel: str = "", hotel: dict | None = None,
             poi_count: int = 0) -> dict:
    """把一个城市登记进索引（fetch_pois 拉完数据后调）。同名覆盖。"""
    name = canonical(city)
    idx = load()
    idx.setdefault("cities", {})
    idx["cities"][name] = {
        "name": name,
        "pois": pois_rel,
        "matrix": matrix_rel,
        "hotel": hotel or {},
        "poi_count": int(poi_count),
        "updated": dt.date.today().isoformat(),
    }
    if not idx.get("default"):
        idx["default"] = name
    save(idx)
    return idx["cities"][name]


def set_default(city: str) -> None:
    idx = load()
    name = canonical(city)
    if name in idx["cities"]:
        idx["default"] = name
        save(idx)


def city_dir(city: str) -> Path:
    """某个城市的数据目录（不保证已存在）。"""
    return CITIES_DIR / canonical(city)
