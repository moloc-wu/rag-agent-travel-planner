"""render_html —— 把求解结果渲染成一个自包含的单文件页面（双击即看，不依赖外部资源）。"""
from __future__ import annotations

import json

TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>多日行程规划系统 · 毕设 demo</title>
<style>
  :root{
    --bg:#0d1320; --card:#151d2c; --card2:#1b2436; --line:#26314a;
    --txt:#e7eefc; --dim:#8b9ab5; --accent:#4c8dff; --ok:#22c48a; --bad:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#16233a 0%,var(--bg) 60%);
       color:var(--txt);font:14px/1.65 "Microsoft YaHei","PingFang SC",system-ui,sans-serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:30px 22px 70px}
  .kicker{color:var(--accent);font-size:12px;letter-spacing:.18em;font-weight:700}
  h1{font-size:26px;margin:6px 0 12px;font-weight:800;letter-spacing:.5px}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:22px}
  .chip{background:var(--card2);border:1px solid var(--line);border-radius:999px;
        padding:4px 12px;font-size:12px;color:var(--dim)}
  .chip b{color:var(--txt);font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
  .card .l{font-size:11px;color:var(--dim);letter-spacing:.1em}
  .card .v{font-size:23px;font-weight:800;margin-top:4px}
  .card .s{font-size:11px;color:var(--dim);margin-top:2px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:20px}
  .panel-h{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:14px;gap:12px}
  .panel-h h2{font-size:15px;margin:0;font-weight:700}
  .hint{font-size:11px;color:var(--dim)}
  #cmp{width:100%;border-collapse:collapse;font-size:13px}
  #cmp th,#cmp td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
  #cmp th{font-size:11px;color:var(--dim);font-weight:600;letter-spacing:.08em}
  #cmp td.num{font-variant-numeric:tabular-nums;font-weight:700}
  #cmp .win{color:var(--ok)}
  #cmp .lose{color:var(--dim)}
  #cmp .delta{color:var(--accent);font-size:12px}
  .mapbox{position:relative;height:600px;border-radius:10px;overflow:hidden;background:#0b1220}
  .mapbox .tiles,.mapbox canvas{position:absolute;left:0;top:0}
  .mapbox canvas{width:100%;height:100%;pointer-events:none}
  .mapbox .tiles img{position:absolute;width:256px;height:256px;filter:brightness(.90) saturate(.86) contrast(1.03)}
  .mapbox .badge2{position:absolute;font-size:10.5px;color:#c8d6ec;background:rgba(11,18,32,.78);
                  padding:1px 7px;border-radius:5px;letter-spacing:.02em}
  .mapbox .badge2.z{left:10px;top:9px}
  .mapbox .badge2.attr{right:9px;bottom:7px}
  .day{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--c,var(--accent));
       border-radius:14px;padding:16px 18px;margin-bottom:14px}
  .day-h{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
  .badge{background:var(--c,var(--accent));color:#08101d;font-weight:800;font-size:12px;
         border-radius:8px;padding:2px 9px}
  .day-h .t1{font-weight:700}
  .day-h .t2{font-size:12px;color:var(--dim)}
  ol.stops{list-style:none;margin:0;padding:0}
  ol.stops li{display:grid;grid-template-columns:96px 1fr;gap:14px;padding:10px 0;
              border-top:1px dashed #212c42}
  ol.stops li:first-child{border-top:0;padding-top:0}
  .tm{font-variant-numeric:tabular-nums;font-size:12px;color:var(--dim);white-space:nowrap}
  .tm b{display:block;color:var(--txt);font-size:14px}
  .nm{font-weight:700;font-size:14.5px}
  .cat{font-size:11px;color:var(--dim);border:1px solid var(--line);border-radius:6px;
       padding:0 6px;margin-left:6px;font-weight:400}
  .tg{font-size:11.5px;color:var(--accent);margin-top:2px}
  .meta{font-size:11.5px;color:var(--dim);margin-top:2px;font-variant-numeric:tabular-nums}
  .leg{font-size:11px;color:#5f7492;margin-top:3px}
  .wait{color:#f5a623}
  .ck{display:grid;grid-template-columns:22px 130px 1fr;gap:10px;padding:7px 0;
      border-top:1px solid #1d2740;font-size:13px}
  .ck:first-child{border-top:0}
  .ck .i{font-weight:800}
  .ck .n{color:var(--txt)}
  .ck .d{color:var(--dim);font-size:12.5px}
  .ck.pass .i{color:var(--ok)}
  .ck.fail .i{color:var(--bad)}
  .rate{font-size:34px;font-weight:800;color:var(--ok)}
  .foot{color:var(--dim);font-size:11.5px;line-height:1.9;margin-top:26px;
        border-top:1px solid var(--line);padding-top:14px}
  .sw{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
  .sw button{background:var(--card2);color:var(--dim);border:1px solid var(--line);
             border-radius:10px;padding:7px 14px;font-size:12.5px;cursor:pointer;font-family:inherit}
  .sw button.on{background:#1e3a6b;color:#cfe1ff;border-color:#3763b8}
  .legend{display:flex;gap:16px;font-size:11.5px;color:var(--dim);margin-top:10px;flex-wrap:wrap}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
</style>
</head>
<body>
<div class="wrap">
  <div class="kicker">毕业设计 demo · 多日行程规划</div>
  <h1 id="title"></h1>
  <div class="chips" id="chips"></div>

  <div class="grid" id="statcards"></div>

  <section class="panel">
    <div class="panel-h"><h2>两种求解策略对比</h2><span class="hint">M1 效用密度贪心 → M2 局部搜索</span></div>
    <table id="cmp"></table>
  </section>

  <div class="sw" id="sw"></div>

  <section class="panel">
    <div class="panel-h"><h2>路线图</h2><span class="hint">底图为高德栅格瓦片 · 编号 = 当天第几站 · 虚线为当日返程</span></div>
    <div class="mapbox" id="mapbox">
      <div class="tiles" id="tiles"></div>
      <canvas id="map"></canvas>
      <div class="badge2 z" id="zoominfo"></div>
      <div class="badge2 attr">© 高德地图</div>
    </div>
    <div class="legend" id="legend"></div>
  </section>

  <div id="days"></div>

  <section class="panel">
    <div class="panel-h"><h2>硬约束复核</h2><span class="hint">校验器独立于求解器实现，避免自证</span></div>
    <div style="display:flex;align-items:center;gap:18px;margin-bottom:10px">
      <div class="rate" id="rate">—</div>
      <div class="hint" id="ratesub"></div>
    </div>
    <div id="checks"></div>
  </section>

  <div class="foot" id="foot"></div>
</div>

<script>
const DATA = __PAYLOAD__;
const DAYC = ['#4c8dff','#22c48a','#f5a623','#c86bfa','#ff6b6b','#38bdf8','#e879f9'];
let state = 'local';

const el = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt = (n, d=0) => Number(n).toLocaleString('zh-CN', {maximumFractionDigits:d, minimumFractionDigits:d});

function variant(key){
  return DATA.variants.find(v => v.key === key) || DATA.variants[0];
}

function renderHead(){
  const m = DATA.meta;
  el('title').textContent = `${m.city} · ${m.cons.days} 天行程规划`;
  const chips = [
    ['预算', `¥${fmt(m.cons.budget)}`],
    ['每天', `${m.cons.day_start} – ${m.cons.day_end}`],
    ['住宿', m.cons.hotel],
    ['偏好', Object.entries(m.cons.interests).filter(([,w]) => w > 0.55).map(([k]) => k).join('、') || '均衡'],
    ['候选点', `${DATA.pois.length} 个`],
  ];
  el('chips').innerHTML = chips.map(([k,v]) => `<span class="chip">${k} <b>${esc(v)}</b></span>`).join('')
    + (m.notes||[]).map(n => `<span class="chip">${esc(n)}</span>`).join('');
}

function renderStats(){
  const t = variant(state).plan.totals;
  const v = variant(state).verify;
  const cards = [
    ['总效用', fmt(t.utility,1), '偏好加权后的收益'],
    ['停留点', `${t.stops} 个`, `共 ${DATA.pois.length} 个候选`],
    ['预计花费', `¥${fmt(t.total_cost)}`, `预算 ¥${fmt(t.budget)} · 余 ¥${fmt(t.budget - t.total_cost)}`],
    ['交通', `${fmt(t.km,1)} km`, `约 ${t.travel_min} 分钟`],
    ['约束满足率', `${v.pass_rate}%`, `${v.passed}/${v.total} 项通过`],
  ];
  el('statcards').innerHTML = cards.map(([l,v2,s]) =>
    `<div class="card"><div class="l">${l}</div><div class="v">${esc(v2)}</div><div class="s">${esc(s)}</div></div>`
  ).join('');
}

function renderCompare(){
  const A = variant('greedy'), B = variant('local');
  const ta = A.plan.totals, tb = B.plan.totals;
  const rows = [
    ['综合得分（效用 − λ×交通分钟）', ta.score, tb.score, 1, true],
    ['总效用', ta.utility, tb.utility, 1, true],
    ['停留点', ta.stops, tb.stops, 0, true],
    ['门票', ta.ticket, tb.ticket, 0, false],
    ['交通里程 (km)', ta.km, tb.km, 1, false],
    ['交通时间 (分钟)', ta.travel_min, tb.travel_min, 0, false],
    ['预计总花费', ta.total_cost, tb.total_cost, 0, false],
    ['约束满足率 (%)', A.verify.pass_rate, B.verify.pass_rate, 1, true],
  ];
  let h = `<tr><th>指标</th><th>${esc(A.label)}</th><th>${esc(B.label)}</th><th>变化</th></tr>`;
  for (const [name, a, b, d, higherBetter] of rows){
    const better = higherBetter ? b > a : b < a;
    const same = Math.abs(b - a) < 1e-9;
    const cA = better ? 'lose' : (same ? '' : 'win');
    const cB = better ? 'win' : (same ? '' : 'lose');
    const diff = b - a;
    const pct = a ? Math.abs(diff / a * 100) : 0;
    const arrow = same ? '—' : (diff > 0 ? '↑' : '↓');
    h += `<tr><td>${name}</td>
      <td class="num ${cA}">${fmt(a,d)}</td>
      <td class="num ${cB}">${fmt(b,d)}</td>
      <td class="delta">${same ? '持平' : `${arrow} ${fmt(Math.abs(diff),d)}（${fmt(pct,1)}%）`}</td></tr>`;
  }
  el('cmp').innerHTML = h;
}

function renderSwitch(){
  el('sw').innerHTML = DATA.variants.map(v =>
    `<button data-k="${v.key}" class="${v.key===state?'on':''}">${esc(v.label)}</button>`
  ).join('');
  el('sw').querySelectorAll('button').forEach(b => b.onclick = () => {
    state = b.dataset.k; renderAll();
  });
}

function renderLegend(){
  const d = variant(state).plan.days;
  el('legend').innerHTML = d.map(x =>
    `<span><i style="background:${DAYC[(x.index-1)%DAYC.length]}"></i>第 ${x.index} 天 · ${x.stops.length} 个点</span>`
  ).join('') + `<span><i style="background:#ffd166"></i>★ 住宿点</span>`
    + `<span>数字 = 当天第几站，实线为当天路线，虚线为当日返程</span>`;
}

function renderDays(){
  const p = variant(state).plan;
  el('days').innerHTML = p.days.map(d => {
    const c = DAYC[(d.index-1)%DAYC.length];
    const stops = d.stops.map((s,i) => `
      <li>
        <div class="tm"><b>${s.arrive_str}</b>${s.leave_str} 离开</div>
        <div>
          <div class="nm">${esc(s.name)}<span class="cat">${esc(s.category)}</span></div>
          <div class="tg">${esc(s.tags.join(' · '))}</div>
          <div class="meta">开放 ${s.window} · 建议 ${s.visit_min} 分钟 · 门票 ¥${fmt(s.ticket)} · 效用 ${fmt(s.utility,1)}</div>
          <div class="leg">↑ 上一站过来 ${fmt(s.leg_km,1)} km / ${s.leg_min} 分钟${s.wait_min?` · <span class="wait">等开门 ${s.wait_min} 分钟</span>`:''}</div>
        </div>
      </li>`).join('');
    const empty = d.stops.length ? stops :
      `<li><div class="tm"></div><div class="meta">这一天没安排点（预算或时间窗放不下）</div></li>`;
    return `<div class="day" style="--c:${c}">
      <div class="day-h"><span class="badge">D${d.index}</span>
        <span class="t1">第 ${d.index} 天</span>
        <span class="t2">${d.stops.length} 个点 · ${fmt(d.total_km,1)} km · 路上 ${d.travel_min} 分钟</span>
      </div>
      <ol class="stops">${empty}</ol></div>`;
  }).join('');
}

function renderChecks(){
  const v = variant(state).verify;
  el('rate').textContent = v.pass_rate + '%';
  el('rate').style.color = v.pass_rate >= 95 ? 'var(--ok)' : '#f5a623';
  el('ratesub').innerHTML = `${v.passed} / ${v.total} 项硬约束通过 · 计划书目标 <b>≥ 95%</b>`;
  el('checks').innerHTML = v.checks.map(c =>
    `<div class="ck ${c.ok?'pass':'fail'}"><div class="i">${c.ok?'✓':'✗'}</div>
     <div class="n">${esc(c.name)}</div><div class="d">${esc(c.detail)}</div></div>`
  ).join('');
}

// ====== 地图：高德栅格瓦片 + 路线覆盖层 ======
const TILE = 256;
const tileUrl = (x, y, z) =>
  `https://webrd0${1 + (Math.abs(x * 31 + y * 17) % 4)}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=${x}&y=${y}&z=${z}`;
const lngX = (lng, z) => (lng + 180) / 360 * Math.pow(2, z) * TILE;
const latY = (lat, z) => {
  const r = lat * Math.PI / 180;
  return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z) * TILE;
};

// WGS-84 → GCJ-02：高德底图是火星坐标，示例数据是 WGS-84，不转就会偏几百米
function wgs2gcj(lat, lng){
  const a = 6378245.0, ee = 0.00669342162296594323, PI = Math.PI;
  const tLat = (x, y) => {
    let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(y * PI) + 40 * Math.sin(y / 3 * PI)) * 2 / 3;
    r += (160 * Math.sin(y / 12 * PI) + 320 * Math.sin(y * PI / 30)) * 2 / 3;
    return r;
  };
  const tLng = (x, y) => {
    let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * PI) + 20 * Math.sin(2 * x * PI)) * 2 / 3;
    r += (20 * Math.sin(x * PI) + 40 * Math.sin(x / 3 * PI)) * 2 / 3;
    r += (150 * Math.sin(x / 12 * PI) + 300 * Math.sin(x / 30 * PI)) * 2 / 3;
    return r;
  };
  let dLat = tLat(lng - 105, lat - 35), dLng = tLng(lng - 105, lat - 35);
  const radLat = lat / 180 * PI;
  let magic = Math.sin(radLat);
  magic = 1 - ee * magic * magic;
  const sq = Math.sqrt(magic);
  dLat = (dLat * 180) / ((a * (1 - ee)) / (magic * sq) * PI);
  dLng = (dLng * 180) / (a / sq * Math.cos(radLat) * PI);
  return [lat + dLat, lng + dLng];
}

function toMap(q){
  if (DATA.meta.datum === 'gcj02') return {lat: q.lat, lng: q.lng};
  const [la, ln] = wgs2gcj(q.lat, q.lng);
  return {lat: la, lng: ln};
}

function drawMap(){
  const box = el('mapbox'), cv = el('map');
  const dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth || 1100, H = box.clientHeight || 600;
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const p = variant(state).plan;
  const stops = p.days.flatMap(d => d.stops);
  const pts = stops.concat([DATA.hotel]).map(toMap);

  const lngs = pts.map(q => q.lng), lats = pts.map(q => q.lat);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);

  // 选一个刚好把全部停靠点装下的缩放级别
  const padX = 82, padY = 44;
  let z = 15;
  while (z > 3){
    const dx = Math.abs(lngX(maxLng, z) - lngX(minLng, z));
    const dy = Math.abs(latY(minLat, z) - latY(maxLat, z));
    if (dx <= W - padX && dy <= H - padY) break;
    z -= 1;
  }

  const originX = (lngX(minLng, z) + lngX(maxLng, z)) / 2 - W / 2;
  const originY = (latY(minLat, z) + latY(maxLat, z)) / 2 - H / 2;
  const proj = q => {
    const c = toMap(q);
    return [lngX(c.lng, z) - originX, latY(c.lat, z) - originY];
  };

  // 铺瓦片
  const holder = el('tiles');
  holder.innerHTML = '';
  const n = Math.pow(2, z);
  const tx0 = Math.floor(originX / TILE), tx1 = Math.floor((originX + W) / TILE);
  const ty0 = Math.floor(originY / TILE), ty1 = Math.floor((originY + H) / TILE);
  const frag = document.createDocumentFragment();
  for (let tx = tx0; tx <= tx1; tx++){
    for (let ty = ty0; ty <= ty1; ty++){
      if (ty < 0 || ty >= n) continue;
      const img = new Image();
      img.src = tileUrl(((tx % n) + n) % n, ty, z);
      img.style.left = (tx * TILE - originX) + 'px';
      img.style.top = (ty * TILE - originY) + 'px';
      img.alt = '';
      frag.appendChild(img);
    }
  }
  holder.appendChild(frag);
  el('zoominfo').textContent = `高德底图 z=${z}`;

  // 标记位置：重叠时用黄金角螺旋挪开，画一条引线指回真实坐标
  const marks = [];
  p.days.forEach(d => d.stops.forEach((s, i) => {
    const [x, y] = proj(s);
    marks.push({ d: d.index, i: i + 1, x, y, px: x, py: y });
  }));
  const placed = [proj(DATA.hotel)];
  for (const m of marks){
    const clash = (X, Y) => placed.some(q => Math.hypot(q[0] - X, q[1] - Y) < 27);
    if (clash(m.x, m.y)){
      for (let k = 1; k < 80; k++){
        const ang = k * 2.399963;
        const r = 14 * Math.sqrt(k);
        const nx = m.x + Math.cos(ang) * r, ny = m.y + Math.sin(ang) * r;
        if (!clash(nx, ny)) { m.px = nx; m.py = ny; break; }
      }
    }
    placed.push([m.px, m.py]);
  }

  // 每一天的路线
  p.days.forEach(d => {
    const c = DAYC[(d.index - 1) % DAYC.length];
    const fwd = [DATA.hotel].concat(d.stops);
    ctx.beginPath();
    fwd.forEach((q, i) => {
      const [x, y] = proj(q);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = 'rgba(0,0,0,.55)'; ctx.lineWidth = 6; ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.strokeStyle = c; ctx.lineWidth = 3; ctx.stroke();

    if (d.stops.length){
      const a = proj(d.stops[d.stops.length - 1]), b = proj(DATA.hotel);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
      ctx.setLineDash([5, 5]); ctx.lineWidth = 1.8; ctx.globalAlpha = .55;
      ctx.strokeStyle = c; ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
  });

  // 真实坐标上的小圆点 + 引线
  for (const m of marks){
    const c = DAYC[(m.d - 1) % DAYC.length];
    ctx.beginPath(); ctx.arc(m.x, m.y, 3.6, 0, 7);
    ctx.fillStyle = '#fff'; ctx.fill();
    ctx.strokeStyle = c; ctx.lineWidth = 1.8; ctx.stroke();
    if (Math.hypot(m.px - m.x, m.py - m.y) > 3){
      ctx.beginPath(); ctx.moveTo(m.x, m.y); ctx.lineTo(m.px, m.py);
      ctx.strokeStyle = 'rgba(255,255,255,.8)'; ctx.lineWidth = 1.1; ctx.stroke();
    }
  }

  // 编号
  for (const m of marks){
    const c = DAYC[(m.d - 1) % DAYC.length];
    ctx.beginPath(); ctx.arc(m.px, m.py, 13.5, 0, 7);
    ctx.fillStyle = 'rgba(255,255,255,.94)'; ctx.fill();
    ctx.beginPath(); ctx.arc(m.px, m.py, 10.5, 0, 7);
    ctx.fillStyle = c; ctx.fill();
    ctx.fillStyle = '#08101d'; ctx.font = 'bold 12px "Microsoft YaHei"';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(m.i), m.px, m.py + .5);
  }

  // 住宿点
  const [hx, hy] = proj(DATA.hotel);
  ctx.beginPath(); ctx.arc(hx, hy, 13, 0, 7); ctx.fillStyle = 'rgba(255,255,255,.94)'; ctx.fill();
  ctx.beginPath(); ctx.arc(hx, hy, 9.5, 0, 7); ctx.fillStyle = '#ffd166'; ctx.fill();

  const label = '★ ' + DATA.hotel.name;
  ctx.font = 'bold 12px "Microsoft YaHei"'; ctx.textBaseline = 'middle';
  const tw = ctx.measureText(label).width;
  const rightSide = hx < W - tw - 46;
  const lx = rightSide ? hx + 18 : hx - 18 - tw;
  ctx.fillStyle = 'rgba(11,18,32,.82)';
  ctx.fillRect(lx - 6, hy - 10, tw + 12, 20);
  ctx.fillStyle = '#ffd166'; ctx.textAlign = 'left';
  ctx.fillText(label, lx, hy + .5);
}

function renderFoot(){
  const m = DATA.meta;
  const tsrc = m.travel_source === 'amap'
    ? '交通距离/时间取自高德驾车路径规划（实测路网 + 实时路况），不是直线估算。'
    : '交通时间 = 球面距离 × 1.35 绕行系数 ÷ 24 km/h + 5 分钟停车步行。';
  const src = m.dataset === 'amap' ? 'POI 来自高德 Web 服务实时检索' : 'POI 为手工示例集';
  el('foot').innerHTML = `
    <div>目标函数：最大化 Σ效用 − ${fmt(m.lambda_travel, 2)} × 交通分钟　|　求解评估次数：${fmt(m.evals.greedy)}（贪心）+ ${fmt(m.evals.total - m.evals.greedy)}（局部搜索）</div>
    <div>${esc(tsrc)}　|　${esc(src)}（坐标基准 ${esc(m.datum)}）</div>
    <div>效用 = 基础热度 × 偏好匹配度。</div>
    <div>${esc(m.data_note)}</div>
    <div>生成时间 ${esc(m.generated)}</div>`;
}

function renderAll(){
  renderStats(); renderSwitch(); renderLegend(); drawMap(); renderDays(); renderChecks();
}

renderHead(); renderCompare(); renderAll(); renderFoot();
window.addEventListener('resize', drawMap);
</script>
</body>
</html>
"""


def render(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return TEMPLATE.replace("__PAYLOAD__", data)
