// api.js — 插件后端 bridge 调用的统一封装
// 页面通过 window.AstrBotPluginPage 与插件后端通信，这里集中管理全部接口。

const bridge = window.AstrBotPluginPage;

export function ready() {
  return bridge.ready();
}

export async function listPlatforms() {
  return bridge.apiGet("platforms");
}

export async function listConfs() {
  return bridge.apiGet("confs");
}

export async function listGroups() {
  return bridge.apiGet("groups");
}

export async function createGroup(payload) {
  return bridge.apiPost("groups", payload);
}

export async function deleteGroups(ids) {
  return bridge.apiPost("groups/delete", { ids });
}

export async function addGroupSessions(groupId, count) {
  return bridge.apiPost(`groups/${encodeURIComponent(groupId)}/sessions`, { count });
}

export async function updateGroup(payload) {
  return bridge.apiPost(`groups/${encodeURIComponent(payload.id)}/update`, payload);
}

export async function updateSession(payload) {
  return bridge.apiPost("sessions/update", payload);
}

export async function deleteSessions(ids) {
  return bridge.apiPost("sessions/delete", { ids });
}

export async function cloneSession(sessionId, count) {
  return bridge.apiPost("sessions/clone", { session_id: sessionId, count });
}

export async function deriveSession(sessionId, count, name) {
  return bridge.apiPost("sessions/derive", { session_id: sessionId, count, name });
}

export async function getHistory(id) {
  return bridge.apiGet(`sessions/${encodeURIComponent(id)}/history`);
}

export async function resetSessions(ids) {
  return bridge.apiPost("reset", { ids });
}

export async function runTest(payload) {
  return bridge.apiPost("test/run", payload);
}

export async function runStatus(testId) {
  // 查询串必须走第二个参数（params）：父窗口会拒绝端点字符串中的 `?`
  return bridge.apiGet("test/run/status", { test_id: testId });
}

export async function getPending() {
  return bridge.apiGet("sessions/pending");
}

export async function saveHistory(payload) {
  return bridge.apiPost("sessions/history/save", payload);
}

export async function regenerateHistory(payload) {
  return bridge.apiPost("sessions/history/regenerate", payload);
}

export async function listTestsets() {
  return bridge.apiGet("testsets");
}

export async function createTestset(payload) {
  return bridge.apiPost("testsets", payload);
}

export async function updateTestset(payload) {
  return bridge.apiPost(`testsets/${encodeURIComponent(payload.id)}/update`, payload);
}

export async function deleteTestsets(ids) {
  return bridge.apiPost("testsets/delete", { ids });
}

export async function runTestset(payload) {
  return bridge.apiPost("testsets/run", payload);
}

export async function runTestsetStatus(runId) {
  // 查询串走第二参数（params），见 runStatus
  return bridge.apiGet("testsets/run/status", { run_id: runId });
}

export async function abortTestsetRun(runId) {
  return bridge.apiPost("testsets/run/abort", { run_id: runId });
}

export async function listTestsetRuns() {
  return bridge.apiGet("testsets/runs");
}

export async function listIdentities() {
  return bridge.apiGet("identities");
}

export async function createIdentity(payload) {
  return bridge.apiPost("identities", payload);
}

export async function updateIdentity(identityId, payload) {
  return bridge.apiPost(`identities/${encodeURIComponent(identityId)}/update`, payload);
}

export async function deleteIdentities(ids) {
  return bridge.apiPost("identities/delete", { ids });
}

export async function listChatGroups() {
  return bridge.apiGet("chat-groups");
}

export async function createChatGroup(payload) {
  return bridge.apiPost("chat-groups", payload);
}

export async function updateChatGroup(groupId, payload) {
  return bridge.apiPost(`chat-groups/${encodeURIComponent(groupId)}/update`, payload);
}

export async function deleteChatGroups(ids) {
  return bridge.apiPost("chat-groups/delete", { ids });
}

export async function getStream(id) {
  return bridge.apiGet(`sessions/${encodeURIComponent(id)}/stream`);
}

export async function clearStream(ids) {
  return bridge.apiPost("sessions/stream/clear", { ids });
}

// ---------- SSE 事件流 ----------

// 单一全局订阅：重复调用返回同一订阅；断线时父窗口报告 onError，
// 本模块把订阅置空并通知页面（由页面延迟重连 + 快照对账兜底丢失的事件）。
let eventSub = null;

export async function subscribeEvents(onEvent, onError) {
  if (eventSub) return eventSub;
  eventSub = await bridge.subscribeSSE("events", {
    onMessage: ({ parsed }) => {
      if (parsed && parsed.type) onEvent(parsed);
    },
    onError: () => {
      eventSub = null;
      if (onError) onError();
    },
  });
  return eventSub;
}
