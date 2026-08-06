// modal.js — 自绘弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代）
// 弹窗回调只在本模块内部持有（modalCallback），其他模块通过 openModal /
// showModal / hideModal 使用，无需感知弹窗内部状态。
const $ = (id) => document.getElementById(id);

let modalCallback = null;

export function openModal({
  title,
  content,
  okText = "确定",
  cancelText = "取消",
  danger = false,
  showCancel,
  wide = false,
  onOk,
  onCancel,
} = {}) {
  const body = $("modal-body");
  body.innerHTML = "";
  if (title) {
    const h = document.createElement("div");
    h.className = "modal-title";
    h.textContent = title;
    body.appendChild(h);
  }
  if (typeof content === "string") {
    const p = document.createElement("p");
    p.textContent = content;
    body.appendChild(p);
  } else if (content) {
    body.appendChild(content);
  }
  // 无 onOk 的纯提示弹窗默认只显示确定按钮
  const cancel = showCancel === undefined ? Boolean(onOk) : showCancel;
  $("modal-ok").textContent = okText;
  $("modal-ok").classList.toggle("danger", danger);
  $("modal-cancel").textContent = cancelText;
  $("modal-cancel").hidden = !cancel;
  $("modal-mask").querySelector(".modal").classList.toggle("modal-wide", wide);
  modalCallback = { onOk: onOk || null, onCancel: onCancel || null };
  $("modal-mask").hidden = false;
  const first = body.querySelector("input, select, textarea");
  if (first) first.focus();
}

export function showModal(text, opts = {}) {
  openModal({ content: text, ...opts });
}

export function hideModal() {
  $("modal-mask").hidden = true;
  modalCallback = null;
}

$("modal-ok").addEventListener("click", async () => {
  const cb = modalCallback;
  hideModal();
  if (!cb || !cb.onOk) return;
  try {
    await cb.onOk();
  } catch (err) {
    // 原实现把失败提示写到群发栏（showRunStatus），这里改用弹窗自身提示，
    // 避免 modal 模块反向依赖 app.js 的发送区
    showModal("操作失败: " + err.message);
  }
});

$("modal-cancel").addEventListener("click", () => {
  const cb = modalCallback;
  hideModal();
  if (cb && cb.onCancel) cb.onCancel();
});

$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) hideModal();
});
