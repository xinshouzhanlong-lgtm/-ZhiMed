/* 管理端：md 提案-预览-确认 + 医生照片 + 数据源健康 */
const $ = (id) => document.getElementById(id);
const store = {
  get token() { return localStorage.getItem('guide_admin_token') || ''; },
  set token(v) { localStorage.setItem('guide_admin_token', v); },
  clear() { localStorage.removeItem('guide_admin_token'); },
};
let currentPatch = null;
let doctors = [];

function authHeaders(extra = {}) {
  return Object.assign({ Authorization: 'Bearer ' + store.token }, extra);
}

async function api(path, opts = {}) {
  const resp = await fetch(path, Object.assign({}, opts, {
    headers: authHeaders(opts.headers || {}),
  }));
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (e) { data = { detail: text }; }
  if (!resp.ok) {
    const d = data && data.detail;
    const msg = (d && d.message) || (typeof d === 'string' ? d : JSON.stringify(d || {}));
    const err = new Error(msg || ('HTTP ' + resp.status));
    err.status = resp.status; err.code = d && d.code;
    throw err;
  }
  return data;
}

/* ── 登录 ── */
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('loginErr').textContent = '';
  try {
    const data = await fetch('/api/auth/password', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('username').value, password: $('password').value }),
    }).then((r) => r.json().then((j) => ({ ok: r.ok, j })));
    if (!data.ok) throw new Error(data.j.detail || '登录失败');
    store.token = data.j.token;
    enterApp(data.j.principal);
  } catch (err) {
    $('loginErr').textContent = err.message;
  }
});

function enterApp(principal) {
  $('login').hidden = true;
  $('app').hidden = false;
  $('who').textContent = `${principal.sub}（${principal.role === 'admin' ? '管理员' : '医生'}）· ${principal.entry}`;
  boot();
}

async function tryResume() {
  if (!store.token) return;
  try {
    const p = await api('/api/auth/me');
    enterApp(p);
  } catch (e) { store.clear(); }
}

/* ── Tab ── */
document.querySelectorAll('.tab').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    ['panelMd', 'panelDoctor', 'panelSource'].forEach((id) => { $(id).hidden = true; });
    $(btn.dataset.panel).hidden = false;
  };
});

/* ── 启动加载 ── */
async function boot() {
  const deptData = await api('/api/admin/departments');
  const sel = $('deptSel');
  sel.innerHTML = '';
  deptData.departments.forEach((d) => {
    const o = document.createElement('option');
    o.value = d.department_id;
    o.textContent = d.name;
    sel.appendChild(o);
  });
  await loadDoctors();
  await loadVersions();
  await loadHealth();
}

async function loadDoctors() {
  const res = await fetch('/api/schedule/today').then((r) => r.json());
  const deptIds = (res.rows || []).map((r) => r.department_id);
  const set = new Set(deptIds);
  const out = [];
  for (const id of set) {
    const d = await fetch('/api/departments/' + encodeURIComponent(id)).then((r) => r.json()).catch(() => null);
    if (!d) continue;
    // 逐个科室取医生：复用 /api/voice/turn 太重，这里直接读医生列表接口
    const list = await api('/api/admin/doctors?dept_id=' + encodeURIComponent(id)).catch(() => ({ doctors: [] }));
    (list.doctors || []).forEach((doc) => out.push(doc));
  }
  doctors = out;
  const sel = $('doctorSel');
  sel.innerHTML = '';
  if (!out.length) {
    const o = document.createElement('option');
    o.textContent = '（无医生数据）'; o.value = '';
    sel.appendChild(o);
    return;
  }
  out.forEach((doc) => {
    const o = document.createElement('option');
    o.value = doc.doctor_id;
    o.textContent = `${doc.name} · ${doc.title} · ${doc.department_id}`;
    sel.appendChild(o);
  });
  await loadPhotoPreview();
}

