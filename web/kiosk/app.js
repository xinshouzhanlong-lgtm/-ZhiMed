/* 患者端主屏：今日排班网格 + 状态条 + 语音抽屉 */
const state = { rows: [], degraded: false, disclaimer: '' };

const $ = (id) => document.getElementById(id);
const deviceId = (() => {
  const k = 'guide_device_id';
  let v = localStorage.getItem(k);
  if (!v) { v = 'dev-' + Math.random().toString(36).slice(2, 10); localStorage.setItem(k, v); }
  return v;
})();

function pad(n) { return String(n).padStart(2, '0'); }
function clock() {
  const d = new Date();
  $('clock').textContent = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  $('todayLine').textContent = `更新于 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
setInterval(clock, 30000); clock();

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

async function loadSchedule() {
  try {
    const resp = await fetch('/api/schedule/today');
    const data = await resp.json();
    state.rows = data.rows || [];
    state.degraded = !!data.degraded;
    state.disclaimer = data.disclaimer || '';
    render(data);
  } catch (e) {
    $('statusText').textContent = '排班加载失败';
    $('dot').className = 'dot err';
  }
}

function render(data) {
  const dot = $('dot');
  const bar = $('degradedBar');

  if (!data.ok) {
    dot.className = 'dot err';
    $('statusText').textContent = '排班暂不可用';
    bar.hidden = false;
    bar.textContent = '排班数据暂时取不到，请咨询导医台。';
  } else if (data.degraded) {
    dot.className = 'dot warn';
    const src = data.meta ? (data.meta.source === 'api' ? 'HIS' : '手动') : '手动';
    $('statusText').textContent = `降级 · ${src}数据 · ${data.reason || ''}`;
    bar.hidden = false;
    bar.textContent = '当前排班为人工维护数据，可能不是最新。请以现场导医为准。';
  } else {
    dot.className = 'dot';
    const t = data.meta ? new Date(data.meta.fetched_at) : new Date();
    $('statusText').textContent = `数据正常 · HIS 实时 · ${pad(t.getHours())}:${pad(t.getMinutes())}`;
    bar.hidden = true;
  }

  const grid = $('grid');
  grid.innerHTML = '';
  const onDuty = state.rows.filter((r) => r.status === 'on_duty');
  const suspended = state.rows.filter((r) => r.status !== 'on_duty');
  $('emptyHint').hidden = state.rows.length > 0;

  [...onDuty, ...suspended].forEach((r) => {
    const el = document.createElement('div');
    el.className = 'card' + (r.status === 'on_duty' ? '' : ' off');
    const srcLabel = r.source === 'api' ? 'HIS' : '手动';
    const badgeCls = r.source === 'api' ? 'badge' : 'badge manual';
    const stale = r.stale ? '<span class="badge stale">已过期</span>' : '';
    el.innerHTML = `
      <h3>${r.department_name || r.department_id}</h3>
      <div class="loc">${r.location || '位置待补充'}</div>
      <div class="meta">
        <span>${r.status === 'on_duty' ? (r.doctor_count || 0) + ' 位医生出诊' : '今日停诊'}</span>
        <span class="${badgeCls}">${srcLabel}</span>
      </div>
      ${stale}`;
    if (r.status === 'on_duty') {
      el.onclick = () => ask(`/${r.department_name} 在几楼`);
    }
    grid.appendChild(el);
  });
}

/* 供 voice.js 调用：打开抽屉并提问 */
function ask(text) {
  window.Voice && window.Voice.submit(text);
  window.Voice && window.Voice.open();
}

/* 科室卡 / 医生卡点击 → 追问 */
window.GuideKiosk = {
  deviceId,
  onDeptClick(deptId, name) { ask(`${name} 在几楼`); },
  onDoctorClick(doctorName) { ask(`${doctorName} 医生什么时候坐诊`); },
};

loadSchedule();
setInterval(loadSchedule, 60000);
