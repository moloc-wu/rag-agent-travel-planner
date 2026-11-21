"""
server —— FastAPI 后端：登录注册 + 规划接口 + 历史方案 + 静态前端。

启动：
    python server.py                     # http://127.0.0.1:8765
    python server.py --port 9000

设计说明：
- 密码用 PBKDF2-SHA256 + 随机盐，库里不存明文；
- 登录签发 HMAC 签名的 token（7 天），不引第三方 JWT 库；
- 历史方案存 SQLite（data/app.db），一条记录 = 一份完整 payload。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import planner
import route
import weather as weather_mod
import knowledge
import store as store_mod

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DB_PATH = ROOT / "data" / "app.db"
SECRET_FILE = ROOT / "data" / "secret.key"
TOKEN_TTL = 7 * 24 * 3600
PBKDF2_ROUNDS = 120_000

app = FastAPI(title="多日行程规划", version="1.0.0")

STORE = None          # 启动时由 init_store() 填上


# ==================== 基础设施 ====================

def init_store():
    """默认 MySQL，连不上自动退回 SQLite（见 store.py）。"""
    global STORE
    STORE = store_mod.Store()
    promoted = STORE.bootstrap_admin()
    if promoted:
        print(f"  [store] 库里还没有管理员，已把最早的账号「{promoted}」提为 admin")
    return STORE


def secret() -> bytes:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    SECRET_FILE.parent.mkdir(exist_ok=True)
    s = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(s)
    return s


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def make_token(uid: int) -> str:
    exp = int(time.time()) + TOKEN_TTL
    raw = f"{uid}.{exp}".encode()
    sig = hmac.new(secret(), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{uid}.{exp}.{sig}".encode()).decode()


def parse_token(token: str) -> int | None:
    try:
        uid, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".")
        good = hmac.new(secret(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="请先登录")
    uid = parse_token(authorization.split(" ", 1)[1].strip())
    if uid is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    row = STORE.get_user_by_id(uid)
    if row is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return row


def current_user_optional(authorization: str | None = Header(default=None)):
    """游客也能用的接口用这个：没带 token 就返回 None，不抛 401。

    只给「不写库、不涉及个人数据」的接口用（第 30 条：游客不注册也能跑一次）。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    uid = parse_token(authorization.split(" ", 1)[1].strip())
    if uid is None:
        return None
    return STORE.get_user_by_id(uid)


def require_admin(user=Depends(current_user)):
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="只有管理员能进这个页面")
    return user


# ==================== 游客限流（第 30 条）====================
# 内存里的滑动窗口，只约束「未登录」这一档；注册用户不限。
# 单进程 demo 够用，重启即清零，不落库。
_ANON_WINDOW: dict[str, list[float]] = {}
ANON_LIMIT = 10          # 每人每窗口最多几次规划
ANON_WINDOW_SEC = 60.0


def _anon_allow(host: str) -> bool:
    now = time.time()
    q = _ANON_WINDOW.setdefault(host, [])
    q[:] = [t for t in q if now - t < ANON_WINDOW_SEC]
    if len(q) >= ANON_LIMIT:
        return False
    q.append(now)
    return True


# ==================== 请求模型 ====================

class RegisterReq(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=6, max_length=64)


class LoginReq(BaseModel):
    username: str
    password: str


class SegmentReq(BaseModel):
    city: str
    days: int = Field(default=1, ge=1, le=14)
    transport: str = "train"       # train / plane / drive


