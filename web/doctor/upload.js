/* 医生自助页：从 URL fragment 取 token → 换 Principal → 上传头像。
   token 放 fragment（#t=）而不是 query：不进 Nginx access log、不进 Referer。 */
const $ = (id) => document.getElementById(id);
let token = localStorage.getItem('guide_doctor_jwt') || '';
let magicToken = '';
let principal = null;

function fail(msg) {
  $('title').textContent = '无法打开';
  $('sub').textContent = '';
  $('card').hidden = true;
  $('errBox').hidden = false;
  $('errBox').textContent = msg;
}

async function boot() {
  const frag = (location.hash || '').replace(/^#/, '');
  const params = new URLSearchParams(frag);
  magicToken = params.get('t') || '';
  if (magicToken) {
    // 换票后立刻从地址栏抹掉 token，避免被截图/复制外泄
    history.replaceState(null, '', location.pathname);
    try {
      const r = await fetch('/api/auth/exchange', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: magicToken }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        return fail(j.detail || '链接已失效或已使用，请联系管理员重新发送。');
      }
      const data = await r.json();
      token = data.token;
      principal = data.principal;
      localStorage.setItem('guide_doctor_jwt', token);
    } catch (e) {
      return fail('网络异常，请稍后重试。');
    }
  } else if (!token) {
    return fail('缺少访问链接。请向管理员索取照片上传链接。');
  } else {
    try {
      const r = await fetch('/api/auth/me', { headers: { Authorization: 'Bearer ' + token } });
      if (!r.ok) throw new Error('expired');
      principal = await r.json();
    } catch (e) {
      localStorage.removeItem('guide_doctor_jwt');
      return fail('登录已过期，请重新打开管理员发送的链接。');
    }
  }
  await render();
}

async function render() {
  $('title').textContent = '医生照片';
  $('sub').textContent = '上传后由系统统一裁剪压缩，无需自行处理尺寸。';
  $('card').hidden = false;

  const doc = await fetch('/api/doctors/' + encodeURIComponent(principal.sub)).then((r) => r.json()).catch(() => null);
  $('who').textContent = doc ? `${doc.name} · ${doc.title}` : principal.sub;
  const box = $('avatar');
  if (doc && doc.photo) box.innerHTML = `<img src="${doc.photo.url_480}" alt="头像">`;
  else box.textContent = doc ? doc.name.slice(0, 1) : '—';
}

$('uploadBtn').onclick = async () => {
  const file = $('file').files[0];
  if (!file) { $('msg').textContent = '请先选择图片'; return; }
  const fd = new FormData();
  fd.append('file', file);
  const headers = { Authorization: 'Bearer ' + token };
  if (magicToken) headers['X-Magic-Link'] = magicToken;

  $('uploadBtn').disabled = true;
  $('msg').textContent = '上传中…';
  try {
    const r = await fetch(`/api/doctors/${encodeURIComponent(principal.sub)}/photo`, {
      method: 'POST', headers, body: fd,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const map = { E_BAD_MAGIC: '文件不是有效图片（仅支持 JPEG/PNG/WebP）',
                    E_SVG_REJECTED: '不接受 SVG', E_BAD_DIMENSION: '图片太小，短边需 ≥240px',
                    E_TOO_LARGE: '图片超过 10MB' };
      $('msg').textContent = map[j.detail && j.detail.code] || (j.detail && j.detail.message) || '上传失败';
      return;
    }
    $('msg').textContent = j.deduped ? '已上传（同图已存在，未重复写入）' : '已上传，感谢配合';
    await render();
  } catch (e) {
    $('msg').textContent = '网络异常，请重试';
  } finally {
    $('uploadBtn').disabled = false;
  }
};

boot();
