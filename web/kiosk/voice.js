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
  const WELCOME_TEXT = '可以这样问：「心内科在几楼」「今天有哪些科室出诊」「张明医生什么时候坐诊」';
  const SESSION_KEY = 'guide_session_id';
  const IDLE_MS = 60_000;
  const WARNING_MS = 10_000;

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recog = null;
  let listening = false;
  let recognitionTimer = null;
  let speaking = false;
  let utterance = null;
  let speechTimer = null;
  let active = false;
  let generation = 0;
  let idleDeadline = 0;
  const requests = new Set();
  let previousSession = null;
  try { previousSession = sessionStorage.getItem(SESSION_KEY); } catch (e) { /* 存储禁用时仍可清屏 */ }
  let sessionId = newSessionId();

  function newSessionId() {
    const id = 's-' + (window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID() : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
    try { sessionStorage.setItem(SESSION_KEY, id); } catch (e) { /* 会话仍在内存中隔离 */ }
    return id;
  }

  function notifyEnd(id) {
    if (!id) return;
    // 清屏不等待网络；即使通知失败，新会话也不会再次使用旧编号。
    fetch('/api/voice/session/end', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
      body: JSON.stringify({ session_id: id, device_id: window.GuideKiosk.deviceId }),
    }).catch(() => {});
  }

  function touch(activate = false) {
    if (activate) active = true;
    if (!active) return;
    idleDeadline = Date.now() + IDLE_MS;
    $('privacyCountdown').hidden = true;
  }

  function checkIdle() {
    if (!active) return;
    if (listening || speaking) {
      idleDeadline = Date.now() + IDLE_MS;
      $('privacyCountdown').hidden = true;
      return;
    }
    const remaining = idleDeadline - Date.now();
    if (remaining <= 0) {
      endSession();
    } else if (remaining <= WARNING_MS) {
      $('privacyCountdownText').textContent = `为保护隐私，${Math.ceil(remaining / 1000)} 秒后清除本次屏幕内容。`;
      $('privacyCountdown').hidden = false;
    }
  }

  function stopSpeech() {
    clearTimeout(speechTimer);
    if (utterance) utterance.onstart = utterance.onend = utterance.onerror = null;
    utterance = null;
    speaking = false;
    if (window.speechSynthesis) {
      try { window.speechSynthesis.cancel(); } catch (e) { /* 停止失败也不得恢复旧会话 */ }
    }
  }

  function clearScreen() {
    const welcome = document.createElement('div');
    welcome.className = 'hint';
    welcome.textContent = WELCOME_TEXT;
    body.replaceChildren(welcome);
    body.scrollTop = 0;
    barInput.value = '';
    askInput.value = '';
    $('disclaimer').textContent = '';
    $('privacyCountdown').hidden = true;
    $('privacyCountdownText').textContent = '';
  }

  function endSession(showNotice = true) {
    const oldId = sessionId;
    const hadConsultation = active;
    generation += 1;
    active = false;
    idleDeadline = 0;
    requests.forEach((controller) => controller.abort());
    requests.clear();
    clearTimeout(pressTimer);
    pressTimer = null;
    stopListen(true);
    stopSpeech();
    clearScreen();
    drawer.hidden = true;
    clearTimeout(toastTimer);
    $('toast').hidden = true;
    sessionId = newSessionId();
    if (hadConsultation) notifyEnd(oldId);
    if (showNotice) toast('本次咨询已结束，屏幕内容已清除。');
  }

  // ── 小工具 ──
  let toastTimer = null;
  function toast(msg, ms = 2600) {
    const t = $('toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function openDrawer() { checkIdle(); drawer.hidden = false; touch(true); }
  function closeDrawer() { endSession(); }
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
    stopSpeech();
    if (!window.speechSynthesis || !text) return;
    const currentGeneration = generation;
    try {
      const u = new SpeechSynthesisUtterance(text);
      utterance = u;
      u.lang = 'zh-CN';
      u.rate = 1.0;
      // 只有浏览器实际开始播报才延后清理。排队/被禁止的播报不能暂停倒计时。
      u.onstart = () => {
        if (generation !== currentGeneration || utterance !== u || !active) return;
        speaking = true;
        touch();
      };
      const finished = () => {
        if (generation !== currentGeneration || utterance !== u) return;
        clearTimeout(speechTimer);
        utterance = null;
        const wasSpeaking = speaking;
        speaking = false;
        if (wasSpeaking) touch();
      };
      u.onend = u.onerror = finished;
      // 浏览器漏报结束事件时也不能无限期保留屏幕内容。
      speechTimer = setTimeout(() => {
        if (generation !== currentGeneration || utterance !== u) return;
        const wasSpeaking = speaking;
        stopSpeech();
        if (wasSpeaking) touch();
      }, 120_000);
      window.speechSynthesis.speak(u);
    } catch (e) { stopSpeech(); touch(); }
  }

  // ── 提问 ──
  async function submit(text) {
    const clean = (text || '').trim();
    if (!clean) return;
    openDrawer();
    stopSpeech();
    const currentGeneration = generation;
    const currentSession = sessionId;
    const controller = new AbortController();
    requests.add(controller);
    bubble('user', escapeHtml(clean));
    barInput.value = '';
    askInput.value = '';
    try {
      const resp = await fetch('/api/voice/turn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({ session_id: currentSession, text: clean, device_id: window.GuideKiosk.deviceId }),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      if (generation !== currentGeneration || !active) return;
      touch();
      renderAnswer(data);
    } catch (e) {
      if (generation !== currentGeneration || !active || e.name === 'AbortError') return;
      bubble('bot', '网络异常，请稍后再试或咨询导医台。');
      touch();
    } finally {
      requests.delete(controller);
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
    openDrawer();
    stopSpeech();
    stopListen(true);
    const currentGeneration = generation;
    const recognition = new SR();
    recog = recognition;
    const isCurrent = () => generation === currentGeneration && recog === recognition;
    recognition.lang = 'zh-CN';
    recognition.continuous = false;     // 说完自动结束，不需要长按
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => { if (isCurrent()) setListening(true); };
    recognition.onresult = (ev) => {
      if (!isCurrent()) return;
      touch();
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
    recognition.onerror = (ev) => {
      if (!isCurrent()) return;
      clearTimeout(recognitionTimer);
      setListening(false);
      touch();
      toast(errText(ev.error));
      if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') barInput.focus();
    };
    recognition.onend = () => {
      if (!isCurrent()) return;
      clearTimeout(recognitionTimer);
      recog = null;
      setListening(false);
      touch();
    };
    try {
      setListening(true);
      recognitionTimer = setTimeout(() => {
        if (!isCurrent()) return;
        stopListen(true);
        touch();
        toast('本次语音识别已结束，可重新点击语音或打字提问。');
      }, 30_000);
      recognition.start();
    } catch (e) {
      stopListen(true);
      touch();
      toast('无法启动语音识别，请直接打字提问');
    }
  }

  function stopListen(discard = false) {
    if (recog) {
      const recognition = recog;
      if (discard) {
        recog = null;
        recognition.onstart = recognition.onresult = recognition.onerror = recognition.onend = null;
        clearTimeout(recognitionTimer);
      }
      try { if (discard) recognition.abort(); else recognition.stop(); } catch (e) { /* ignore */ }
    }
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
  $('endSession').addEventListener('click', () => endSession());
  $('endSessionNow').addEventListener('click', () => endSession());
  $('continueSession').addEventListener('click', () => { checkIdle(); if (active) touch(); });
  [barInput, askInput].forEach((input) => input.addEventListener('input', () => {
    checkIdle();
    if (input.value.trim()) touch(true);
    else touch();
  }));
  ['pointerdown', 'keydown', 'wheel', 'touchmove'].forEach((name) => {
    document.addEventListener(name, () => { checkIdle(); touch(); }, { passive: true });
  });
  window.addEventListener('focus', checkIdle);
  document.addEventListener('visibilitychange', checkIdle);
  window.addEventListener('pagehide', () => endSession(false));
  window.addEventListener('pageshow', checkIdle);
  setInterval(checkIdle, 250);

  window.Voice = { submit, open: openDrawer, close: closeDrawer, listen: toggleListen };

  // 刷新/恢复页面也从空白新会话开始，不能只清画面却继承旧患者上下文。
  clearScreen();
  if (previousSession) notifyEnd(previousSession);

  if (!SR) {
    setListening(false, '当前浏览器不支持语音识别，可点输入框打字提问（功能不受影响）');
  }
})();
