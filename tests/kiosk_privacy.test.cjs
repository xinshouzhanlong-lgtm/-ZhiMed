/* node --test tests/kiosk_privacy.test.cjs
   使用可控时钟、浏览器事件和迟到的网络/语音回调验证患者端隐私边界。 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Clock {
  constructor() { this.now = 1000000; this.next = 1; this.timers = new Map(); }
  add(fn, delay, interval = false) {
    const id = this.next++;
    this.timers.set(id, { fn, at: this.now + delay, interval: interval ? delay : 0 });
    return id;
  }
  advance(ms) {
    const end = this.now + ms;
    while (true) {
      const due = [...this.timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      const [id, timer] = due;
      this.now = timer.at;
      if (timer.interval) timer.at += timer.interval;
      else this.timers.delete(id);
      timer.fn();
    }
    this.now = end;
  }
}

class Events {
  constructor() { this.events = new Map(); }
  addEventListener(name, fn) {
    if (!this.events.has(name)) this.events.set(name, []);
    this.events.get(name).push(fn);
  }
  emit(name, event = {}) {
    for (const fn of this.events.get(name) || []) fn({ preventDefault() {}, ...event });
  }
}

class Element extends Events {
  constructor() {
    super(); this.children = []; this._text = ''; this._html = ''; this.value = ''; this.hidden = false;
    this.classList = { toggle() {} }; this.parts = new Map();
  }
  set textContent(value) { this._text = String(value); this._html = ''; this.children = []; }
  get textContent() { return this._text + this._html + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this._html = value; this._text = ''; this.children = []; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; this._html = this._text = ''; }
  querySelector(name) {
    if (!this.parts.has(name)) this.parts.set(name, new Element());
    return this.parts.get(name);
  }
  focus() { this.focused = true; }
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness({ speech = false, speechStarts = true, microphone = false, previousSession = null, endFails = false } = {}) {
  const clock = new Clock();
  const html = fs.readFileSync(path.join(__dirname, '../web/kiosk/index.html'), 'utf8');
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const document = new Events();
  document.getElementById = (id) => { assert(elements.has(id), 'Missing HTML element: ' + id); return elements.get(id); };
  document.createElement = () => new Element();
  const window = new Events();
  let uuid = 0;
  window.crypto = { randomUUID() { return 'test-session-' + ++uuid; } };
  window.GuideKiosk = { deviceId: 'test-device' };
  const storage = new Map(previousSession ? [['guide_session_id', previousSession]] : []);
  const calls = [], recognitions = [], spoken = [];
  let responseFactory = () => Promise.resolve({ ok: true, json: async () => ({ text: '回答内容', cards: [], disclaimer: '就诊指引' }) });
  if (microphone) {
    window.SpeechRecognition = class {
      constructor() { recognitions.push(this); }
      start() { if (this.onstart) this.onstart(); }
      stop() { this.stopped = true; }
      abort() { this.aborted = true; if (this.onend) this.onend(); }
    };
  }
  if (speech) window.speechSynthesis = {
    cancelled: 0, cancel() { this.cancelled += 1; },
    speak(utterance) { spoken.push(utterance); if (speechStarts && utterance.onstart) utterance.onstart(); },
  };
  const context = vm.createContext({
    document, window, console, AbortController,
    Date: class extends Date { static now() { return clock.now; } },
    setTimeout: (fn, ms) => clock.add(fn, ms), clearTimeout: id => clock.timers.delete(id),
    setInterval: (fn, ms) => clock.add(fn, ms, true),
    sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value) },
    SpeechSynthesisUtterance: class { constructor(text) { this.text = text; } },
    fetch(url, options) {
      calls.push({ url, options, payload: JSON.parse(options.body) });
      if (url === '/api/voice/session/end') return endFails ? Promise.reject(new Error('offline')) : Promise.resolve({ ok: true });
      return responseFactory();
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/kiosk/voice.js'), 'utf8'), context);
  return {
    clock, window, document, recognitions, spoken, storage,
    el: id => document.getElementById(id),
    requests: () => calls.filter(call => call.url === '/api/voice/turn'),
    ended: () => calls.filter(call => call.url === '/api/voice/session/end'),
    respondWith(fn) { responseFactory = fn; },
  };
}

function resultEvent(text) {
  const result = [{ transcript: text, confidence: 0.95 }];
  result.isFinal = true;
  return { resultIndex: 0, results: [result] };
}

test('结束和关闭均清空屏幕、输入与会话，且下次使用新编号', async () => {
  const h = harness();
  await h.window.Voice.submit('患者的原始问题');
  const oldId = h.requests()[0].payload.session_id;
  h.el('barInput').value = '未发送的文字';
  h.el('askInput').value = '另一段文字';
  h.el('endSession').emit('click');
  assert.equal(h.el('drawer').hidden, true);
  assert.equal(h.el('drawerBody').children.length, 1);
  assert(!h.el('drawerBody').textContent.includes('患者的原始问题'));
  assert.equal(h.el('barInput').value, '');
  assert.equal(h.el('askInput').value, '');
  assert.equal(h.el('disclaimer').textContent, '');
  assert.equal(h.ended()[0].payload.session_id, oldId);
  await h.window.Voice.submit('下一位的问题');
  assert.notEqual(h.requests()[1].payload.session_id, oldId);
  h.el('closeDrawer').emit('click');
  assert.equal(h.el('drawer').hidden, true);
  assert(!h.el('drawerBody').textContent.includes('下一位的问题'));
});

test('闲置 50 秒提示、60 秒清理；继续咨询重新计时', async () => {
  const h = harness();
  await h.window.Voice.submit('问题');
  h.clock.advance(49750);
  assert.equal(h.el('privacyCountdown').hidden, true);
  h.clock.advance(250);
  assert.equal(h.el('privacyCountdown').hidden, false);
  assert(h.el('privacyCountdownText').textContent.includes('10 秒'));
  h.el('continueSession').emit('click');
  assert.equal(h.el('privacyCountdown').hidden, true);
  h.clock.advance(59000);
  assert.equal(h.el('drawer').hidden, false);
  h.clock.advance(1000);
  assert.equal(h.el('drawer').hidden, true);
  assert.equal(h.ended().length, 1);
});

test('点击和输入延长咨询，但无用户操作时仍会清理', async () => {
  const h = harness();
  await h.window.Voice.submit('问题');
  h.clock.advance(45000);
  h.document.emit('pointerdown');
  h.clock.advance(45000);
  h.el('askInput').value = '正在输入';
  h.el('askInput').emit('input');
  h.clock.advance(59000);
  assert.equal(h.el('drawer').hidden, false);
  h.clock.advance(1000);
  assert.equal(h.el('askInput').value, '');
  assert.equal(h.el('drawer').hidden, true);
});

test('未发送的首页输入也会清理，清理无需依赖结束接口可用', async () => {
  const h = harness({ endFails: true });
  h.el('barInput').value = '未发送的隐私内容';
  h.el('barInput').emit('input');
  h.clock.advance(60000);
  assert.equal(h.el('barInput').value, '');
  assert.equal(h.ended().length, 1);
  await Promise.resolve();
});

test('清理会中止旧请求；即使旧请求仍然返回也不显示或播报', async () => {
  const h = harness({ speech: true });
  const transport = deferred();
  h.respondWith(() => transport.promise);
  const pending = h.window.Voice.submit('上一位的问题');
  const first = h.requests()[0];
  h.window.Voice.close();
  assert.equal(first.options.signal.aborted, true);
  transport.resolve({ ok: true, json: async () => ({ text: '上一位的回答', cards: [] }) });
  await pending;
  assert(!h.el('drawerBody').textContent.includes('上一位'));
  assert.equal(h.spoken.length, 0);
  assert.equal(h.el('drawer').hidden, true);
});

test('清理发生在解析回答期间，迟到的 JSON 同样被丢弃', async () => {
  const h = harness();
  const body = deferred();
  h.respondWith(async () => ({ ok: true, json: () => body.promise }));
  const pending = h.window.Voice.submit('旧问题');
  await Promise.resolve();
  h.clock.advance(60000);
  body.resolve({ text: '迟到的旧回答', cards: [] });
  await pending;
  assert(!h.el('drawerBody').textContent.includes('旧'));
});

test('新的咨询不受旧请求失败影响', async () => {
  const h = harness();
  const old = deferred();
  h.respondWith(() => old.promise);
  const pending = h.window.Voice.submit('旧问题');
  h.window.Voice.close();
  h.respondWith(async () => ({ ok: true, json: async () => ({ text: '新回答', cards: [] }) }));
  await h.window.Voice.submit('新问题');
  old.reject(new Error('old network error'));
  await pending;
  assert(h.el('drawerBody').textContent.includes('新回答'));
  assert(!h.el('drawerBody').textContent.includes('网络异常'));
});

test('播报期间延后清理，结束播报后重新计算 60 秒', async () => {
  const h = harness({ speech: true });
  await h.window.Voice.submit('问题');
  h.clock.advance(90000);
  assert.equal(h.el('drawer').hidden, false);
  assert.equal(h.el('privacyCountdown').hidden, true);
  h.spoken[0].onend();
  h.clock.advance(60000);
  assert.equal(h.el('drawer').hidden, true);
  assert(h.window.speechSynthesis.cancelled > 0);
});

test('播报结束事件丢失时，不会无限保留内容', async () => {
  const h = harness({ speech: true });
  await h.window.Voice.submit('问题');
  h.clock.advance(180000);
  assert.equal(h.el('drawer').hidden, true);
});

test('浏览器未开始播报时仍在闲置 60 秒后清理', async () => {
  const h = harness({ speech: true, speechStarts: false });
  await h.window.Voice.submit('问题');
  assert.equal(h.spoken.length, 1);
  h.clock.advance(50000);
  assert.equal(h.el('privacyCountdown').hidden, false);
  h.clock.advance(10000);
  assert.equal(h.el('drawer').hidden, true);
  assert(!h.el('drawerBody').textContent.includes('回答内容'));
});

test('未开始播报的迟到错误也不能延长闲置期限', async () => {
  const h = harness({ speech: true, speechStarts: false });
  await h.window.Voice.submit('问题');
  h.clock.advance(50000);
  h.spoken[0].onerror();
  h.clock.advance(10000);
  assert.equal(h.el('drawer').hidden, true);
});

test('旧播报回调不能延长下一位患者的会话', async () => {
  const h = harness({ speech: true });
  await h.window.Voice.submit('上一位的问题');
  const lateFinished = h.spoken[0].onend;
  h.window.Voice.close();
  await h.window.Voice.submit('下一位的问题');
  h.spoken[1].onend();
  h.clock.advance(50000);
  lateFinished();
  h.clock.advance(10000);
  assert.equal(h.el('drawer').hidden, true);
});

test('结束咨询会停止识别、取消播报并忽略迟到的语音回调', async () => {
  const h = harness({ speech: true, microphone: true });
  await h.window.Voice.submit('问题');
  const audioDone = h.spoken[0].onend;
  h.window.Voice.listen();
  const recognition = h.recognitions[0];
  const lateResult = recognition.onresult;
  h.el('endSession').emit('click');
  assert.equal(recognition.aborted, true);
  lateResult(resultEvent('上一位患者的语音'));
  audioDone();
  assert.equal(h.requests().length, 1);
  assert.equal(h.el('barInput').value, '');
  assert.equal(h.el('drawer').hidden, true);
});

test('识别卡住时有超时保护，之后仍按闲置规则清理', () => {
  const h = harness({ microphone: true });
  h.window.Voice.listen();
  h.clock.advance(30000);
  assert.equal(h.recognitions[0].aborted, true);
  h.clock.advance(60000);
  assert.equal(h.el('drawer').hidden, true);
});

test('结束会取消尚未触发的长按录音定时器', () => {
  const h = harness({ microphone: true });
  h.el('voiceBtn').emit('pointerdown');
  h.window.Voice.close();
  h.clock.advance(180);
  assert.equal(h.recognitions.length, 0);
});

test('刷新页面使用新会话，页面离开时也清空', async () => {
  const h = harness({ previousSession: 'old-tab-session' });
  assert.notEqual(h.storage.get('guide_session_id'), 'old-tab-session');
  assert.equal(h.ended()[0].payload.session_id, 'old-tab-session');
  await h.window.Voice.submit('问题');
  h.window.emit('pagehide');
  assert.equal(h.el('drawer').hidden, true);
  assert(!h.el('drawerBody').textContent.includes('回答内容'));
});

test('后台定时器被限速后，返回页面立即核对绝对到期时间', async () => {
  const h = harness();
  await h.window.Voice.submit('问题');
  h.clock.now += 70000;
  h.document.emit('visibilitychange');
  assert.equal(h.el('drawer').hidden, true);
});
