/* 语音输入：点一下开始说（不是必须按住），说完自动提交；不支持识别的环境自动降级为打字。
   三个入口共用同一套逻辑：主屏条「语音」按钮、右下悬浮按钮、抽屉内放大按钮。 */
(function () {
  const $ = (id) => document.getElementById(id);
  const body = $('drawerBody');
  const drawer = $('drawer');
  const barInput = $('barInput');
  const askInput = $('askInput');
  const hint = $('askHint');
  const DEFAULT_HINT = '点「语音」说话，或直接打字提问 · 也可以问「今天有哪些科室出诊」';

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recog = null;
  let listening = false;
  let sessionId = (() => {
    const k = 'guide_session_id';
    let v = sessionStorage.getItem(k);
    if (!v) { v = 's-' + Math.random().toString(36).slice(2, 10); sessionStorage.setItem(k, v); }
    return v;
  })();

  // ── 小工具 ──
  let toastTimer = null;
  function toast(msg, ms = 2600) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function openDrawer() { drawer.hidden = false; }
  function closeDrawer() { drawer.hidden = true; stopListen(); }
  function scrollDown() { body.scrollTop = body.scrollHeight; }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  }

  // ── 渲染 ──
  function bubble(role, html) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + role;
    wrap.innerHTML = `<div class="bubble">${html}</div>`;
    body.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function avatar(name, photoUrl) {
    if (photoUrl) {
      return `<img src="${photoUrl}" alt="${escapeHtml(name)}" onerror="this.parentNode.textContent='${escapeHtml(name).slice(0, 1)}'">`;
    }
    return escapeHtml(name).slice(0, 1);
  }

  function renderAnswer(data) {
    const cards = data.cards || [];
    const deptCards = cards.filter((c) => c.kind === 'dept');
    const docCards = cards.filter((c) => c.kind === 'doctor');

    let html = escapeHtml(data.display_text || data.text || '');
    if (data.degraded) {
      html = '<div class="hint" style="margin-bottom:6px">⚠️ 数据来源：人工维护，可能不是最新</div>' + html;
    }
    const wrap = bubble('bot', html);

    if (deptCards.length) {
      const box = document.createElement('div');
      box.className = 'cards';
      deptCards.forEach((c) => {
        const el = document.createElement('div');
        el.className = 'mini';
        el.innerHTML = `<div class="t">${escapeHtml(c.title)}</div><div class="s">${escapeHtml(c.subtitle)}</div>`;
        el.onclick = () => submit(`${c.title} 在几楼`);
        box.appendChild(el);
      });
      wrap.appendChild(box);
    }

    if (docCards.length) {
      const box = document.createElement('div');
      box.className = 'avatars';
      docCards.forEach((c) => {
        const el = document.createElement('div');
        el.className = 'avatar-card';
        el.innerHTML = `
          <div class="avatar">${avatar(c.title, c.photo_url)}</div>
          <div class="n">${escapeHtml(c.title)}</div>
          <div class="r">${escapeHtml((c.subtitle || '').split('·')[0].trim())}</div>`;
        el.onclick = () => submit(`${c.title} 医生什么时候坐诊`);
        box.appendChild(el);
      });
      wrap.appendChild(box);
    }

    if (data.latency) {
      const lat = document.createElement('div');
      lat.className = 'lat';
      lat.textContent = `总耗时 ${data.latency.total_ms} ms · 通道 ${data.path}`;
      wrap.appendChild(lat);
    }

    speak(data.text);
    $('disclaimer').textContent = data.disclaimer || '';
  }

  function speak(text) {
    if (!window.speechSynthesis || !text) return;
    try {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN';
      u.rate = 1.0;
      speechSynthesis.speak(u);
    } catch (e) { /* 播报失败不影响阅读 */ }
  }

  // ── 提问 ──
  async function submit(text) {
    const clean = (text || '').trim();
    if (!clean) return;
    openDrawer();
    bubble('user', escapeHtml(clean));
    barInput.value = '';
    askInput.value = '';
    try {
      const resp = await fetch('/api/voice/turn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, text: clean, device_id: window.GuideKiosk.deviceId }),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      renderAnswer(await resp.json());
    } catch (e) {
      bubble('bot', '网络异常，请稍后再试或咨询导医台。');
    }
  }

  // ── 识别状态 ──
  function setListening(on, msg) {
    listening = on;
    const micBtn = $('micBtn');
    micBtn.classList.toggle('listening', on);
    micBtn.querySelector('.mic-label').textContent = on ? '聆听中' : '语音';
    $('voiceBtn').classList.toggle('listening', on);
    $('voiceBtn').querySelector('.fab-text').textContent = on ? '正在聆听…' : '按住或点击说话';
    $('bigMic').classList.toggle('listening', on);
    $('bigMic').textContent = on ? '正在聆听…（再点一下结束）' : '🎤 点一下开始说话';
    hint.classList.toggle('listening', on);
    hint.textContent = msg || (on ? '正在聆听…说完稍等一下，或再点一次结束' : DEFAULT_HINT);
  }

  function errText(code) {
    return {
      'not-allowed': '麦克风未授权。可在浏览器地址栏允许麦克风，或直接打字提问。',
      'service-not-allowed': '当前环境禁止使用语音识别，请直接打字提问。',
      'no-speech': '没有听到声音，请再说一次。',
      'audio-capture': '没有找到麦克风设备，请检查设备或直接打字。',
      'network': '语音识别服务不可达，请直接打字提问。',
    }[code] || ('语音识别失败（' + code + '），请直接打字提问。');
  }

  function startListen() {
    if (listening) return;
    if (!SR) {
      toast('当前浏览器不支持语音识别，请直接用输入框打字提问');
      barInput.focus();
      return;
    }
    recog = new SR();
    recog.lang = 'zh-CN';
    recog.continuous = false;     // 说完自动结束，不需要长按
    recog.interimResults = true;
    recog.maxAlternatives = 1;

    recog.onstart = () => setListening(true);
    recog.onresult = (ev) => {
      let finalText = '';
      let interim = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) finalText += r[0].transcript;
        else interim += r[0].transcript;
      }
      // 实时反馈：识别中的文字直接回显到输入条，用户能看到"它在听"
      if (interim) barInput.value = interim;
      if (finalText) {
        const conf = ev.results[ev.results.length - 1][0].confidence || 0;
        barInput.value = finalText;
        setListening(false);
        if (conf && conf < 0.55) {
          toast('没听太清，已把识别结果放进输入框，请确认后点「查询」');
          barInput.focus();
        } else {
          submit(finalText);
        }
      }
    };
    recog.onerror = (ev) => {
      setListening(false);
      toast(errText(ev.error));
      if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') barInput.focus();
    };
    recog.onend = () => setListening(false);
    try {
      recog.start();
    } catch (e) {
      setListening(false);
      toast('无法启动语音识别，请直接打字提问');
    }
  }

  function stopListen() {
    if (recog) { try { recog.stop(); } catch (e) { /* ignore */ } }
    setListening(false);
  }

  function toggleListen() {
    if (listening) stopListen();
    else startListen();
  }

  // ── 事件绑定 ──
  $('micBtn').addEventListener('click', toggleListen);
  $('bigMic').addEventListener('click', toggleListen);

  // 悬浮按钮：长按可持续说，轻点也能触发（不依赖长按）
  let pressTimer = null;
  const fab = $('voiceBtn');
  fab.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    pressTimer = setTimeout(() => { pressTimer = null; startListen(); }, 180);
  });
  fab.addEventListener('pointerup', () => {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; toggleListen(); }
  });
  fab.addEventListener('pointercancel', () => {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
  });

  $('askBarForm').addEventListener('submit', (e) => { e.preventDefault(); submit(barInput.value); });
  $('askForm').addEventListener('submit', (e) => { e.preventDefault(); submit(askInput.value); });
  $('closeDrawer').addEventListener('click', closeDrawer);

  window.Voice = { submit, open: openDrawer, close: closeDrawer, listen: toggleListen };

  if (!SR) {
    setListening(false, '当前浏览器不支持语音识别，可点输入框打字提问（功能不受影响）');
  }
})();