async function loadPhotoPreview() {
  const id = $('doctorSel').value;
  if (!id) return;
  const doc = await fetch('/api/doctors/' + encodeURIComponent(id)).then((r) => r.json()).catch(() => null);
  const box = $('photoPreview');
  if (doc && doc.photo) {
    box.innerHTML = `<img src="${doc.photo.url_480}" alt="头像">`;
  } else {
    box.textContent = '无照片';
  }
}
$('doctorSel').onchange = loadPhotoPreview;

/* ── md 提案 ── */
$('proposeBtn').onclick = async () => {
  const newValue = $('newValue').value.trim();
  if (!newValue) { alert('请填写新值'); return; }
  $('diffBox').hidden = true;
  try {
    const data = await api('/api/admin/md/propose', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        department_id: $('deptSel').value,
        field: $('fieldSel').value,
        new_value: newValue,
        channel: lastAsr ? 'voice' : 'manual',
        raw_utterance: lastAsr ? lastAsr.text : '',
        asr_confidence: lastAsr ? lastAsr.confidence : 1.0,
      }),
    });
    currentPatch = { patch: data.patch, token: data.confirm_token };
    $('diffText').textContent = data.patch.diff;
    $('diffConf').textContent = `原值：${data.patch.before_value} → 新值：${data.patch.after_value}` +
      (lastAsr ? ` · 语音置信度 ${(lastAsr.confidence * 100).toFixed(0)}%` : '');
    $('diffBox').hidden = false;
    $('diffBox').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (err) {
    alert(describeErr(err));
  }
};

$('cancelBtn').onclick = () => { $('diffBox').hidden = true; currentPatch = null; };

$('commitBtn').onclick = async () => {
  if (!currentPatch) return;
  $('commitBtn').disabled = true;
  try {
    const r = await api('/api/admin/md/commit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ patch_id: currentPatch.patch.patch_id, confirm_token: currentPatch.token }),
    });
    alert(`已保存，文件校验：${r.verified ? '通过' : '失败'}，hash ${r.content_hash.slice(0, 12)}…`);
    $('diffBox').hidden = true;
    currentPatch = null;
    lastAsr = null;
    $('newValue').value = '';
    await loadVersions();
    await boot();
  } catch (err) {
    alert(describeErr(err));
  } finally {
    $('commitBtn').disabled = false;
  }
};

function describeErr(err) {
  const map = {
    E_LOW_CONFIDENCE: '语音识别置信度过低，请手动修正文本后再试',
    E_STALE_BASE: '文件已被他人修改，请重新生成提案',
    E_NO_CHANGE: '新值与当前值相同',
    E_FIELD_NOT_ALLOWED: '该字段不允许修改',
    E_NO_TOKEN: '确认令牌无效或已过期',
  };
  return map[err.code] || err.message;
}

async function loadVersions() {
  const data = await api('/api/admin/md/versions').catch(() => ({ versions: [] }));
  const box = $('versions');
  box.innerHTML = '';
  if (!data.versions.length) { box.innerHTML = '<div class="muted">暂无版本记录</div>'; return; }
  data.versions.forEach((v) => {
    const el = document.createElement('div');
    el.className = 'vitem';
    const t = new Date(v.created_at).toLocaleString();
    el.innerHTML = `<span>${t}</span><span>${v.channel}</span><span>${v.author}</span>
      <span>${v.content_hash.slice(0, 10)}…</span><span>${v.verified ? '已校验' : '未校验'}</span>`;
    const btn = document.createElement('button');
    btn.className = 'ghost';
    btn.textContent = '回滚';
    btn.onclick = async () => {
      if (!confirm('确认回滚到该版本？')) return;
      try {
        await api('/api/admin/md/rollback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version_id: v.id }),
        });
        await loadVersions();
        await boot();
        alert('已回滚');
      } catch (err) { alert(err.message); }
    };
    el.appendChild(btn);
    box.appendChild(el);
  });
}

