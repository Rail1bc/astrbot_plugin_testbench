// utils.js — 纯工具函数与最终配置解析（唯一依赖 state）
// effectiveView 是后端 group_store.effective() 的客户端镜像，用于渲染时同步
// 展示最终配置（会话覆盖 → 组配置 → 默认值）；曾漏掉 sender 字段导致会话
// 展开配置里发送者 ID / 昵称恒显示「—」。
import { state } from "./state.js";

// 设置表单字段：label.settings-field 包裹「字段名 + 控件」（弹窗表单的通用行结构）
export function field(label, input) {
  const l = document.createElement("label");
  l.className = "settings-field";
  const span = document.createElement("span");
  span.textContent = label;
  l.appendChild(span);
  l.appendChild(input);
  return l;
}

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function statusText(status) {
  switch (status) {
    case "ok":
      return "成功";
    case "no_reply":
      return "无回复";
    case "error":
      return "错误";
    default:
      return status;
  }
}

export function confName(id) {
  if (!id) return "";
  const c = state.confs.find((x) => x.id === id);
  return c ? c.name : id;
}

export function platformName(id) {
  const p = state.platforms.find((x) => x.id === id);
  return p ? p.display_name || p.name : id;
}

export function findSession(id) {
  for (const g of state.groups) {
    const s = (g.sessions || []).find((x) => x.id === id);
    if (s) return { group: g, session: s };
  }
  return null;
}

// 解析会话的最终配置（组配置 + 会话覆盖）
export function effectiveView(id) {
  const f = findSession(id);
  if (!f) return null;
  const { group, session } = f;
  const confId =
    session.conf_id === undefined || session.conf_id === null
      ? group.conf_id
      : session.conf_id || null;
  return {
    id: session.id,
    name: session.name || session.id,
    platform_id: session.platform_id || group.platform_id || "webchat",
    conf_id: confId,
    sender_id: session.sender_id || group.sender_id || "testbench",
    sender_name: session.sender_name || group.sender_name || "测试台",
    message_type: session.message_type || group.message_type || "FriendMessage",
    auto_at: session.auto_at ?? group.auto_at ?? true,
    chat_group_id: session.chat_group_id ?? group.chat_group_id ?? null,
    group_name: group.name,
  };
}
