// state.js — 全部模块共享的可变状态
// ES module 顶层绑定无法跨模块共享可变值（模块间相互 import 的 const 是只读
// 绑定），因此把全部共享状态收进一个 state 对象，列表/面板/发送等模块都从
// state 读写。可变状态集中在叶子模块，保持模块间依赖单向。
export const state = {
  groups: [],
  platforms: [],
  confs: [],
  openIds: [],
  pinnedIds: [],
  panelEls: new Map(),
  historyCache: new Map(),
  expandedGroups: new Set(),
  expandedSessions: new Set(),
  testsets: [],
  expandedTestsets: new Set(),
  activeRunId: null,
};
