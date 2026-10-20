"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const MAX_BYTES = 512 * 1024;
  const PAGE_SIZE = 100;
  const fields = [
    {name: "timestamp_s", label: "时间", units: ["s", "ms"]},
    {name: "cell_voltage_v", label: "电芯电压", units: ["V", "mV"]},
    {name: "current_a", label: "电流", units: ["A", "mA"]},
    {name: "temperature_c", label: "温度", units: ["degC", "K"]},
  ];
  const state = {csv: "", source: "", input: null, result: null, waveform: null, page: 0, busy: false, selectedEvent: null, fileRead: 0};
  let localModule = null, localController = null;
  const loadLocal = async () => localModule || (localModule = await import('/assets/local-built/entry.js'));

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function numeric(value) { return typeof value === "number" && Number.isFinite(value); }
  function format(value) {
    if (!numeric(value)) return value === null || value === undefined ? "—" : String(value);
    if (Math.abs(value) >= 1e6 || (value !== 0 && Math.abs(value) < 0.001)) return value.toExponential(3);
    return Number(value.toFixed(4)).toString();
  }

  function showError(message) {
    $("error-message").textContent = message;
    $("error-message").hidden = !message;
  }

  function setBusy(value, message) {
    state.busy = value;
    $("analyze-button").disabled = value;
    $("inspect-window").disabled = value;
    $("reset-window").disabled = value;
    $("csv-file").disabled = value;
    $("load-demo").disabled = value;
    $("mode").disabled = value;
    $("cancel-local").hidden = !(value && $("mode").value === "browser");
    if (message) $("run-status").textContent = message;
  }

  function readHeaders(text) {
    const headers = []; let cell = "", quoted = false;
    for (let i = 0; i < text.length; i += 1) {
      const char = text[i];
      if (char === '"') {
        if (quoted && text[i + 1] === '"') { cell += '"'; i += 1; }
        else quoted = !quoted;
      } else if (char === "," && !quoted) { headers.push(cell); cell = ""; }
      else if ((char === "\n" || char === "\r") && !quoted) { headers.push(cell); break; }
      else cell += char;
      if (i === text.length - 1) headers.push(cell);
    }
    if (quoted || !headers.length || new Set(headers).size !== headers.length || headers.some((header) => !header.trim() || header.length > 256)) {
      throw new Error("无法读取 CSV 列名；请检查表头和重复列。");
    }
    return headers;
  }

  function buildMapping(headers) {
    const container = $("column-mapping"); container.replaceChildren();
    for (const field of fields) {
      const group = element("div", undefined, "mapping-item");
      const label = element("label", `${field.label}列`);
      const select = element("select"); select.id = `map-${field.name}`;
      const empty = element("option", field.name === "timestamp_s" ? "请选择时间列" : "无此标准列 / 使用附加通道"); empty.value = ""; select.append(empty);
      for (const header of headers) { const option = element("option", header); option.value = header; select.append(option); }
      select.value = headers.includes(field.name) ? field.name : "";
      label.append(select);
      const unitLabel = element("label", "原始单位");
      const unit = element("select"); unit.id = `unit-${field.name}`;
      for (const name of field.units) { const option = element("option", name); option.value = name; unit.append(option); }
      unitLabel.append(unit); group.append(label, unitLabel); container.append(group);
    }
  }

  function clearResult() {
    state.input = null; state.result = null; state.waveform = null; state.page = 0; state.selectedEvent = null;
    $("results").hidden = true; $("event-details").hidden = true;
  }

  function setUpload(text, source) {
    const headers = readHeaders(text);
    state.csv = text; state.source = source;
    clearResult(); buildMapping(headers); showError("");
    $("file-status").textContent = `${source} · ${headers.length} 列 · ${(new TextEncoder().encode(text).length / 1024).toFixed(1)} KiB`;
    $("run-status").textContent = "日志已读取，请确认列映射与单位";
    $("csv-file").required = false;
  }

  $("csv-file").addEventListener("change", async () => {
    const marker = ++state.fileRead;
    const file = $("csv-file").files[0];
    state.csv = ""; clearResult();
    if (!file) { $("file-status").textContent = "尚未选择文件"; return; }
    try {
      if (file.size > MAX_BYTES) throw new Error("CSV 超过公开版 512 KiB 上限，请使用本地完整版本或减少数据。");
      const bytes = await file.arrayBuffer();
      const text = new TextDecoder("utf-8", {fatal: true}).decode(bytes).replace(/^\uFEFF/, "");
      if (marker !== state.fileRead) return;
      setUpload(text, file.name);
    } catch (error) {
      if (marker !== state.fileRead) return;
      showError(error instanceof TypeError ? "CSV 必须采用 UTF-8 编码。" : error.message);
      $("run-status").textContent = "日志未读取";
    }
  });

  $("load-demo").addEventListener("click", () => {
    state.fileRead += 1;
    const lines = ["timestamp_s,cell_voltage_v,current_a,temperature_c"];
    for (let time = 0; time <= 30; time += 1) {
      const voltage = time >= 25 && time <= 29 ? 2.8 : 3.7;
      const current = time >= 5 && time <= 10 ? 61 : 10;
      const temperature = time >= 17 && time <= 23 ? 66 : 25;
      lines.push(`${time},${voltage},${current},${temperature}`);
    }
    $("csv-file").value = "";
    setUpload(lines.join("\n") + "\n", "browser-demo.csv");
  });

  $("mode").addEventListener("change", () => {
    const enabled = $("mode").value === "agent";
    $("agent-settings").hidden = !enabled;
    $("free-settings").hidden = $("mode").value !== "browser";
    $("api-key").required = enabled; $("model").required = enabled;
    if (!enabled) $("api-key").value = "";
  });

  function jsonObject(id, label) {
    const text = $(id).value.trim();
    if (!text) return {};
    let value;
    try { value = JSON.parse(text); } catch (_error) { throw new Error(`${label} 必须为有效 JSON。`); }
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(`${label} 必须为 JSON 对象。`);
    return value;
  }

  function inputPayload() {
    if (!state.csv) throw new Error("请先选择并读取 CSV 日志。");
    const columnMap = Object.create(null); const units = Object.create(null);
    for (const field of fields) {
      const source = $(`map-${field.name}`).value;
      if (source) { columnMap[field.name] = source; units[field.name] = $(`unit-${field.name}`).value; }
    }
    Object.assign(columnMap, jsonObject("extra-map", "附加列映射"));
    Object.assign(units, jsonObject("extra-units", "附加单位"));
    const payload = {csv_text: state.csv, source_name: state.source, column_map: columnMap, units,
      current_convention: $("current-convention").value};
    const packId = $("pack-id").value.trim();
    if (packId) payload.pack_id = packId;
    const profile = jsonObject("profile", "规则/质量配置");
    if (Object.keys(profile).length) payload.profile = profile;
    return payload;
  }

  async function request(path, payload) {
    const response = await fetch(path, {method: "POST", credentials: "omit", cache: "no-store",
      headers: {"Content-Type": "application/json", "X-BMS-Request": "1"}, body: JSON.stringify(payload)});
    let result;
    try { result = await response.json(); } catch (_error) { throw new Error("服务返回无效响应，请稍后重试。"); }
    if (!response.ok) throw new Error(result?.error?.message || "请求未完成，请稍后重试。");
    return result;
  }

  $("analysis-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy) return;
    showError(""); let payload;
    try {
      payload = inputPayload();
      payload.mode = $("mode").value; payload.question = $("question").value;
      if (payload.mode === "agent") {
        payload.api_key = $("api-key").value.trim(); payload.model = $("model").value.trim();
        if (!payload.api_key || !payload.model) throw new Error("Agent 模式必须填写本次 Key 与模型 ID。");
        payload.max_turns = Number($("max-turns").value); payload.timeout_s = Number($("timeout").value);
        payload.max_output_tokens = Number($("max-output").value); payload.max_total_tokens = Number($("max-total").value);
      }
      // Snapshot only non-credential data for later offline window requests.
      state.input = inputPayload();
      $("api-key").value = "";
      setBusy(true, payload.mode === "agent" ? "模型分析中；本次调用可能计费…" : "离线规则分析中…");
      $("results").hidden = true;
      let result;
      if (payload.mode === "browser") {
        // No key, model endpoint, cookies or uploaded CSV are sent over the network in free mode.
        localController = new AbortController();
        $("run-status").textContent = "正在本地解析 CSV 与执行规则…";
        const local = await loadLocal();
        result = await local.analyzeFree({...state.input,question:payload.question},{
          consent:$("model-consent").checked,signal:localController.signal,
          onProgress:message=>{ $("run-status").textContent=message; }});
      } else {
        const pending = request("/api/analyze", payload);
        delete payload.api_key;
        result = await pending;
      }
      if (!result.report?.analysis || !result.waveform) throw new Error("分析结果缺少必要字段。");
      state.result = result; state.waveform = result.waveform; state.page = 0; state.selectedEvent = null;
      renderResult();
      $("run-status").textContent = result.report.status === "complete" ? "分析完成 · 结果可导出" :
        result.report.status === "fallback" ? "免费 AI 未完成 · 已保留本地规则结果 · 无付费 API 调用" : "模型未完成 · 已保留离线证据";
    } catch (error) {
      showError(error.message || "分析未完成，请稍后重试。");
      $("run-status").textContent = "本次分析未完成";
    } finally {
      if (payload) delete payload.api_key;
      $("api-key").value = "";
      localController = null;
      setBusy(false);
    }
  });
  $("cancel-local").addEventListener("click", () => { localController?.abort(); });
  $("model-consent").addEventListener("change", () => { if (!$("model-consent").checked) localController?.abort(); });

  function renderResult() {
    const report = state.result.report, summary = report.analysis.summary, conclusion = report.conclusion || {};
    $("metrics").replaceChildren();
    for (const [label, value] of [["采样数", summary.sample_count], ["保护事件", summary.fault_event_count], ["质量事件", summary.quality_event_count], ["采样时长（s）", summary.duration_s]]) {
      const card = element("div", undefined, "metric"); card.append(element("span", label, "metric-label"), element("strong", format(value))); $("metrics").append(card);
    }
    $("conclusion-summary").textContent = conclusion.summary || report.error || "模型分析未完成，以下保留确定性规则证据。";
    const coverage = conclusion.coverage || report.coverage;
    if (coverage && typeof coverage === "object") {
      const scan = coverage.deterministic_scan_complete ? "完成" : "未确认";
      const owner = report.mode === "offline" || report.local_ai?.model_used === false ? "规则程序" : "模型选择证据、程序核验";
      const complete = coverage.mode === "full" ? "完整取证" : "部分取证";
      $("coverage-note").textContent = `全量规则扫描：${scan}；逐项引用 ${format(coverage.cited_events)} / ${format(coverage.total_events)}；未引用 ${format(coverage.remaining_events)}；${owner}${complete}。`;
    } else {
      $("coverage-note").textContent = "以完整导出的规则事件为准；自然语言结论不等于物理根因验证。";
    }
    $("limitations").replaceChildren();
    for (const limitation of conclusion.limitations || []) $("limitations").append(element("li", limitation));
    const usage = report.usage || {};
    $("usage-note").textContent = report.mode === "browser" ?
      (report.local_ai?.model_used ? `免费本地 Qwen3-1.7B：付费 API 调用数 0。本地模型输入 ${format(usage.local_inference?.input_tokens)}、输出 ${format(usage.local_inference?.output_tokens)} token（设备计算，不是 API 账单）。` :
        `免费 AI 未运行完成：${report.local_ai?.reason || "请检查设备与模型下载"} 这里显示本地规则结果，付费 API 调用数 0。`) :
      report.mode === "offline" ? "本站规则模式：未调用模型，API 用量为零；CSV 已发送至本站后端。" : `本次 SDK 用量：请求 ${format(usage.requests)}，输入 ${format(usage.input_tokens)} token，输出 ${format(usage.output_tokens)} token。未知用量不代表未计费；未估算货币费用。`;
    $("window-start").value = summary.start_s; $("window-end").value = summary.end_s;
    $("event-details").hidden = true;
    $("results").hidden = false;
    buildChannels(); renderEvents(); renderWaveform();
  }

  function channels() {
    const samples = state.waveform?.samples || [];
    const descriptions = state.result?.report?.analysis?.summary?.channels || {};
    const result = [];
    for (const [field, label, defaultUnit] of [["cell_voltage_v", "标准电压", "V"], ["current_a", "电流", "A"], ["temperature_c", "标准温度", "degC"]]) {
      const mapName = field === "cell_voltage_v" ? "cell_voltages_v" : field === "temperature_c" ? "temperatures_c" : null;
      const hasMaps = mapName && samples.some((sample) => Object.keys(sample[mapName] || {}).length);
      const exists = Boolean(descriptions[field]) || samples.some((sample) => numeric(sample[field])) || (!hasMaps && samples.some((sample) => Object.hasOwn(sample, field)));
      if (exists) result.push({id: field, label, unit: descriptions[field]?.unit || defaultUnit, value: (sample) => sample[field]});
    }
    for (const [mapName, prefix, label, defaultUnit] of [["cell_voltages_v", "cell_voltage_v_", "电芯", "V"], ["temperatures_c", "temperature_c_", "温度", "degC"]]) {
      const ids = Array.from(new Set(samples.flatMap((sample) => Object.keys(sample[mapName] || {})))).sort();
      for (const id of ids) result.push({id: `${prefix}${id}`, label: `${label} ${id}`, unit: descriptions[`${prefix}${id}`]?.unit || defaultUnit, value: (sample) => sample[mapName]?.[id]});
    }
    for (const [mapName, field, label, unit] of [["cell_voltages_v", "cell_spread_v", "电芯压差", "V"], ["temperatures_c", "temperature_spread_c", "探头温差", "degC"]]) {
      const generic = mapName === "cell_voltages_v" ? "cell_voltage_v" : "temperature_c";
      const legacy = result.some((channel) => channel.id === generic);
      const count = new Set(samples.flatMap((sample) => Object.keys(sample[mapName] || {}))).size + (legacy ? 1 : 0);
      if (count >= 2) result.push({id: field, label, unit, value: (sample) => {
        const values = Object.values(sample[mapName] || {});
        if (legacy) values.push(sample[generic]);
        if (values.length < 2 || values.some((value) => !numeric(value))) return null;
        const spread = Math.max(...values) - Math.min(...values);
        return numeric(spread) ? spread : null;
      }});
    }
    return result;
  }

  function buildChannels() {
    const previous = $("signal-channel").value, select = $("signal-channel"); select.replaceChildren();
    for (const channel of channels()) { const option = element("option", `${channel.label} (${channel.unit})`); option.value = channel.id; select.append(option); }
    if (Array.from(select.options).some((option) => option.value === previous)) select.value = previous;
  }

  function activeChannel() { return channels().find((channel) => channel.id === $("signal-channel").value); }

  function renderWaveform() {
    const canvas = $("waveform"), context = canvas.getContext("2d");
    const width = Math.max(280, canvas.getBoundingClientRect().width), height = 270, ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const samples = state.waveform?.samples || [], channel = activeChannel();
    const values = channel ? samples.map((sample) => channel.value(sample)).filter(numeric) : [];
    context.font = '12px "Segoe UI", sans-serif'; context.fillStyle = "#627582";
    if (!values.length || !samples.length) {
      context.fillText("当前窗口没有可绘制的有效读数", 20, 38);
      $("waveform-note").textContent = `匹配 ${format(state.waveform?.matched_samples)} 条；当前通道没有可绘制的有效读数。`;
      renderSampleRows(channel); return;
    }
    const pad = {left: 76, right: 20, top: 22, bottom: 36};
    const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
    const minimum = Math.min(...values), maximum = Math.max(...values), divider = Math.max(Math.abs(minimum), Math.abs(maximum), 1);
    const lo = minimum / divider, hi = maximum / divider;
    const first = samples[0].timestamp_s, last = samples[samples.length - 1].timestamp_s;
    const x = (time) => pad.left + (last === first ? 0.5 : (time - first) / (last - first)) * plotWidth;
    const y = (value) => pad.top + (hi === lo ? 0.5 : 1 - (value / divider - lo) / (hi - lo)) * plotHeight;
    for (let i = 0; i <= 4; i += 1) {
      const py = pad.top + i * plotHeight / 4;
      context.strokeStyle = "#e3ebed"; context.beginPath(); context.moveTo(pad.left, py); context.lineTo(width - pad.right, py); context.stroke();
      context.fillStyle = "#627582"; context.textAlign = "right";
      context.fillText(format((hi - (hi - lo) * i / 4) * divider), pad.left - 9, py + 4);
    }
    context.textAlign = "left"; context.fillText(channel.unit, 13, 16);
    context.fillText(`${format(first)} s`, pad.left, height - 12); context.textAlign = "right"; context.fillText(`${format(last)} s`, width - pad.right, height - 12);
    if (state.selectedEvent) {
      const confirmed = state.selectedEvent.confirmed_s;
      if (confirmed >= first && confirmed <= last) { context.setLineDash([4, 4]); context.strokeStyle = "#b67b30"; context.beginPath(); context.moveTo(x(confirmed), pad.top); context.lineTo(x(confirmed), height - pad.bottom); context.stroke(); context.setLineDash([]); }
    }
    context.strokeStyle = "#146f69"; context.lineWidth = 1.8;
    context.setLineDash(state.waveform.downsampled ? [4, 3] : []);
    context.beginPath(); let connected = false;
    for (const sample of samples) { const value = channel.value(sample); if (!numeric(value)) { connected = false; continue; } if (connected) context.lineTo(x(sample.timestamp_s), y(value)); else context.moveTo(x(sample.timestamp_s), y(value)); connected = true; }
    context.stroke(); context.setLineDash([]); context.fillStyle = "#146f69";
    for (const sample of samples) { const value = channel.value(sample); if (numeric(value)) { context.beginPath(); context.arc(x(sample.timestamp_s), y(value), 2.7, 0, Math.PI * 2); context.fill(); } }
    const sampling = state.waveform.sampling || {};
    const fidelity = sampling.critical_points_complete === false ? `关键点未全部保留（缺失 ${format(sampling.critical_points_missing)}）；请缩小窗口。` : sampling.critical_points_complete === true ? "本窗口关键点已全部保留。" : "关键点保真状态未提供。";
    $("waveform-note").textContent = `匹配 ${format(state.waveform.matched_samples)} 条，展示 ${format(state.waveform.returned_samples)} 条${state.waveform.downsampled ? "（已抽样）" : ""}。${fidelity} 连线仅辅助阅读，不据此推断采样间连续性。`;
    renderSampleRows(channel);
  }

  function renderSampleRows(channel) {
    $("sample-value-heading").textContent = channel ? `${channel.label} (${channel.unit})` : "读数";
    $("sample-rows").replaceChildren();
    for (const sample of state.waveform?.samples || []) { const row = element("tr"); for (const value of [sample.timestamp_s, channel?.value(sample), sample.row_number]) row.append(element("td", format(value))); $("sample-rows").append(row); }
  }

  function renderEvents() {
    const events = state.result.report.analysis.events || [], count = Math.max(1, Math.ceil(events.length / PAGE_SIZE));
    state.page = Math.min(state.page, count - 1); $("event-rows").replaceChildren();
    for (const event of events.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE)) {
      const row = element("tr");
      for (const value of [event.evidence_id, `${event.code} / ${event.field}`, `${format(event.start_s)} / ${format(event.confirmed_s)} / ${format(event.recovered_s)}`, event.status, format(event.peak_value)]) row.append(element("td", value));
      const cell = element("td"), button = element("button", "查看", "secondary"); button.type = "button"; button.setAttribute("aria-label", `查看证据 ${event.evidence_id}`); button.addEventListener("click", () => inspectEvent(event)); cell.append(button); row.append(cell); $("event-rows").append(row);
    }
    if (!events.length) { const row = element("tr"), cell = element("td", "当前配置未检出事件，不代表电池安全。"); cell.colSpan = 6; row.append(cell); $("event-rows").append(row); }
    $("events-total").textContent = `${events.length} 个事件`;
    $("page-status").textContent = `第 ${state.page + 1} / ${count} 页`;
    $("previous-page").disabled = state.page === 0; $("next-page").disabled = state.page >= count - 1;
  }

  async function inspectRange(start, end, event = null) {
    if (state.busy || !state.input || !numeric(start) || !numeric(end) || start > end) { if (!state.busy) showError("请输入有限、递增的窗口时间。"); return; }
    showError(""); setBusy(true, "读取离线证据窗口…");
    try {
      const payload = {...state.input, start_s: start, end_s: end, max_samples: 50};
      state.waveform = state.result.report.mode === "browser" ? await (await loadLocal()).inspectLocally(payload) : await request("/api/window", payload);
      $("window-start").value = start; $("window-end").value = end; buildChannels();
      if (event) {
        const names = channels().map((channel) => channel.id);
        const candidates = event.channel_id ? [`${event.field}_${event.channel_id}`, event.field] : [event.field];
        const selected = candidates.find((name) => names.includes(name));
        if (selected) $("signal-channel").value = selected;
      }
      renderWaveform();
      $("run-status").textContent = "证据窗口已更新 · 未调用模型";
    } catch (error) { showError(error.message || "窗口读取失败。"); }
    finally { setBusy(false); }
  }

  function inspectEvent(event) {
    if (state.busy) return;
    state.selectedEvent = event;
    $("event-details").textContent = JSON.stringify(event, null, 2); $("event-details").hidden = false;
    const summary = state.result.report.analysis.summary;
    const start = Math.max(summary.start_s, event.start_s - 2), end = Math.min(summary.end_s, (event.recovered_s ?? event.confirmed_s) + 2);
    inspectRange(start, end, event);
  }

  $("inspect-window").addEventListener("click", () => { state.selectedEvent = null; inspectRange(Number($("window-start").value), Number($("window-end").value)); });
  $("reset-window").addEventListener("click", () => { const summary = state.result.report.analysis.summary; state.selectedEvent = null; inspectRange(summary.start_s, summary.end_s); });
  $("signal-channel").addEventListener("change", renderWaveform);
  $("previous-page").addEventListener("click", () => { state.page -= 1; renderEvents(); });
  $("next-page").addEventListener("click", () => { state.page += 1; renderEvents(); });
  window.addEventListener("resize", () => { if (state.waveform) renderWaveform(); });

  function download(text, extension, mime) {
    const name = (state.result.report.source_name || "bms-log").replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80);
    const url = URL.createObjectURL(new Blob([text], {type: mime}));
    const link = element("a"); link.href = url; link.download = `${name}-report.${extension}`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("export-json").addEventListener("click", () => download(JSON.stringify(state.result.report, null, 2), "json", "application/json;charset=utf-8"));
  $("export-markdown").addEventListener("click", () => {
    if (typeof state.result.report_markdown !== "string") { showError("本次结果缺少 Markdown 导出。"); return; }
    download(state.result.report_markdown, "md", "text/markdown;charset=utf-8");
  });

  // Only the inference engine may cache public model artifacts; never CSV, questions or keys.
  buildMapping([]);
})();