/* ── 语音录入（管理端） ── */
let lastAsr = null;
$('voiceBtn').onclick = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert('当前浏览器不支持语音识别，请直接输入'); return; }
  const rec = new SR();
  rec.lang = 'zh-CN';
  rec.interimResults = false;
  $('voiceBtn').textContent = '🎤 识别中…';
  rec.onresult = (ev) => {
    const text = ev.results[0][0].transcript;
    const confidence = ev.results[0][0].confidence || 0.9;
    lastAsr = { text, confidence };
    $('newValue').value = text;
    $('asrConf').textContent = `识别：${text}（置信度 ${(confidence * 100).toFixed(0)}%）`;
    if (confidence < 0.85) {
      $('asrConf').classList.add('warnc');
      alert('识别置信度低于 85%，已阻止生成提案。请人工修正文本后再提交。');
    } else {
      $('asrConf').classList.remove('warnc');
    }
  };
  rec.onerror = () => { $('voiceBtn').textContent = '🎤 语音录入'; };
  rec.onend = () => { $('voiceBtn').textContent = '🎤 语音录入'; };
  rec.start();
};

/* ── 医生照片 ── */
$('uploadBtn').onclick = async () => {
  const file = $('photoFile').files[0];
  const doctorId = $('doctorSel').value;
  if (!file || !doctorId) { alert('请选择医生与图片'); return; }
  if (!$('consentAck').checked) { alert('代传必须勾选"已取得肖像使用授权"'); return; }
  const fd = new FormData();
  fd.append('file', file);
  fd.append('consent_ack', 'true');
  $('uploadMsg').textContent = '上传中…';
  try {
    const r = await api(`/api/doctors/${encodeURIComponent(doctorId)}/photo`, { method: 'POST', body: fd });
    $('uploadMsg').textContent = r.deduped ? '上传成功（命中内容去重，未重写文件）' : '上传成功';
    await loadPhotoPreview();
  } catch (err) {
    const map = { E_BAD_MAGIC: '文件不是有效图片或格式不支持', E_SVG_REJECTED: '不接受 SVG',
                  E_BAD_DIMENSION: '图片尺寸不符合要求', E_TOO_LARGE: '图片过大' };
    $('uploadMsg').textContent = map[err.code] || err.message;
  }
};

$('linkBtn').onclick = async () => {
  const doctorId = $('doctorSel').value;
  if (!doctorId) return;
  try {
    const r = await api('/api/admin/magic-links', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doctor_id: doctorId }),
    });
    $('linkRow').hidden = false;
    $('linkOut').value = r.url;
  } catch (err) { alert(err.message); }
};

$('copyLink').onclick = async () => {
  try { await navigator.clipboard.writeText($('linkOut').value); $('copyLink').textContent = '已复制'; }
  catch (e) { $('linkOut').select(); }
};

/* ── 数据源 ── */
async function loadHealth() {
  const d = await api('/api/admin/source-health').catch(() => null);
  const box = $('healthBox');
  if (!d) { box.textContent = '加载失败'; return; }
  box.innerHTML = `
    <div class="k">熔断器</div><div class="${d.breaker.state === 'closed' ? 'ok' : 'warnc'}">${d.breaker.state}（连续失败 ${d.breaker.failures}）</div>
    <div class="k">默认策略</div><div>${d.policy_default.mode} · 超时 ${d.policy_default.timeout_ms}ms</div>
    ${(d.adapters || []).map((a) => `<div class="k">${a.id}</div><div>${a.mode ? 'mode=' + a.mode : ''} ${a.ok ? '可用' : '不可用'} ${a.configured === false && a.mode === 'http' ? '（未配置真实地址）' : ''}</div>`).join('')}
  `;
  const cb = $('conflictBox');
  if (!d.conflicts.length) { cb.textContent = '无'; cb.className = 'muted'; }
  else {
    cb.className = '';
    cb.innerHTML = d.conflicts.map((c) => `
      <div class="vitem"><span>${c.date}</span><span>${c.department_id}</span>
      <span>HIS: ${c.api_value}</span><span>手动: ${c.manual_value}</span></div>`).join('');
  }
}

tryResume();
