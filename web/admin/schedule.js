/* Excel 排班：模板下载 → 校验预览 → 确认导入，以及按日期查看生效排班。 */
(function () {
  const get = (id) => document.getElementById(id);
  let pending = null;
  let busy = false;

  const now = new Date();
  get('scheduleDate').value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;

  function setBusy(value) {
    busy = value;
    ['previewScheduleBtn', 'commitScheduleBtn', 'cancelScheduleImport', 'scheduleFile'].forEach((id) => { get(id).disabled = value; });
  }

  function resetPreview() {
    pending = null;
    get('schedulePreview').hidden = true;
    get('scheduleErrors').hidden = true;
    get('scheduleErrors').replaceChildren();
    get('schedulePreviewRows').replaceChildren();
  }

  function showRows(target, rows, preview) {
    target.replaceChildren();
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      const values = [r.date, r.doctor_name, r.department_name, r.period, r.room || '—', r.status === 'on_duty' ? '出诊' : '停诊'];
      if (preview) { values.unshift(r.row_number); values.push(r.action); }
      else values.push(r.source === 'manual' ? 'Excel 导入' : '医生数据源');
      values.forEach((value) => {
        const td = document.createElement('td');
        td.textContent = value;
        tr.appendChild(td);
      });
      target.appendChild(tr);
    });
  }

  async function load() {
    const day = get('scheduleDate').value;
    if (!day) return;
    get('scheduleListMsg').textContent = '正在加载…';
    try {
      const data = await api('/api/admin/schedules?date=' + encodeURIComponent(day));
      if (get('scheduleDate').value !== day) return;
      showRows(get('scheduleRows'), data.rows || [], false);
      get('scheduleListMsg').textContent = data.rows.length ? `共 ${data.rows.length} 条排班，包含停诊记录。` : '该日期暂无排班。';
    } catch (err) {
      if (get('scheduleDate').value !== day) return;
      get('scheduleRows').replaceChildren();
      get('scheduleListMsg').textContent = err.message;
    }
  }

  get('downloadScheduleTemplate').onclick = async () => {
    const button = get('downloadScheduleTemplate');
    button.disabled = true;
    try {
      const response = await fetch('/api/admin/schedules/template', { headers: authHeaders() });
      if (!response.ok) throw new Error('模板下载失败，请确认登录状态后重试。');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = '医生排班导入模板.xlsx';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      get('scheduleImportMsg').textContent = err.message;
    } finally { button.disabled = false; }
  };

  get('scheduleFile').onchange = () => { resetPreview(); get('scheduleImportMsg').textContent = ''; };
  get('cancelScheduleImport').onclick = () => { resetPreview(); get('scheduleImportMsg').textContent = '已取消，本次排班未保存。'; };

  get('previewScheduleBtn').onclick = async () => {
    if (busy) return;
    resetPreview();
    const file = get('scheduleFile').files[0];
    if (!file) { get('scheduleImportMsg').textContent = '请先选择填写好的 Excel 文件。'; return; }
    if (!file.name.toLowerCase().endsWith('.xlsx') || file.size > 5 * 1024 * 1024) {
      get('scheduleImportMsg').textContent = '请使用不超过 5MB 的 .xlsx 文件。'; return;
    }
    const body = new FormData();
    body.append('file', file);
    setBusy(true);
    get('scheduleImportMsg').textContent = '正在校验文件…';
    try {
      const data = await api('/api/admin/schedules/preview', { method: 'POST', body });
      pending = data;
      showRows(get('schedulePreviewRows'), data.rows, true);
      get('schedulePreviewSummary').textContent = `${data.count} 条排班，涉及 ${data.doctor_count} 位医生。请核对后确认，预览有效期 15 分钟。`;
      get('schedulePreview').hidden = false;
      get('scheduleImportMsg').textContent = '校验通过，尚未保存。';
    } catch (err) {
      get('scheduleImportMsg').textContent = err.message;
      const errors = (err.detail && err.detail.errors) || [];
      errors.forEach((error) => {
        const li = document.createElement('li');
        li.textContent = `第 ${error.row} 行 · ${error.field}：${error.message}`;
        get('scheduleErrors').appendChild(li);
      });
      get('scheduleErrors').hidden = errors.length === 0;
    } finally { setBusy(false); }
  };

  get('commitScheduleBtn').onclick = async () => {
    if (busy || !pending) return;
    setBusy(true);
    const firstDay = pending.dates[0];
    try {
      const result = await api('/api/admin/schedules/commit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ import_id: pending.import_id }),
      });
      resetPreview();
      get('scheduleFile').value = '';
      get('scheduleDate').value = firstDay;
      get('scheduleImportMsg').textContent = `已导入 ${result.count} 条排班，患者端查询已生效。`;
      await load();
    } catch (err) {
      get('scheduleImportMsg').textContent = err.message;
      if (err.status === 409) resetPreview();
    } finally { setBusy(false); }
  };

  get('scheduleDate').onchange = load;
  get('refreshScheduleBtn').onclick = load;
  window.ScheduleAdmin = { load };
})();
