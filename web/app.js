/* 多日行程规划 —— 前端逻辑（Vue 3 + Element Plus，全部走本地 vendor，不依赖 CDN） */
const { createApp, ref, reactive, computed, onMounted, nextTick, watch } = Vue;
const { ElMessage, ElMessageBox } = ElementPlus;

const TAGS = ['自然风光', '历史文化', '美食', '夜景', '亲子', '艺术', '网红打卡'];
const DAYC = ['#4c8dff', '#22c48a', '#f5a623', '#c86bfa', '#ff6b6b', '#38bdf8', '#e879f9'];

/* ---------- 请求 ---------- */
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const token = localStorage.getItem('token');
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await fetch(path, { ...opts, headers });
  let data = {};
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok) {
    const msg = data.detail || ('请求失败 HTTP ' + res.status);
    if (res.status === 401) localStorage.removeItem('token');
    throw new Error(msg);
  }
  return data;
}

/* ---------- 高德瓦片地图 ---------- */
const TILE = 256;
const tileUrl = (x, y, z) =>
  `https://webrd0${1 + (Math.abs(x * 31 + y * 17) % 4)}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=${x}&y=${y}&z=${z}`;
const lngX = (lng, z) => (lng + 180) / 360 * Math.pow(2, z) * TILE;
const latY = (lat, z) => {
  const r = lat * Math.PI / 180;
  return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z) * TILE;
};

