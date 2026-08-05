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
  selectedTestsetId: null,
  activeRunId: null,
  // 事件驱动：在途条目快照（entry_id -> 条目），来自 /events 的 pending 事件
  pendingEntries: new Map(),
  // 事件驱动：已结束的测试集运行报告暂存（run_id -> 完整 run dict），
  // 供「查看报告」按需展示（不再自动弹窗）
  runReports: {},
  latestReportRunId: null,
  // 事件驱动：测试集运行中已反馈过逐会话结果的步骤索引（去重，防重复刷新）
  testsetReportedSteps: new Set(),
  // 页眉菜单「复制历史」的剪贴板：{conversations, sourceName, at}，null 表示未复制
  clipboard: null,
};
