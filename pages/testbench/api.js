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