class PlanReq(BaseModel):
    query: str | None = None
    city: str | None = None
    depart_from: str | None = None                                  # 环线：出发地
    transport: str = "train"        # 单城往返的交通方式：train / plane / drive（第 55 条）
    segments: list[SegmentReq] | None = None                        # 环线：段列表
    start_date: str | None = None                                   # 环线：出发日期
    days: int | None = Field(default=None, ge=1, le=14)
    budget: float | None = Field(default=None, ge=0, le=100000)
    interests: dict[str, float] | None = None
    day_start: str | None = None
    day_end: str | None = None
    must_visit: list[str] | None = None
    avoid: list[str] | None = None          # 不想要：climb / museum / temple / shopping / night
    max_stops: int | None = Field(default=None, ge=1, le=8)   # 每天最多几个点，用户定
    with_elderly: bool = False              # 带老人
    with_kids: bool = False                 # 带孩子
    # 出行人数（第 27 条）：成人 / 1.2m 以上儿童 / 1.2m 以下儿童（多数景区免票）/ 老人 60+
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    children_free: int = Field(default=0, ge=0, le=9)
    seniors: int = Field(default=0, ge=0, le=9)
    students: int = Field(default=0, ge=0, le=9)   # 学生（第 54 条）：车票 75 折、门票常见半价
    # 时刻锚点（第 19 条）：某天几点前要到某地
    anchor_name: str | None = None
    anchor_day: int = Field(default=0, ge=0, le=14)
    anchor_before: str | None = None       # HH:MM
    room_beds: int = Field(default=0, ge=0, le=3)   # 0=不指定 1=大床 2=标间 3=家庭房
    hotel: str = ""                                 # 第 68 条：用户自己点名的旅馆，空=让算法挑
    # 第 69 条：用户自己分配的房型，比如 [{"beds":1,"count":1},{"beds":2,"count":1}]；空=按人数自动推
    room_plan: list[dict] | None = None
    # 餐标（第 61 条）：每人每天吃多少钱，用户选，默认 80
    food_per_day: float | None = Field(default=None, ge=0, le=2000)
    # 到达当天几点开始玩（第 57 条）：留空按「到站 + 安顿」估，填了就按他说的
    arrive_start_at: str | None = None
    # 出发时段偏好（第 56 条）：morning / forenoon / afternoon / evening，空 = 不指定
    depart_prefer: str | None = None
    # 市内交通方式（第 63 条）：mixed / drive / taxi / transit
    city_mode: str | None = None
    # 休息日（第 25 条）：填第几天，1 起，比如 [2] = 第 2 天不出门
    rest_full: list[int] | None = None      # 整天休：睡到自然醒
    rest_half: list[int] | None = None      # 半休：13:00 以后出门
    sample: bool = False
    use_kb: bool = False