function wgs2gcj(lat, lng) {
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

function drawMap(canvas, holder, payload, key) {
  if (!canvas || !holder || !payload) return;
  const plan = (payload.variants.find(v => v.key === key) || payload.variants[0]).plan;
  const box = canvas.parentElement;
  const W = box.clientWidth || 900, H = box.clientHeight || 520;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  holder.innerHTML = '';

  const gcj = payload.meta.datum !== 'gcj02';
  const conv = q => {
    if (!gcj) return { lat: q.lat, lng: q.lng };
    const [la, ln] = wgs2gcj(q.lat, q.lng);
    return { lat: la, lng: ln };
  };

  const stops = plan.days.flatMap(d => d.stops);
  const pts = stops.concat([payload.hotel]).map(conv);
  const lngs = pts.map(q => q.lng), lats = pts.map(q => q.lat);
  const minLng = Math.min(...lngs), maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats), maxLat = Math.max(...lats);

  let z = 15;
  const padX = 82, padY = 44;
  while (z > 3) {
    const dx = Math.abs(lngX(maxLng, z) - lngX(minLng, z));
    const dy = Math.abs(latY(minLat, z) - latY(maxLat, z));
    if (dx <= W - padX && dy <= H - padY) break;
    z -= 1;
  }
  const originX = (lngX(minLng, z) + lngX(maxLng, z)) / 2 - W / 2;
  const originY = (latY(minLat, z) + latY(maxLat, z)) / 2 - H / 2;
  const proj = q => {
    const c = conv(q);
    return [lngX(c.lng, z) - originX, latY(c.lat, z) - originY];
  };

  const n = Math.pow(2, z);
  const tx0 = Math.floor(originX / TILE), tx1 = Math.floor((originX + W) / TILE);
  const ty0 = Math.floor(originY / TILE), ty1 = Math.floor((originY + H) / TILE);
  const frag = document.createDocumentFragment();
  for (let tx = tx0; tx <= tx1; tx++) {
    for (let ty = ty0; ty <= ty1; ty++) {
      if (ty < 0 || ty >= n) continue;
      const img = new Image();
      img.src = tileUrl(((tx % n) + n) % n, ty, z);
      img.style.left = (tx * TILE - originX) + 'px';
      img.style.top = (ty * TILE - originY) + 'px';
      frag.appendChild(img);
    }
  }
  holder.appendChild(frag);

  // 标注重叠就用黄金角螺旋挪开
  const marks = [];
  plan.days.forEach(d => d.stops.forEach((s, i) => {
    const [x, y] = proj(s);
    marks.push({ d: d.index, i: i + 1, x, y, px: x, py: y });
  }));
  const placed = [proj(payload.hotel)];
  for (const m of marks) {
    const clash = (X, Y) => placed.some(q => Math.hypot(q[0] - X, q[1] - Y) < 27);
    if (clash(m.x, m.y)) {
      for (let k = 1; k < 80; k++) {
        const ang = k * 2.399963, r = 14 * Math.sqrt(k);
        const nx = m.x + Math.cos(ang) * r, ny = m.y + Math.sin(ang) * r;
        if (!clash(nx, ny)) { m.px = nx; m.py = ny; break; }
      }
    }
    placed.push([m.px, m.py]);
  }

  plan.days.forEach(d => {
    const c = DAYC[(d.index - 1) % DAYC.length];
    ctx.beginPath();
    [payload.hotel].concat(d.stops).forEach((q, i) => {
      const [x, y] = proj(q);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = 'rgba(0,0,0,.5)'; ctx.lineWidth = 6; ctx.lineJoin = 'round'; ctx.stroke();
    ctx.strokeStyle = c; ctx.lineWidth = 3; ctx.stroke();
    if (d.stops.length) {
      const a = proj(d.stops[d.stops.length - 1]), b = proj(payload.hotel);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
      ctx.setLineDash([5, 5]); ctx.lineWidth = 1.8; ctx.globalAlpha = .55;
      ctx.strokeStyle = c; ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
  });

  marks.forEach(m => {
    const c = DAYC[(m.d - 1) % DAYC.length];
    ctx.beginPath(); ctx.arc(m.x, m.y, 3.6, 0, 7);
    ctx.fillStyle = '#fff'; ctx.fill();
    ctx.strokeStyle = c; ctx.lineWidth = 1.8; ctx.stroke();
    if (Math.hypot(m.px - m.x, m.py - m.y) > 3) {
      ctx.beginPath(); ctx.moveTo(m.x, m.y); ctx.lineTo(m.px, m.py);
      ctx.strokeStyle = 'rgba(255,255,255,.85)'; ctx.lineWidth = 1.1; ctx.stroke();
    }
  });

  marks.forEach(m => {
    const c = DAYC[(m.d - 1) % DAYC.length];
    ctx.beginPath(); ctx.arc(m.px, m.py, 13.5, 0, 7);
    ctx.fillStyle = 'rgba(255,255,255,.95)'; ctx.fill();
    ctx.beginPath(); ctx.arc(m.px, m.py, 10.5, 0, 7);
    ctx.fillStyle = c; ctx.fill();
    ctx.fillStyle = '#08101d'; ctx.font = 'bold 12px "Microsoft YaHei"';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(m.i), m.px, m.py + .5);
  });

  const [hx, hy] = proj(payload.hotel);
  ctx.beginPath(); ctx.arc(hx, hy, 13, 0, 7); ctx.fillStyle = 'rgba(255,255,255,.95)'; ctx.fill();
  ctx.beginPath(); ctx.arc(hx, hy, 9.5, 0, 7); ctx.fillStyle = '#ffd166'; ctx.fill();
  // 住宿点标上名字，不然地图上就一个黄点，看不出住哪儿
  const hname = (payload.hotel.name || '住宿点').replace('（住宿点）', '');
  ctx.font = '600 12px system-ui, -apple-system, sans-serif';
  const tw = ctx.measureText(hname).width;
  ctx.fillStyle = 'rgba(255,255,255,.93)';
  ctx.fillRect(hx + 14, hy - 9, tw + 10, 18);
  ctx.fillStyle = '#5a4708';
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(hname, hx + 19, hy + .5);
}

/* ---------- 应用 ---------- */
const app = createApp({
  setup() {
    const page = ref('plan');
    const user = ref(null);
    // 游客模式（第 30 条）：不登录也能规划，历史/社区/知识库这些要登录的再挡。
    const guest = ref(false);
    const authMode = ref('login');
    const authBusy = ref(false);
    const authForm = reactive({ username: '', password: '', password2: '' });

    const meta = ref(null);
    const pois = ref([]);
    const TRANSPORTS = [
      { v: 'train', l: '高铁' },
      { v: 'plane', l: '飞机' },
      { v: 'drive', l: '自驾' },
    ];
    const pad2 = (n) => String(n).padStart(2, '0');
    const isoDate = (d) => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
    function tomorrowIso() {
      const d = new Date();
      d.setDate(d.getDate() + 1);
      return isoDate(d);
    }
    // 「这些不想要」的勾选项，key 跟后端 avoid.py 的口径一一对应
    const AVOID = [
      { key: 'climb', label: '爬山' },
      { key: 'museum', label: '博物馆' },
      { key: 'temple', label: '寺庙' },
      { key: 'shopping', label: '逛街' },
      { key: 'night', label: '夜景' },
    ];
    const form = reactive({
      query: '', city: '', days: 3, budget: 1500,
      tags: ['自然风光', '历史文化'],
      dayStart: '09:00', dayEnd: '22:00', sample: false, useKb: false,
      // ---- 同行与强度（单城环线共用）----
      elderly: false, kids: false,
      stopsPerDay: 4,          // 每天最多几个点：用户定，系统不替他推断
      avoid: [],               // 不想要的类型
      // ---- 出行人数（第 27 条）：分开填，票价和房费口径不一样 ----
      adults: 1, children: 0, childrenFree: 0, seniors: 0, students: 0,
      roomBeds: 0,             // 0=不指定 1=大床 2=标间 3=家庭房（老口径，老分享链接还可能带）
      rooms: { 1: 0, 2: 0, 3: 0 },   // 第 69 条：大床 / 标间 / 家庭房各订几间，全 0 = 按人数自动推
      hotelPick: '',           // 第 68 条：自己点的旅馆名，空 = 让算法挑
      foodPerDay: 80,          // 餐标（第 61 条）：元/人/天，用户选
      arriveStart: null,       // 到达当天几点开始玩（第 57 条），留空按估算
      departPrefer: '',        // 坐几点的车（第 56 条）：morning/forenoon/afternoon/evening
      cityMode: '',            // 市内交通（第 63 条）：drive/taxi/transit，空 = 混合口径
      // 休息日（第 25 条）：选「第几天不出门」。整天休 = 睡到自然醒，半休 = 13:00 后出门
      restFull: [], restHalf: [],
      // ---- 时刻锚点（第 19 条）：某天几点前要到某地 ----
      anchorName: '', anchorDay: 0, anchorBefore: '',
      // ---- 环线模式（出发地 → 段1 → 段2 → 回出发地）----
      mode: 'single',
      departFrom: '',
      transport: 'train',      // 单城往返的交通方式（第 55 条）
      startDate: tomorrowIso(),
      segments: [{ city: '', days: 3, transport: 'train' }],
    });
    // 第 25 条：休息日下拉能选「第几天」——单城按天数，环线按各段天数加起来
    const dayOptions = computed(() => {
      let n = Number(form.days) || 3;
      if (form.mode === 'route') {
        n = form.segments.reduce((s, x) => s + (Number(x.days) || 0), 0) || 3;
      }
      n = Math.max(1, Math.min(14, n));
      return Array.from({ length: n }, (_, i) => i + 1);
    });

    function addSegment() {
      if (form.segments.length >= 3) return;
      const prev = form.segments[form.segments.length - 1];
      form.segments.push({ city: '', days: prev ? prev.days : 3, transport: 'train' });
    }
    function removeSegment(i) {
      if (form.segments.length <= 1) return;
      form.segments.splice(i, 1);
    }
    // 表单里这几个数默认就有值（3 天 / 1500 元），但默认值不等于用户说了：
    // 自然语言里写了「预算 2000」，就不该被下面那个 1500 悄悄盖掉。
    const touched = reactive({ days: false, budget: false, times: false });
    // 重新写一遍需求 = 用户又自己说了，之前手改过的表单就别再压着它
    function resetTouched() {
      touched.days = false;
      touched.budget = false;
      touched.times = false;
    }
    const planning = ref(false);
    const result = ref(null);
    const variant = ref('local');
    const history = ref([]);
    const histBusy = ref(false);

    /* ---- 社区 / 热门榜 / 管理 ---- */
    const community = ref([]);
    const ranking = ref([]);
    const adminStats = ref([]);
    const adminUsers = ref([]);
    const adminHistory = ref([]);
    const socialOpen = ref(false);
    const socialRow = ref(null);
    const socialLike = ref({ on: false, count: 0 });
    const socialFav = ref({ on: false, count: 0 });
    const socialComments = ref([]);
    const commentText = ref('');
    const commentBusy = ref(false);

    /* ---- 行程编辑 / 账户 ---- */
    const editOpen = ref(false);
    const editBusy = ref(false);
    const editDays = ref([]);
    const editHid = ref(null);
    const accountOpen = ref(false);
    const accountBusy = ref(false);
    const accountForm = reactive({ username: '', oldPassword: '', newPassword: '' });

    /* ---- 知识库 ---- */
    const kbDocs = ref([]);
    const kbForm = reactive({ title: '', source: '', text: '' });
    const kbBusy = ref(false);
    const kbQuery = ref('');
    const kbResult = ref(null);
    const kbSearchBusy = ref(false);

    async function loadDocs() {
      try { kbDocs.value = await api('/api/kb/list'); }
      catch (e) { kbDocs.value = []; }
    }

    function pickFile(file) {
      const f = file && file.raw ? file.raw : file;
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => {
        kbForm.text = String(reader.result || '');
        kbForm.source = f.name || '';
        if (!kbForm.title) kbForm.title = (f.name || '').replace(/\.[^.]+$/, '');
        ElMessage.success('读进来了 ' + (f.name || ''));
      };
      reader.onerror = () => ElMessage.error('读不了这个文件');
      reader.readAsText(f, 'utf-8');
    }

    async function uploadDoc() {
      if (kbBusy.value) return;
      if (!kbForm.title.trim() || !kbForm.text.trim()) {
        ElMessage.warning('标题和正文都要填');
        return;
      }
      kbBusy.value = true;
      try {
        const r = await api('/api/kb/upload', {
          method: 'POST',
          body: JSON.stringify({
            title: kbForm.title.trim(), text: kbForm.text, source: kbForm.source.trim(),
          }),
        });
        ElMessage.success(`切出 ${r.chunks} 个片段` + (r.dropped ? `，去掉 ${r.dropped} 条重复句` : ''));
        kbForm.text = '';
        kbForm.title = '';
        kbForm.source = '';
        await loadDocs();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        kbBusy.value = false;
      }
    }

    async function searchKb() {
      if (!kbQuery.value.trim()) return;
      kbSearchBusy.value = true;
      try {
        kbResult.value = await api('/api/kb/search?q=' + encodeURIComponent(kbQuery.value.trim()) + '&k=5');
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        kbSearchBusy.value = false;
      }
    }

    async function removeDoc(row) {
      try {
        await ElMessageBox.confirm('删掉「' + row.title + '」？片段会一起删', '确认删除', { type: 'warning' });
        await api('/api/kb/' + row.id, { method: 'DELETE' });
        ElMessage.success('已删除');
        await loadDocs();
      } catch (e) {
        if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '删除失败');
      }
    }

    const mapEl = ref(null);
    const tilesEl = ref(null);

    /* ---- 认证 ---- */
    async function doAuth() {
      if (authBusy.value) return;
      if (authMode.value === 'register' && authForm.password !== authForm.password2) {
        ElMessage.warning('两次密码不一致');
        return;
      }
      authBusy.value = true;
      try {
        const path = authMode.value === 'login' ? '/api/login' : '/api/register';
        const body = { username: authForm.username.trim(), password: authForm.password };
        const res = await api(path, { method: 'POST', body: JSON.stringify(body) });
        localStorage.setItem('token', res.token);
        user.value = res.user;
        ElMessage.success(authMode.value === 'login' ? '欢迎回来' : '注册成功');
        await boot();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        authBusy.value = false;
      }
    }

    function logout() {
      localStorage.removeItem('token');
      user.value = null;
      guest.value = false;
      result.value = null;
      history.value = [];
      page.value = 'plan';
      ElMessage.success('已退出');
    }

    /* 不登录，直接试用（第 30 条）。能规划，但历史 / 社区 / 知识库 / 后台要登录。 */
    async function enterGuest() {
      guest.value = true;
      page.value = 'plan';
      try {
        await loadMeta();
        await loadPois();
        await loadHotels();
      } catch (e) {
        ElMessage.error(e.message);
        guest.value = false;
      }
    }

    // 游客点了要登录的菜单：给一句人话，然后留在规划页
    function needLogin(tip) {
      ElMessage.warning(tip || '这个功能要登录才能用');
      page.value = 'plan';
    }

    /* ---- 数据 ---- */
    // 城市是参数：form.city 为空表示「按描述自动识别」，交给后端的自然语言解析
    const cityParam = () => (form.city ? '&city=' + encodeURIComponent(form.city) : '');

    async function loadMeta() {
      try {
        meta.value = await api('/api/meta?sample=' + (form.sample ? 'true' : 'false') + cityParam());
      } catch (e) {
        ElMessage.error(e.message || '这个城市还没有数据');
      }
    }

    async function loadPois() {
      try { pois.value = await api('/api/pois?sample=' + (form.sample ? 'true' : 'false') + cityParam()); }
      catch (e) { pois.value = []; }
    }

    // ---- 候选旅馆（第 68 条）：让用户自己点，不点就还让算法挑 ----
    const hotelOptions = ref([]);
    async function loadHotels() {
      if (form.sample) { hotelOptions.value = []; return; }
      try {
        const d = await api('/api/hotels' + (form.city ? '?city=' + encodeURIComponent(form.city) : ''));
        hotelOptions.value = d.hotels || [];
      } catch (e) { hotelOptions.value = []; }
      // 换了城市，上一条城市点过的旅馆就不再适用了
      if (!hotelOptions.value.some(h => h.name === form.hotelPick)) form.hotelPick = '';
    }
    // 当前点中的那家（没点就是 null），房型提示要用它的价
    const pickedHotel = computed(
      () => hotelOptions.value.find(h => h.name === form.hotelPick) || null);

    // ---- 房型分配（第 69 条）：三个计数框，全 0 就不传，让后端按人数自动推 ----
    const roomPlanPayload = () => {
      const out = [];
      [1, 2, 3].forEach(b => {
        const n = Number(form.rooms[b]) || 0;
        if (n > 0) out.push({ beds: b, count: n });
      });
      return out.length ? out : null;
    };
    // 按「你点的那家」的房价，算这套组合每晚多少钱
    const roomPlanTotal = computed(() => {
      const hs = pickedHotel.value;
      if (!hs) return 0;
      let sum = 0;
      [1, 2, 3].forEach(b => {
        const n = Number(form.rooms[b]) || 0;
        if (n <= 0) return;
        const r = (hs.rooms || []).find(x => Number(x.beds) === b);
        sum += Number(r ? r.price : hs.price) * n;
      });
      return Math.round(sum);
    });

    // 换城市：旧结果属于上一个城市，清掉，再把这一城的数据重新取一遍
    async function onCityChange() {
      result.value = null;
      await loadMeta();
      await loadPois();
      await loadHotels();
      await loadRanking();
    }

    async function loadHistory() {
      try { history.value = await api('/api/history'); }
      catch (e) { history.value = []; }
    }

    async function boot() {
      await loadMeta();
      await loadPois();
      await loadHotels();
      await loadHistory();
      await loadCommunity();
      await loadRanking();
    }

    /* ---- 社区 / 热门榜 ---- */
    async function loadCommunity() {
      try { community.value = await api('/api/community'); }
      catch (e) { community.value = []; }
    }

    async function loadRanking() {
      try { ranking.value = await api('/api/ranking?sample=' + (form.sample ? 'true' : 'false') + cityParam()); }
      catch (e) { ranking.value = []; }
    }

    async function openSocial(row) {
      socialRow.value = row;
      socialOpen.value = true;
      commentText.value = '';
      try {
        const s = await api('/api/history/' + row.id + '/social');
        socialLike.value = s.like;
        socialFav.value = s.fav;
        socialComments.value = s.comments || [];
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    async function toggleLike(row) {
      try {
        const r = await api('/api/history/' + row.id + '/like', { method: 'POST' });
        socialLike.value = r;
        if (socialRow.value && socialRow.value.id === row.id) socialRow.value.likes = r.count;
        row.likes = r.count;
        ElMessage.success(r.on ? '已点赞' : '已取消点赞');
      } catch (e) { ElMessage.error(e.message); }
    }

    async function toggleFav(row) {
      try {
        const r = await api('/api/history/' + row.id + '/fav', { method: 'POST' });
        socialFav.value = r;
        if (socialRow.value && socialRow.value.id === row.id) socialRow.value.favs = r.count;
        row.favs = r.count;
        ElMessage.success(r.on ? '已收藏' : '已取消收藏');
      } catch (e) { ElMessage.error(e.message); }
    }

    async function sendComment() {
      const body = commentText.value.trim();
      if (!body || !socialRow.value) return;
      commentBusy.value = true;
      try {
        const r = await api('/api/history/' + socialRow.value.id + '/comment',
          { method: 'POST', body: JSON.stringify({ body }) });
        socialComments.value = r.comments || [];
        socialRow.value.comments = socialComments.value.length;
        commentText.value = '';
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        commentBusy.value = false;
      }
    }

    /* ---- 管理后台 ---- */
    async function loadAdmin() {
      try {
        const [st, us, hi] = await Promise.all([
          api('/api/admin/stats'), api('/api/admin/users'), api('/api/admin/history'),
        ]);
        adminStats.value = [
          { l: '用户数', v: st.users }, { l: '管理员', v: st.admins },
          { l: '方案数', v: st.history }, { l: '点赞', v: st.likes },
          { l: '收藏', v: st.favs }, { l: '评论', v: st.comments },
        ];
        adminUsers.value = us;
        adminHistory.value = hi;
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    async function setRole(row, role) {
      try {
        await api('/api/admin/users/' + row.id + '/role',
          { method: 'POST', body: JSON.stringify({ role }) });
        ElMessage.success('已更新');
        await loadAdmin();
      } catch (e) { ElMessage.error(e.message); }
    }

    async function adminRemove(row) {
      try {
        await ElMessageBox.confirm('删掉「' + row.title + '」？这个操作不可撤销', '确认删除', { type: 'warning' });
        await api('/api/admin/history/' + row.id, { method: 'DELETE' });
        ElMessage.success('已删除');
        await Promise.all([loadAdmin(), loadCommunity(), loadHistory()]);
      } catch (e) {
        if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '删除失败');
      }
    }

    /* ---- 规划 ---- */
    const interestWeights = () => {
      const out = {};
      TAGS.forEach(t => { out[t] = form.tags.includes(t) ? 1.35 : 0.55; });
      return out;
    };

    const isRoute = computed(() => !!(result.value && result.value.meta && result.value.meta.mode === 'route'));

    async function runPlan() {
      planning.value = true;
      try {
        let body;
        if (form.mode === 'route') {
          const segs = form.segments
            .map(s => ({ city: (s.city || '').trim(), days: Number(s.days) || 1, transport: s.transport || 'train' }))
            .filter(s => s.city);
          if (!form.departFrom.trim()) throw new Error('环线模式要先填出发地');
          if (!segs.length) throw new Error('至少填一段目的地');
          body = {
            depart_from: form.departFrom.trim(),
            segments: segs,
            start_date: form.startDate || null,
            budget: form.budget,
            interests: interestWeights(),
            day_start: form.dayStart,
            day_end: form.dayEnd,
            avoid: form.avoid.length ? form.avoid : null,
            max_stops: form.stopsPerDay || null,
            with_elderly: form.elderly,
            with_kids: form.kids,
            adults: form.adults || 1,
            children: form.children || 0,
            children_free: form.childrenFree || 0,
            seniors: form.seniors || 0,
            students: form.students || 0,
            room_beds: form.roomBeds || 0,
            food_per_day: form.foodPerDay || null,
            arrive_start_at: form.arriveStart || null,
            depart_prefer: form.departPrefer || null,
            city_mode: form.cityMode || null,
            sample: form.sample,
          };
        } else {
          body = {
            query: form.query.trim() || null,
            city: form.city || null,
            // 填了出发地且跟目的地不是同一个城市，后端会补成 A → B → A 往返（第 55 条）
            depart_from: form.departFrom.trim() || null,
            transport: form.transport || 'train',
            days: touched.days ? form.days : null,
            budget: touched.budget ? form.budget : null,
            interests: interestWeights(),
            day_start: touched.times ? form.dayStart : null,
            day_end: touched.times ? form.dayEnd : null,
            start_date: form.startDate || null,
            avoid: form.avoid.length ? form.avoid : null,
            max_stops: form.stopsPerDay || null,
            with_elderly: form.elderly,
            with_kids: form.kids,
            adults: form.adults || 1,
            children: form.children || 0,
            children_free: form.childrenFree || 0,
            seniors: form.seniors || 0,
            students: form.students || 0,
            room_beds: form.roomBeds || 0,
            hotel: form.hotelPick || null,
            room_plan: roomPlanPayload(),
            food_per_day: form.foodPerDay || null,
            arrive_start_at: form.arriveStart || null,
            depart_prefer: form.departPrefer || null,
            city_mode: form.cityMode || null,
            anchor_name: form.anchorName.trim() || null,
            anchor_day: form.anchorDay || 0,
            anchor_before: form.anchorBefore || null,
            // 第 25 条：休息日
            rest_full: form.restFull.length ? form.restFull : null,
            rest_half: form.restHalf.length ? form.restHalf : null,
            sample: form.sample,
            use_kb: form.useKb,
          };
        }
        result.value = await api('/api/plan', {
          method: 'POST',
          body: JSON.stringify(body),
        });
        // 表单框跟着结果走，免得出现「输入框写 1500、结果里写预算 2000」这种看着打架的情况
        if (!isRoute.value) {
          form.days = result.value.meta.cons.days;
          form.budget = result.value.meta.cons.budget;
        }
        variant.value = 'local';
        // 第 67-6 条：新方案可能换了城市，之前点的「我要吃这家」不能留着，
        // 否则新方案里没有那家店时会显示成「自己填的」占位
        Object.keys(mealPicks).forEach(k => delete mealPicks[k]);
        Object.keys(mealCustom).forEach(k => delete mealCustom[k]);
        ElMessage.success('规划完成');
        await nextTick();
        if (!isRoute.value) drawMap(mapEl.value, tilesEl.value, result.value, variant.value);
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        planning.value = false;
      }
    }

    // 顶栏显示的城市跟着结果走：规划完北京之后，别还挂着长沙的标签
    const headCity = computed(() => (result.value && result.value.meta
      ? result.value.meta.city : (meta.value ? meta.value.city : '')));
    const headPois = computed(() => (result.value
      ? result.value.pois.length : (meta.value ? meta.value.poi_count : 0)));
    const headTravel = computed(() => (result.value && result.value.meta
      ? result.value.meta.travel_source : (meta.value ? meta.value.travel_source : '')));

    const currentVariant = computed(() =>
      result.value ? (result.value.variants.find(v => v.key === variant.value) || result.value.variants[0]) : null);
    const currentPlan = computed(() => currentVariant.value ? currentVariant.value.plan : null);
    const currentVerify = computed(() => currentVariant.value ? currentVariant.value.verify : null);

    // 第 59 条：把「吃饭那 60 分钟」也排进当天时间轴。以前它只把后面景点的到达时间往后推，
    // 页面上看不出那段时间在干嘛（老大看到的是「上一站 12:23 走、下一站 13:31 到」）。
    function dayTimeline(d) {
      const out = [];
      (d.stops || []).forEach((s, i) => out.push({
        key: 's' + i, rest: false, t: s.arrive, idx: i,
      }));
      (d.rests || []).forEach((r, i) => out.push({
        key: 'r' + i, rest: true, t: r.start,
        start_str: r.start_str, end_str: r.end_str, min: r.min,
      }));
      return out.sort((a, b) => a.t - b.t);
    }

    const statCards = computed(() => {
      if (!result.value) return [];
      const t = currentPlan.value.totals;
      const v = currentVerify.value;
      return [
        { l: '停留点', v: t.stops + ' 个', s: '共 ' + result.value.pois.length + ' 个候选' },
        { l: '预计花费', v: '¥' + Math.round(t.grand_total || t.total_cost), s: '预算 ¥' + Math.round(t.budget) },
        { l: '人均', v: '¥' + Math.round((t.grand_total || t.total_cost) / (t.travelers || 1)),
          s: (t.travelers || 1) + ' 人 · ' + (t.rooms || 1) + ' 间房' },
        { l: '交通', v: t.km.toFixed(1) + ' km', s: '约 ' + t.travel_min + ' 分钟' },
        { l: '约束满足率', v: v.pass_rate + '%', s: v.passed + '/' + v.total + ' 项通过' },
      ];
    });

    const cmpRows = computed(() => {
      if (!result.value) return [];
      const A = result.value.variants.find(v => v.key === 'greedy');
      const B = result.value.variants.find(v => v.key === 'local');
      const ta = A.plan.totals, tb = B.plan.totals;
      const mk = (name, a, b, higherBetter, fmt) => {
        const better = higherBetter ? b > a : b < a;
        const diff = b - a;
        return {
          name,
          a: fmt(a), b: fmt(b),
          d: Math.abs(diff) < 1e-9 ? '持平'
            : (diff > 0 ? '↑ ' : '↓ ') + fmt(Math.abs(diff)) + (a ? `（${Math.abs(diff / a * 100).toFixed(1)}%）` : ''),
          better: better ? 'b' : (Math.abs(diff) < 1e-9 ? '' : 'a'),
        };
      };
      const f1 = x => x.toFixed(1), f0 = x => Math.round(x).toString();
      return [
        mk('综合得分（效用 − λ×交通分钟）', ta.score, tb.score, true, f1),
        mk('总效用', ta.utility, tb.utility, true, f1),
        mk('停留点', ta.stops, tb.stops, true, f0),
        mk('门票（元）', ta.ticket, tb.ticket, false, f0),
        mk('交通里程（km）', ta.km, tb.km, false, f1),
        mk('交通时间（分钟）', ta.travel_min, tb.travel_min, false, f0),
        mk('预计总花费（元）', ta.total_cost, tb.total_cost, false, f0),
        mk('约束满足率（%）', A.verify.pass_rate, B.verify.pass_rate, true, f1),
      ];
    });

    watch(variant, async () => {
      await nextTick();
      drawMap(mapEl.value, tilesEl.value, result.value, variant.value);
    });

    watch(() => form.sample, async () => {
      await loadMeta();
      await loadPois();
      result.value = null;
    });

    /* ---- 花费构成 ---- */
    const costBars = computed(() => {
      if (!result.value) return [];
      const t = currentPlan.value.totals;
      const budget = t.budget || 1;
      const bars = [
        { l: '门票', v: t.ticket, color: '#4c8dff' },
        { l: '餐饮', v: t.food, color: '#22c48a' },
        { l: '交通', v: t.transport, color: '#f5a623' },
        { l: '住宿', v: t.hotel || 0, color: '#9b6bf2' },
        // 第 64 条：机动是「兜里要带的钱」，不算进规划花费，所以用灰条单独标出来
        { l: '机动', v: t.pocket || 0, color: '#c0c4cc' },
      ];
      // 第 67-3 条：分母不能再只用预算——机动不在 total_cost 里，五条加起来会顶到线外，
      // 看着像超预算了。用「五项之和」和「预算」取大的那个当分母，就永远不会溢出。
      const sum = bars.reduce((a, b) => a + (b.v || 0), 0) || 1;
      const base = Math.max(sum, budget);
      return bars.map(x => ({
        ...x,
        pct: Math.min(100, x.v / base * 100),
        over: x.v > budget,
      }));
    });

    /* ---- 费用明细（第 65 条）：每一分钱怎么来的，一行一行对 ---- */
    const billRows = computed(() => {
      if (!result.value) return [];
      const t = currentPlan.value.totals || {};
      const c = (result.value.meta && result.value.meta.cons) || {};
      const rows = [];
      const persons = t.travelers || 1;
      const days = c.days || t.days || 1;
      const nights = t.nights != null ? t.nights : Math.max(0, days - 1);
      const rooms = t.rooms || 1;

      // 城际：每段一行，单人价 × 折算张数（第 62 条的口径）
      (result.value.legs || []).forEach(l => {
        if (!(l.price > 0)) return;
        const src = l.source === 'schedule' ? '（12306 实查）' : '（估算）';
        rows.push({
          item: '城际 · ' + l.from + '→' + l.to,
          unit: '¥' + l.price + '/人' + src,
          qty: (l.ticket_factor || persons) + ' 张',
          sub: Math.round(l.price_total != null ? l.price_total : l.price * persons),
          note: l.train_code
            ? (l.train_code + ' ' + (l.dep_time || '') + '–' + (l.arr_time || ''))
            : (l.mode === 'drive' ? '自驾，油费过路费未计入' : '按距离和出行方式估算'),
        });
      });

      if (t.hotel > 0) {
        // 单价从总额反推均价：环线各段房价不一样，取 meta 里那一个数会对不上账
        const perNight = t.hotel / Math.max(1, rooms) / Math.max(1, nights);
        rows.push({
          item: '住宿',
          unit: '¥' + Math.round(perNight) + '/晚 × ' + rooms + ' 间',
          qty: nights + ' 晚',
          sub: Math.round(t.hotel),
          note: '房价是估算值，以当日实际价为准',
        });
      }

      if (t.ticket > 0) {
        const paid = [];
        let freeCount = 0;
        (currentPlan.value.days || []).forEach(d => (d.stops || []).forEach(s => {
          if (s.ticket > 0) paid.push(s.name + ' ¥' + s.ticket);
          else freeCount += 1;
        }));
        rows.push({
          item: '门票',
          unit: '按人群折算（成人 1 张，儿童 / 老人 / 学生半张）',
          qty: paid.length + ' 个收费点',
          sub: Math.round(t.ticket),
          note: paid.join('、') + (freeCount ? '；另 ' + freeCount + ' 个点免费' : ''),
        });
      }

      if (t.transport > 0) {
        const cm = c.city_mode || 'mixed';
        const cmMap = {
          drive: { l: '自驾', u: '¥1.2/km（油费过路费）', n: '按车算，跟去几个人没关系' },
          taxi: { l: '打车', u: '起步 ¥13 + ¥2.5/km', n: '每去一个点算一趟，一辆车坐几个都一样' },
          transit: { l: '公交地铁', u: '¥2.5/人次', n: '按人次算，跟公里数关系不大' },
          mixed: { l: '', u: '¥' + (c.km_cost || 2.6) + '/km', n: '打车 / 公交混合口径（没选方式时的老算法）' },
        };
        const cmx = cmMap[cm] || cmMap.mixed;
        rows.push({
          item: '市内交通' + (cmx.l ? ' · ' + cmx.l : ''),
          unit: cmx.u,
          qty: cm === 'transit'
            ? ((t.stops || 0) + ' 趟 × ' + persons + ' 人')
            : (Math.round(t.km || 0) + ' km'),
          sub: Math.round(t.transport),
          note: cmx.n,
        });
      }

      if (t.food > 0) {
        rows.push({
          item: '餐饮',
          unit: '¥' + Math.round(c.food_per_day || 80) + '/人/天 × ' + persons + ' 人',
          qty: days + ' 天',
          sub: Math.round(t.food),
          note: '餐标在表单里改，改完这里跟着变',
        });
      }

      if (t.pocket > 0) {
        rows.push({
          item: '机动 / 零花',
          unit: '¥' + Math.round(c.pocket_per_day || 25) + '/人/天 × ' + persons + ' 人',
          qty: days + ' 天',
          sub: Math.round(t.pocket),
          note: '买水、特产、临时打车——不算进规划，但兜里得带着',
        });
      }
      return rows;
    });

    /* ---- 每天吃什么（第 60 条）：候选能点，也能自己填 ---- */
    const mealPicks = reactive({});      // {'1:lunch': '店名'}
    const mealCustom = reactive({});     // 自己填的输入框

    function mealOptions(d, which) {
      return ((d.meals || {})[which]) || [];
    }
    function mealPickName(d, which) {
      return mealPicks[d.index + ':' + which] || '';
    }
    function setMealPick(d, which, name) {
      const key = d.index + ':' + which;
      const n = String(name || '').trim();
      if (!n) delete mealPicks[key];
      else mealPicks[key] = n;
    }
    function mealShown(d, which) {
      const list = mealOptions(d, which);
      const picked = mealPickName(d, which);
      if (picked) {
        return list.find(x => x.name === picked)
          || { name: picked, category: '自己填的', km: '—' };
      }
      return list[0] || null;
    }

    /* ---- 行程编辑 ---- */
    async function openEdit(row) {
      try {
        const h = await api('/api/history/' + row.id);
        editHid.value = row.id;
        editDays.value = (h.payload.variants.find(v => v.key === 'local')
          || h.payload.variants[0]).plan.days
          .map(d => d.stops.map(s => ({ id: s.id, name: s.name, window: s.window })));
        editOpen.value = true;
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    function moveStop(di, si, dir) {
      const arr = editDays.value[di];
      const j = si + dir;
      if (j < 0 || j >= arr.length) return;
      const tmp = arr[si];
      arr[si] = arr[j];
      arr[j] = tmp;
    }

    function dropStop(di, si) {
      editDays.value[di].splice(si, 1);
    }

    async function applyEdit() {
      if (editBusy.value) return;
      editBusy.value = true;
      try {
        const payload = await api('/api/history/' + editHid.value + '/edit', {
          method: 'POST',
          body: JSON.stringify({ days: editDays.value.map(d => d.map(s => s.id)) }),
        });
        result.value = payload;
        variant.value = 'local';
        editOpen.value = false;
        page.value = 'plan';
        ElMessage.success('调整好了，校验也重跑了');
        await nextTick();
        drawMap(mapEl.value, tilesEl.value, result.value, variant.value);
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        editBusy.value = false;
      }
    }

    /* ---- 账户 ---- */
    function openAccount() {
      accountForm.username = user.value ? user.value.username : '';
      accountForm.oldPassword = '';
      accountForm.newPassword = '';
      accountOpen.value = true;
    }

    async function saveProfile() {
      if (accountBusy.value) return;
      const body = {};
      if (accountForm.username.trim() && accountForm.username.trim() !== user.value.username) {
        body.username = accountForm.username.trim();
      }
      if (accountForm.newPassword) {
        body.old_password = accountForm.oldPassword;
        body.new_password = accountForm.newPassword;
      }
      if (!Object.keys(body).length) {
        ElMessage.warning('没有改动');
        return;
      }
      accountBusy.value = true;
      try {
        const r = await api('/api/profile', { method: 'POST', body: JSON.stringify(body) });
        if (r.token) localStorage.setItem('token', r.token);
        if (r.user) user.value = r.user;
        accountOpen.value = false;
        ElMessage.success('已更新：' + (r.changed || []).join('、'));
        await loadAdminIfNeeded();
      } catch (e) {
        ElMessage.error(e.message);
      } finally {
        accountBusy.value = false;
      }
    }

    async function loadAdminIfNeeded() {
      if (user.value && user.value.role === 'admin') await loadAdmin();
    }

    /* ---- 历史 ---- */
    const saveOpen = ref(false);
    const saveBusy = ref(false);
    const saveForm = reactive({ title: '', public: true });

    function openSave() {
      if (!result.value) return;
      if (guest.value) {
        ElMessage.warning('保存方案要登录，点左下角「登录 / 注册」就行');
        return;
      }
      saveForm.title = `${result.value.meta.city} ${result.value.meta.cons.days} 天 · ${result.value.meta.generated.slice(5, 10)}`;
      saveForm.public = true;
      saveOpen.value = true;
    }

    async function confirmSave() {
      if (saveBusy.value) return;
      if (!saveForm.title.trim()) {
        ElMessage.warning('起个名字');
        return;
      }
      saveBusy.value = true;
      try {
        await api('/api/history', {
          method: 'POST',
          body: JSON.stringify({ title: saveForm.title.trim(), payload: result.value, public: saveForm.public }),
        });
        saveOpen.value = false;
        ElMessage.success(saveForm.public ? '已保存并公开到社区' : '已保存（仅自己可见）');
        await Promise.all([loadHistory(), loadCommunity()]);
      } catch (e) {
        ElMessage.error(e.message || '保存失败');
      } finally {
        saveBusy.value = false;
      }
    }

    async function togglePublic(row) {
      try {
        const next = !(row.is_public === 1 || row.is_public === true);
        await api('/api/history/' + row.id + '/public',
          { method: 'POST', body: JSON.stringify({ public: next }) });
        row.is_public = next ? 1 : 0;
        ElMessage.success(next ? '已公开到社区' : '已设为私密');
        await loadCommunity();
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    async function openHistory(row) {
      try {
        const h = await api('/api/history/' + row.id);
        result.value = h.payload;
        variant.value = 'local';
        page.value = 'plan';
        await nextTick();
        // 环线是多城市的，候选点分散在不同城市，地图画不了，跳过
        if (!(result.value.meta && result.value.meta.mode === 'route')) {
          drawMap(mapEl.value, tilesEl.value, result.value, variant.value);
        }
      } catch (e) {
        ElMessage.error(e.message);
      }
    }

    async function removeHistory(row) {
      try {
        await ElMessageBox.confirm('删掉「' + row.title + '」？', '确认删除', { type: 'warning' });
        await api('/api/history/' + row.id, { method: 'DELETE' });
        ElMessage.success('已删除');
        await loadHistory();
      } catch (e) {
        if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '删除失败');
      }
    }

    const downloadJson = () => {
      if (!result.value) return;
      const blob = new Blob([JSON.stringify(result.value, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'plan.json';
      a.click();
      URL.revokeObjectURL(a.href);
    };

    const saveBlob = (text, name, type) => {
      const blob = new Blob([text], { type: type || 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      a.click();
      URL.revokeObjectURL(a.href);
    };

    // 第 14 条：导出 .ics，能直接导进手机/电脑日历（每个景点一个日程）
    // 转格式放在后端（/api/ics），这样同一份逻辑能被接口测试，不用在两边各写一遍
    const downloadIcs = async () => {
      if (!result.value) return;
      try {
        const r = await fetch('/api/ics', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ payload: result.value, variant: variant.value }),
        });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          ElMessage.warning(e.detail || '导日历失败');
          return;
        }
        saveBlob(await r.text(), '行程.ics', 'text/calendar;charset=utf-8');
        ElMessage.success('导出好了，手机日历里能直接打开');
      } catch (err) {
        ElMessage.error('导日历失败：' + err);
      }
    };

    const downloadMd = () => {
      if (!result.value) return;
      const p = currentPlan.value, m = result.value.meta, v = currentVerify.value;
      const win = result.value.variants.find(x => x.key === variant.value);
      let md = `# ${m.city} · ${m.cons.days} 天行程规划\n\n`;
      md += `> ${win.label}　|　生成时间 ${m.generated}\n\n`;
      md += `- 预算 ¥${Math.round(m.cons.budget)}，预计花费 ¥${Math.round(p.totals.total_cost)}\n`;
      md += `- 每天 ${m.cons.day_start} – ${m.cons.day_end}，住宿 ${m.cons.hotel}\n`;
      md += `- 停留 ${p.totals.stops} 个点，交通 ${p.totals.km} km / 约 ${p.totals.travel_min} 分钟\n`;
      md += `- 约束满足率 ${v.pass_rate}%（${v.passed}/${v.total}）\n\n`;
      // 环线：把城际交通单独列一段，不然导出只看到市内
      if (m.mode === 'route' && result.value.legs) {
        md += `## 城际交通\n\n| 出发 | 到达 | 方式 | 耗时 | 票价 | 备注 |\n|---|---|---|---|---|---|\n`;
        result.value.legs.forEach(l => {
          md += `| ${l.from} | ${l.to} | ${l.mode_label} | ${l.duration} | ${l.price ? '¥' + l.price : '不计'} | ${l.note || '—'} |\n`;
        });
        md += `\n`;
        if (result.value.segments) {
          md += `## 分段住宿\n\n| 段 | 城市 | 到达方式 | 住宿 | 房价 |\n|---|---|---|---|---|\n`;
          result.value.segments.forEach(sg => {
            md += `| 段 ${sg.index + 1} | ${sg.city} | ${sg.transport_label} | ${sg.hotel.name} | ¥${sg.hotel.price}/晚 |\n`;
          });
          md += `\n`;
        }
      }
      p.days.forEach(d => {
        const head = d.date ? `${d.date} ${d.weekday}${d.segment_city ? ' · ' + d.segment_city : ''}` : `第 ${d.index} 天`;
        md += `## ${head}${d.on_road ? '（在路上）' : ''}（${d.total_km} km / 路上 ${d.travel_min} 分钟）\n\n`;
        if (!d.stops.length) { md += `_这一天没安排点${d.on_road_note ? '：' + d.on_road_note : ''}_\n\n`; return; }
        md += `| 时间 | 地点 | 开放 | 门票 | 效用 |\n|---|---|---|---|---|\n`;
        d.stops.forEach(s => {
          md += `| ${s.arrive_str}–${s.leave_str} | ${s.name} | ${s.window} | ¥${s.ticket} | ${s.utility} |\n`;
        });
        md += `\n`;
      });
      md += `## 硬约束复核\n\n`;
      v.checks.forEach(c => { md += `- ${c.ok ? '✓' : '✗'} **${c.name}**：${c.detail}\n`; });
      md += `\n---\n\n数据：${m.dataset === 'amap' ? '高德 Web 服务' : '示例集'} / 交通：${m.travel_source === 'amap' ? '高德驾车路径规划' : '直线估算'}。${m.data_note}\n`;
      saveBlob(md, `行程单-${m.city}-${m.cons.days}天.md`, 'text/markdown;charset=utf-8');
    };

    // 住宿表格里给选中那行加个底色
    const hotelRowClass = ({ row }) => (row.picked ? 'hotel-picked' : '');

    // 第 68 条：结果页直接点「住这家」/「还是让算法选」，按新条件重跑一次（1 秒多）
    async function pickHotelRow(name) {
      if (form.hotelPick === name) return;
      form.hotelPick = name;
      await runPlan();
    }
    async function resetHotelPick() {
      if (!form.hotelPick) return;
      form.hotelPick = '';
      await runPlan();
    }

    const printPlan = () => window.print();

    onMounted(async () => {
      // 简易 hash 路由：#/plan #/history #/pois #/about
      const readHash = () => (location.hash.replace(/^#\/?/, '') || 'plan');
      page.value = readHash();
      window.addEventListener('hashchange', () => { page.value = readHash(); });
      watch(page, p => {
        // 管理后台只对 admin 开放：手改 hash 也进不去
        if (p === 'admin' && !(user.value && user.value.role === 'admin')) {
          page.value = 'plan';
          ElMessage.warning('管理后台只有管理员能进');
          return;
        }
        // 游客只能看「规划」和「关于」：其余页面都要查库，后端也不放行
        if (guest.value && ['history', 'community', 'ranking', 'kb', 'pois', 'admin'].includes(p)) {
          page.value = 'plan';
          ElMessage.warning('游客只能试跑规划，历史 / 社区 / 候选点这些要登录');
          return;
        }
        location.hash = '#/' + p;
        if (p === 'community') loadCommunity();
        else if (p === 'ranking') loadRanking();
        else if (p === 'kb') loadDocs();
        else if (p === 'admin') loadAdmin();
      });

      if (!localStorage.getItem('token')) {
        // 第 30 条：带 ?guest=1 打开就直接进游客模式，方便发一条「不登录也能试」的链接
        if (new URLSearchParams(location.search).get('guest') === '1') {
          await enterGuest();
        }
        return;
      }
      try {
        user.value = await api('/api/me');
        await boot();
        // 直接带 hash 打开时 watch 还没挂上，这里补一次
        if (page.value === 'admin' && user.value.role !== 'admin') {
          page.value = 'plan';
          ElMessage.warning('管理后台只有管理员能进');
        } else if (page.value === 'admin') await loadAdmin();
        else if (page.value === 'kb') await loadDocs();
        else if (page.value === 'community') await loadCommunity();
        else if (page.value === 'ranking') await loadRanking();
        // 带 ?query= 打开时自动跑一次，方便分享一条行程
        const qs = new URLSearchParams(location.search);
        if (qs.get('route') === '1') {
          // 环线可直接分享：?route=1&from=石家庄&date=2026-11-10&budget=4000&c1=北京&d1=3&t1=train&c2=杭州&d2=3&t2=plane
          form.mode = 'route';
          form.departFrom = qs.get('from') || '';
          form.startDate = qs.get('date') || tomorrowIso();
          if (qs.get('budget')) form.budget = Number(qs.get('budget'));
          // 人数跟单城那条分享链接对齐：?adults=2&children=1&kids_free=0&seniors=0&room=3
          if (qs.get('adults')) form.adults = Number(qs.get('adults'));
          if (qs.get('children')) form.children = Number(qs.get('children'));
          if (qs.get('kids_free')) form.childrenFree = Number(qs.get('kids_free'));
          if (qs.get('seniors')) form.seniors = Number(qs.get('seniors'));
          if (qs.get('students')) form.students = Number(qs.get('students'));
          if (qs.get('food')) form.foodPerDay = Number(qs.get('food'));
          if (qs.get('arrive')) form.arriveStart = qs.get('arrive');
          if (qs.get('depart')) form.departPrefer = qs.get('depart');
          if (qs.get('citymode')) form.cityMode = qs.get('citymode');
          if (qs.get('room')) form.roomBeds = Number(qs.get('room'));
          const seg = [];
          for (let i = 1; i <= 3; i++) {
            const c = qs.get('c' + i);
            if (!c) break;
            seg.push({
              city: c,
              days: Number(qs.get('d' + i) || 3),
              transport: qs.get('t' + i) || 'train',
            });
          }
          if (seg.length) form.segments = seg;
          await runPlan();
        } else if (qs.get('query')) {
          form.query = qs.get('query');
          if (qs.get('days')) { form.days = Number(qs.get('days')); touched.days = true; }
          if (qs.get('budget')) { form.budget = Number(qs.get('budget')); touched.budget = true; }
          // 出行人数和房型也能分享：?adults=2&children=1&kids_free=0&seniors=0&room=3
          if (qs.get('adults')) form.adults = Number(qs.get('adults'));
          if (qs.get('children')) form.children = Number(qs.get('children'));
          if (qs.get('kids_free')) form.childrenFree = Number(qs.get('kids_free'));
          if (qs.get('seniors')) form.seniors = Number(qs.get('seniors'));
          if (qs.get('food')) form.foodPerDay = Number(qs.get('food'));
          if (qs.get('arrive')) form.arriveStart = qs.get('arrive');
          if (qs.get('depart')) form.departPrefer = qs.get('depart');
          if (qs.get('citymode')) form.cityMode = qs.get('citymode');
          if (qs.get('room')) form.roomBeds = Number(qs.get('room'));
          if (qs.get('date')) form.startDate = qs.get('date');
          if (qs.get('city')) form.city = qs.get('city');
          // 第 68 条：分享链接也能带上「住哪家」
          if (qs.get('hotel')) {
            form.hotelPick = qs.get('hotel');
            await loadHotels();     // 城市可能也是链接带进来的，先按这一城把候选取回来
          }
          // 第 69 条：房型分配也能分享，格式 ?rooms=1:1,2:1（床位数:间数）
          if (qs.get('rooms')) {
            qs.get('rooms').split(',').forEach(pair => {
              const [b, n] = pair.split(':').map(Number);
              if ([1, 2, 3].includes(b) && n > 0) form.rooms[b] = n;
            });
          }
          if (qs.get('kb') === '1') form.useKb = true;
          await runPlan();
        }
      } catch (e) {
        localStorage.removeItem('token');
      }
    });

    return {
      page, user, guest, enterGuest, needLogin, authMode, authBusy, authForm, doAuth, logout,
      dayOptions,
      meta, pois, form, touched, planning, result, variant, history, histBusy,
      community, ranking, adminStats, adminUsers, adminHistory,
      socialOpen, socialRow, socialLike, socialFav, socialComments, commentText, commentBusy,
      mapEl, tilesEl, TAGS, DAYC,
      runPlan, currentPlan, currentVerify, currentVariant, statCards, cmpRows, dayTimeline, billRows,
      mealOptions, mealPickName, setMealPick, mealShown, mealCustom,
      saveOpen, saveBusy, saveForm, openSave, confirmSave, togglePublic,
      openHistory, removeHistory, loadHistory, downloadJson, downloadMd, downloadIcs, printPlan,
      loadCommunity, loadRanking, openSocial, toggleLike, toggleFav, sendComment,
      loadAdmin, setRole, adminRemove,
      costBars, hotelRowClass, hotelOptions, pickedHotel, roomPlanTotal, pickHotelRow, resetHotelPick, editOpen, editBusy, editDays, openEdit, moveStop, dropStop, applyEdit,
      accountOpen, accountBusy, accountForm, openAccount, saveProfile,
      kbDocs, kbForm, kbBusy, kbQuery, kbResult, kbSearchBusy,
      loadDocs, pickFile, uploadDoc, searchKb, removeDoc, onCityChange, resetTouched,
      headCity, headPois, headTravel,
      isRoute, TRANSPORTS, AVOID, addSegment, removeSegment,
    };
  },
});

app.use(ElementPlus, { locale: ElementPlusLocaleZhCn });
app.mount('#app');
