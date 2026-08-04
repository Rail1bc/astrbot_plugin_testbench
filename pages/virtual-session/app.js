// 虚拟会话测试平台 - 页面脚本
// 通过 window.AstrBotPluginPage bridge 与插件后端通信。
const bridge = window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

let providers = [];
let confs = [];
let sessions = [];

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusText(status) {
  switch (status) {
    case "ok":
      return "成功";
    case "no_reply":
      return "无回复";
    case "timeout":
      return "超时";
    case "error":
      return "错误";
    default:
      return status;
  }
}

// ---------- 会话管理 ----------

async function refreshSessions() {
  sessions = await bridge.apiGet("sessions");
  renderSessions();
}

function renderSessions() {
  const body = $("sessions-body");
  body.innerHTML = "";
  if (!sessions.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">暂无虚拟会话，请先在左侧创建</td></tr>';
    return;
  }
  for (const s of sessions) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><input type="checkbox" class="session-check" value="${escapeHtml(s.id)}" /></td>` +
      `<td><code>${escapeHtml(s.id)}</code></td>` +
      `<td>${escapeHtml(s.name)}</td>` +
      `<td>${escapeHtml(s.platform_id)}</td>` +
      `<td>${escapeHtml(s.sender_id)} (${escapeHtml(s.sender_name)})</td>` +
      `<td>${new Date((s.created_at || 0) * 1000).toLocaleString()}</td>`;
    body.appendChild(tr);
  }
}

function selectedSessionIds() {
  return [...document.querySelectorAll(".session-check:checked")].map((el) => el.value);
}

async function loadPlatforms() {
  let platforms = [];
  try {
    platforms = await bridge.apiGet("platforms");
  } catch (err) {
    console.warn("加载平台列表失败:", err);
  }
  const sel = $("create-platform");
  sel.innerHTML =
    '<option value="virtual_test">virtual_test（默认，隔离）</option>' +
    platforms
      .map(
        (p) =>
          `<option value="${escapeHtml(p.id)}">${escapeHtml(p.id)}（${escapeHtml(p.display_name || p.name)}）</option>`,
      )
      .join("");
}

$("btn-create").addEventListener("click", async () => {
  const count = parseInt($("create-count").value, 10);
  if (!Number.isInteger(count) || count < 1) {
    alert("数量必须是大于 0 的整数");
    return;
  }
  const platformId = $("create-platform").value;
  await bridge.apiPost("sessions", {
    count,
    platform_id: platformId && platformId !== "virtual_test" ? platformId : undefined,
    sender_id: $("create-sender-id").value || undefined,
    sender_name: $("create-sender-name").value || undefined,
    name_prefix: $("create-name-prefix").value || undefined,
  });
  await refreshSessions();
});

$("btn-refresh").addEventListener("click", refreshSessions);

$("btn-select-all").addEventListener("click", () => {
  const checks = document.querySelectorAll(".session-check");
  const allChecked = [...checks].every((el) => el.checked);
  checks.forEach((el) => {
    el.checked = !allChecked;
  });
});

$("btn-delete").addEventListener("click", async () => {
  const ids = selectedSessionIds();
  if (!ids.length) {
    alert("请先勾选要删除的会话");
    return;
  }
  if (!confirm(`确定删除选中的 ${ids.length} 个会话吗？`)) return;
  await bridge.apiPost("sessions/delete", { ids });
  await refreshSessions();
});

$("btn-reset").addEventListener("click", async () => {
  const ids = selectedSessionIds();
  if (!ids.length) {
    alert("请先勾选要重置历史的会话");
    return;
  }
  if (!confirm(`确定重置选中的 ${ids.length} 个会话的对话历史吗？`)) return;
  const resp = await bridge.apiPost("reset", { ids });
  alert(`已重置 ${resp.reset} 个会话的对话历史`);
});

// ---------- 选项加载 ----------

async function loadOptions() {
  try {
    providers = await bridge.apiGet("providers");
  } catch (err) {
    console.warn("加载 Provider 列表失败:", err);
    providers = [];
  }
  try {
    confs = await bridge.apiGet("confs");
  } catch (err) {
    console.warn("加载配置档案失败:", err);
    confs = [];
  }
  renderProviderSelect();
  renderConfSelect();
}

function renderProviderSelect() {
  const sel = $("test-provider");
  sel.innerHTML =
    "<option value=''>默认 Provider</option>" +
    providers
      .map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} (${escapeHtml(p.type)})</option>`)
      .join("");
  sel.addEventListener("change", onProviderChange);
  onProviderChange();
}

function onProviderChange() {
  const pid = $("test-provider").value;
  const prov = providers.find((p) => p.id === pid);
  const models = prov && Array.isArray(prov.models) ? prov.models : [];
  const modelSel = $("test-model");
  modelSel.innerHTML =
    "<option value=''>默认模型</option>" +
    models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  if (prov && prov.current_model && models.includes(prov.current_model)) {
    modelSel.value = prov.current_model;
  }
}

function renderConfSelect() {
  const sel = $("test-conf");
  sel.innerHTML =
    "<option value=''>默认</option>" +
    confs
      .filter((c) => c.id !== "default")
      .map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)}</option>`)
      .join("");
}

// ---------- 运行测试 ----------

$("btn-run").addEventListener("click", async () => {
  const ids = selectedSessionIds();
  const text = $("test-text").value;
  if (!ids.length) {
    alert("请先在会话列表中勾选要测试的会话");
    return;
  }
  if (!text || !text.trim()) {
    alert("请输入测试消息");
    return;
  }
  const btn = $("btn-run");
  btn.disabled = true;
  btn.textContent = "运行中…";
  try {
    const result = await bridge.apiPost("test/run", {
      sessions: ids,
      text,
      provider_id: $("test-provider").value || undefined,
      model: $("test-model").value || undefined,
      conf_id: $("test-conf").value || undefined,
      timeout: parseFloat($("test-timeout").value) || 120,
      batch_size: parseInt($("test-batch-size").value, 10) || 10,
      batch_interval: parseFloat($("test-batch-interval").value) || 0,
    });
    renderResult(result);
  } catch (err) {
    alert("测试失败: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "运行测试";
  }
});

function renderResult(result) {
  $("result-card").hidden = false;
  const s = result.stats;
  $("result-summary").innerHTML =
    `<div class="stat"><span class="num">${result.total}</span>总数</div>` +
    `<div class="stat ok"><span class="num">${result.ok}</span>成功</div>` +
    `<div class="stat warn"><span class="num">${result.no_reply}</span>无回复</div>` +
    `<div class="stat warn"><span class="num">${result.timeout}</span>超时</div>` +
    `<div class="stat"><span class="num">${s.min} ~ ${s.max}s</span>耗时范围</div>` +
    `<div class="stat"><span class="num">avg ${s.avg}s</span>平均</div>` +
    `<div class="stat"><span class="num">p50 ${s.p50}s</span></div>` +
    `<div class="stat"><span class="num">p95 ${s.p95}s</span></div>`;

  const body = $("results-body");
  body.innerHTML = "";
  for (const r of result.results) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><code>${escapeHtml(r.session_id)}</code></td>` +
      `<td><span class="badge ${r.status}">${statusText(r.status)}</span></td>` +
      `<td>${r.duration}</td>` +
      `<td class="reply">${escapeHtml(r.reply || "—")}</td>` +
      `<td class="err">${escapeHtml(r.error || "—")}</td>`;
    body.appendChild(tr);
  }
}

// ---------- 初始化 ----------

await bridge.ready();
await Promise.all([loadPlatforms(), loadOptions(), refreshSessions()]);