class SaveReq(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    payload: dict
    public: bool = True


class PublicReq(BaseModel):
    public: bool


# ==================== 认证 ====================

@app.post("/api/register")
def register(req: RegisterReq):
    username = req.username.strip()
    if not all(ch.isalnum() or ch == "_" for ch in username):
        raise HTTPException(status_code=400, detail="用户名只能用字母、数字、下划线")
    salt = secrets.token_hex(16)
    try:
        user = STORE.create_user(username, hash_pw(req.password, salt), salt)
    except store_mod.DuplicateUser:
        raise HTTPException(status_code=409, detail="这个用户名已经被注册了")
    return {"token": make_token(user["id"]), "user": {"id": user["id"], "username": username}}


@app.post("/api/login")
def login(req: LoginReq):
    row = STORE.get_user_by_name(req.username.strip())
    if row is None or not hmac.compare_digest(row["pw_hash"], hash_pw(req.password, row["salt"])):
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    return {"token": make_token(row["id"]), "user": {"id": row["id"], "username": row["username"]}}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


class ProfileReq(BaseModel):
    username: str | None = None
    old_password: str | None = None
    new_password: str | None = Field(default=None, min_length=6, max_length=64)


@app.post("/api/profile")
def update_profile(req: ProfileReq, user=Depends(current_user)):
    """改用户名 / 改密码。改密码必须验旧密码。"""
    changed = []
    if req.username is not None:
        name = req.username.strip()
        if name and name != user["username"]:
            if not (3 <= len(name) <= 24) or not all(ch.isalnum() or ch == "_" for ch in name):
                raise HTTPException(status_code=400, detail="用户名 3–24 位，只能字母、数字、下划线")
            try:
                STORE.update_username(user["id"], name)
            except store_mod.DuplicateUser:
                raise HTTPException(status_code=409, detail="这个用户名已经被占了")
            changed.append("用户名")

    if req.new_password:
        row = STORE.get_user_by_name(user["username"])
        if not req.old_password or not hmac.compare_digest(
                row["pw_hash"], hash_pw(req.old_password, row["salt"])):
            raise HTTPException(status_code=400, detail="原密码不对")
        salt = secrets.token_hex(16)
        STORE.update_password(user["id"], hash_pw(req.new_password, salt), salt)
        changed.append("密码")

    if not changed:
        raise HTTPException(status_code=400, detail="没有要改的东西")
    fresh = STORE.get_user_by_id(user["id"])
    return {"ok": True, "changed": changed, "user": fresh,
            "token": make_token(user["id"])}


# ==================== 业务 ====================

@app.get("/api/meta")
def meta(sample: bool = False, city: str | None = None):
    try:
        return planner.meta_info(city=city, sample=sample)
    except planner.CityNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/pois")
def pois(sample: bool = False, city: str | None = None, user=Depends(current_user_optional)):
    try:
        return planner.list_pois(city=city, sample=sample)
    except planner.CityNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/hotels")
def hotels(city: str | None = None, user=Depends(current_user_optional)):
    """候选旅馆清单（含房型），「自己挑住宿」用。房型价是估的，返回里已标注。"""
    try:
        return planner.list_hotels(city=city)
    except planner.CityNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/weather")
def weather_api(city: str, start_date: str | None = None, days: int = 3,
                user=Depends(current_user)):
    """天气：有和风 key 就是真实预报，没有就以空转返回，前端据此显示占位。"""
    return weather_mod.daily(city, start_date, min(max(days, 1), 14))


@app.post("/api/plan")
def plan(req: PlanReq, request: Request, user=Depends(current_user_optional)):
    # 第 30 条：游客不注册也能跑一次。未登录只限流，不放行写库类接口。
    if user is None:
        host = request.client.host if request.client else "anon"
        if not _anon_allow(host):
            raise HTTPException(
                status_code=429,
                detail=f"游客每分钟最多试跑 {ANON_LIMIT} 次，歇一会儿再来；注册后不限次数",
            )
    if req.segments:
        try:
            payload, _ = route.plan_route(
                (req.depart_from or "").strip() or "出发地",
                [s.model_dump() for s in req.segments],
                start_date=req.start_date,
                budget=req.budget,
                interests=req.interests,
                must_visit=req.must_visit,
                sample=req.sample,
                day_start=req.day_start or "09:00",
                day_end=req.day_end or "21:30",
                avoid=req.avoid,
                max_stops=req.max_stops,
                with_elderly=req.with_elderly,
                with_kids=req.with_kids,
                adults=req.adults,
                children=req.children,
                children_free=req.children_free,
                seniors=req.seniors,
                students=req.students,
                room_beds=req.room_beds,
                food_per_day=req.food_per_day,
                arrive_start_at=req.arrive_start_at or "",
                depart_prefer=req.depart_prefer or "",
                city_mode=req.city_mode or "",
                anchor_name=req.anchor_name or "",
                anchor_day=req.anchor_day or 0,
                anchor_before=req.anchor_before or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except planner.CityNotFound as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:      # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"求解失败：{exc}")
        return payload

    # ---- 单城 + 出发地：补成 A → B → A 的往返（第 55 条）----
    # 出发地和目的地是同一个城市，就是本地游，仍走下面的单城路径。
    if req.depart_from and req.city and not req.segments:
        dep = (req.depart_from or "").strip()
        dst = (req.city or "").strip()
        if dep and dst and dep != dst:
            try:
                payload, _ = route.plan_route(
                    dep,
                    [{"city": dst, "days": req.days or 3,
                      "transport": req.transport or "train"}],
                    start_date=req.start_date,
                    budget=req.budget,
                    interests=req.interests,
                    must_visit=req.must_visit,
                    sample=req.sample,
                    day_start=req.day_start or "09:00",
                    day_end=req.day_end or "21:30",
                    avoid=req.avoid,
                    max_stops=req.max_stops,
                    with_elderly=req.with_elderly,
                    with_kids=req.with_kids,
                    adults=req.adults,
                    children=req.children,
                    children_free=req.children_free,
                    seniors=req.seniors,
                    students=req.students,
                    room_beds=req.room_beds,
                    food_per_day=req.food_per_day,
                    arrive_start_at=req.arrive_start_at or "",
                    depart_prefer=req.depart_prefer or "",
                    city_mode=req.city_mode or "",
                    anchor_name=req.anchor_name or "",
                    anchor_day=req.anchor_day or 0,
                    anchor_before=req.anchor_before or "",
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except planner.CityNotFound as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:      # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"求解失败：{exc}")
            return payload

    try:
        payload, _ = planner.plan_trip(
            req.query,
            city=req.city,
            days=req.days,
            budget=req.budget,
            interests=req.interests,
            day_start=req.day_start,
            day_end=req.day_end,
            sample=req.sample,
            start_date=req.start_date,
            avoid=req.avoid,
            max_stops=req.max_stops,
            with_elderly=req.with_elderly,
            with_kids=req.with_kids,
            adults=req.adults,
            children=req.children,
            children_free=req.children_free,
            seniors=req.seniors,
            students=req.students,
            room_beds=req.room_beds,
            hotel_pick=req.hotel or "",
            room_plan=req.room_plan or None,
            food_per_day=req.food_per_day,
            city_mode=req.city_mode or "",
            anchor_name=req.anchor_name or "",
            anchor_day=req.anchor_day or 0,
            anchor_before=req.anchor_before or "",
            rest_full=req.rest_full or None,
            rest_half=req.rest_half or None,
        )
    except planner.CityNotFound as exc:            # 城市没数据是使用者能改的事，不该算 500
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:                       # noqa: BLE001 - 求解失败要原样告诉前端
        raise HTTPException(status_code=500, detail=f"求解失败：{exc}")

    if req.use_kb and req.query and req.query.strip():
        try:
            idx = knowledge.Index(STORE.all_chunks())
            kb = idx.search(req.query, k=3)
            payload["meta"]["kb_refs"] = kb["hits"]
            payload["meta"]["kb_total"] = kb["total"]
            if kb["hits"]:
                payload["meta"]["notes"] = list(payload["meta"]["notes"]) + [
                    f"知识库参考 {len(kb['hits'])} 段攻略"]
        except Exception as exc:                   # noqa: BLE001 - 知识库坏了不该拖垮规划
            payload["meta"]["kb_error"] = str(exc)
    return payload


def _ics_from_payload(payload: dict, variant: str = "") -> str:
    """把一份方案转成 iCalendar 文本（第 14 条）：每个景点一个日程，能直接导进手机日历。

    没有出发日期就返回空串——日历必须有具体哪天，编一个不如不给。
    `variant`（第 67-2 条）：页面看的是哪套策略就导哪套，不传才退回第一套。
    """
    meta = payload.get("meta") or {}
    start_date = (meta.get("cons") or {}).get("start_date")
    if not start_date:
        return ""

    def stamp(date: str, hhmm: str) -> str:
        if not date or not hhmm:
            return ""
        try:
            y, m, d = str(date).split("-")
            hh, mm = str(hhmm).split(":")
        except ValueError:
            return ""
        return f"{y}{int(m):02d}{int(d):02d}T{int(hh):02d}{int(mm):02d}00"

    def esc(s) -> str:
        return str(s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")

    # 页面用的是选中的那个 variant；不传就按第一套（第 67-2 条）
    variants = payload.get("variants") or []
    pick = None
    if variant:
        pick = next((v for v in variants if v.get("key") == variant), None)
    if pick is None:
        pick = variants[0] if variants else None
    plan = (pick.get("plan") if pick else None) or {}
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//bishe-demo//optw planner//CN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    n = 0
    for d in plan.get("days") or []:
        for s in d.get("stops") or []:
            ds = stamp(d.get("date"), s.get("arrive_str"))
            de = stamp(d.get("date"), s.get("leave_str"))
            if not ds or not de:
                continue
            ticket = s.get("ticket") or 0
            desc = f"第 {d.get('index')} 天 · {s.get('category') or ''}" + (
                f" · 门票 ¥{ticket}" if ticket > 0 else " · 免费")
            lines += ["BEGIN:VEVENT",
                      f"UID:{d.get('index')}-{s.get('id')}@bishe-demo",
                      f"DTSTAMP:{now}",
                      f"DTSTART;TZID=Asia/Shanghai:{ds}",
                      f"DTEND;TZID=Asia/Shanghai:{de}",
                      "SUMMARY:" + esc(s.get("name")),
                      f"LOCATION:{s.get('lat')},{s.get('lng')}",
                      "DESCRIPTION:" + esc(desc),
                      "END:VEVENT"]
            n += 1
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n" if n else ""


class IcsReq(BaseModel):
    payload: dict
    variant: str | None = None      # 第 67-2 条：前端当前在看哪套策略


@app.post("/api/ics")
def make_ics(req: IcsReq):
    """第 14 条：把一份方案导成 .ics，能直接导进手机/电脑日历。"""
    txt = _ics_from_payload(req.payload, req.variant or "")
    if not txt:
        raise HTTPException(status_code=400,
                            detail="这份方案没有出发日期，或者一个景点都没排（先填出发日期再导）")
    return Response(content=txt, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="plan.ics"'})


@app.get("/api/history")
def list_history(user=Depends(current_user)):
    return STORE.list_history(user["id"])


@app.get("/api/history/{hid}")
def get_history(hid: int, user=Depends(current_user)):
    """详情登录即可读——方案在社区里是公开的，点赞收藏评论都作用在它上面。"""
    row = STORE.get_history_admin(hid)
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这条方案")
    return {"id": row["id"], "title": row["title"], "created": str(row["created"]),
            "payload": json.loads(row["payload"]), "is_mine": row["user_id"] == user["id"],
            "public": bool(row.get("is_public", 1))}


@app.post("/api/history")
def save_history(req: SaveReq, user=Depends(current_user)):
    blob = json.dumps(req.payload, ensure_ascii=False)
    return {"id": STORE.add_history(user["id"], req.title.strip(), blob, public=req.public),
            "public": req.public}


@app.post("/api/history/{hid}/public")
def set_public(hid: int, req: PublicReq, user=Depends(current_user)):
    """把自己（或管理员把任何）方案切成公开 / 私密。"""
    row = STORE.get_history_admin(hid)
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这条方案")
    if row["user_id"] != user["id"] and (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="只能改自己的方案")
    STORE.set_public(hid, req.public)
    return {"ok": True, "public": req.public}


@app.delete("/api/history/{hid}")
def delete_history(hid: int, user=Depends(current_user)):
    mine = STORE.get_history(user["id"], hid) is not None
    if not mine and (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=404, detail="找不到这条方案")
    if not STORE.delete_history_admin(hid):
        raise HTTPException(status_code=404, detail="找不到这条方案")
    return {"ok": True}


# ==================== 行程编辑 ====================

class EditReq(BaseModel):
    days: list[list[str]]


@app.post("/api/history/{hid}/edit")
def edit_history(hid: int, req: EditReq, user=Depends(current_user)):
    """按新顺序重排，返回调整后的 payload（不落库，用户看过再点保存）。"""
    row = STORE.get_history_admin(hid)
    if row is None:
        raise HTTPException(status_code=404, detail="找不到这条方案")
    payload = json.loads(row["payload"])
    try:
        return planner.rebuild(payload, req.days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ==================== 互动：收藏 / 点赞 / 评论 ====================

class CommentReq(BaseModel):
    body: str = Field(min_length=1, max_length=300)


class RoleReq(BaseModel):
    role: str


@app.get("/api/community")
def community(user=Depends(current_user)):
    return STORE.list_public_history()


@app.get("/api/history/{hid}/social")
def social(hid: int, user=Depends(current_user)):
    if STORE.get_history_admin(hid) is None:
        raise HTTPException(status_code=404, detail="找不到这条方案")
    state = STORE.reaction_state(user["id"], hid)
    return {**state, "comments": STORE.list_comments(hid)}


@app.post("/api/history/{hid}/like")
def like(hid: int, user=Depends(current_user)):
    return STORE.toggle_reaction(user["id"], hid, "like")


@app.post("/api/history/{hid}/fav")
def fav(hid: int, user=Depends(current_user)):
    return STORE.toggle_reaction(user["id"], hid, "fav")


@app.post("/api/history/{hid}/comment")
def comment(hid: int, req: CommentReq, user=Depends(current_user)):
    if STORE.get_history_admin(hid) is None:
        raise HTTPException(status_code=404, detail="找不到这条方案")
    body = req.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="评论不能是空的")
    STORE.add_comment(user["id"], hid, body)
    return {"comments": STORE.list_comments(hid)}


# ==================== 热门榜 ====================

@app.get("/api/ranking")
def ranking(sample: bool = False, city: str | None = None, user=Depends(current_user)):
    """景点热度榜：评分归一化占 0.6，被历史方案选中的次数归一化占 0.4。"""
    items = planner.list_pois(city=city, sample=sample) or []
    picks = STORE.poi_pick_counts()
    base_max = max((float(p.get("base") or 0) for p in items), default=0) or 1.0
    pick_max = max(picks.values(), default=0)
    out = []
    for p in items:
        b = float(p.get("base") or 0) / base_max
        c = picks.get(p.get("name", ""), 0)
        c_r = (c / pick_max) if pick_max else 0.0
        out.append({**p, "picks": c, "base_norm": round(b, 3),
                    "heat": round(0.6 * b + 0.4 * c_r, 4)})
    out.sort(key=lambda x: (-x["heat"], -float(x.get("base") or 0), x.get("name", "")))
    for i, it in enumerate(out, 1):
        it["rank"] = i
    return out


# ==================== 管理后台 ====================

@app.get("/api/admin/stats")
def admin_stats(user=Depends(require_admin)):
    return STORE.stats()


@app.get("/api/admin/users")
def admin_users(user=Depends(require_admin)):
    return STORE.list_users()


@app.post("/api/admin/users/{uid}/role")
def admin_set_role(uid: int, req: RoleReq, user=Depends(require_admin)):
    if uid == user["id"] and req.role != "admin":
        raise HTTPException(status_code=400, detail="不能把自己降级")
    STORE.set_role(uid, req.role)
    return {"ok": True}


@app.get("/api/admin/history")
def admin_history(user=Depends(require_admin)):
    return STORE.list_all_history()


@app.delete("/api/admin/history/{hid}")
def admin_delete_history(hid: int, user=Depends(require_admin)):
    if not STORE.delete_history_admin(hid):
        raise HTTPException(status_code=404, detail="找不到这条方案")
    return {"ok": True}


# ==================== 知识库 ====================

class DocReq(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=500_000)
    source: str = ""


@app.post("/api/kb/upload")
def kb_upload(req: DocReq, user=Depends(current_user)):
    chunks, dup = knowledge.split_text(req.text)
    if not chunks:
        raise HTTPException(status_code=400, detail="切不出有效片段，文本太短或者全是重复段")
    doc_id = STORE.add_doc(user["id"], req.title.strip(), req.source.strip(), chunks)
    return {"id": doc_id, "chunks": len(chunks), "chars": sum(len(c) for c in chunks), "dropped": dup}


@app.get("/api/kb/list")
def kb_list(user=Depends(current_user)):
    return STORE.list_docs()


@app.delete("/api/kb/{doc_id}")
def kb_delete(doc_id: int, user=Depends(current_user)):
    doc = [d for d in STORE.list_docs() if d["id"] == doc_id]
    if not doc:
        raise HTTPException(status_code=404, detail="找不到这篇文档")
    if not STORE.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="删除失败")
    return {"ok": True}


@app.get("/api/kb/search")
def kb_search(q: str, k: int = 5, user=Depends(current_user)):
    idx = knowledge.Index(STORE.all_chunks())
    return idx.search(q, k=max(1, min(20, k)))


# ==================== 静态前端 ====================

app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


def main():
    import uvicorn

    ap = argparse.ArgumentParser(description="多日行程规划服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    init_store()
    print(f"  服务启动： http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
