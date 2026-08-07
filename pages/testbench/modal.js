// modal.js — 自绘弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代）
// 弹窗回调只在本模块内部持有（modalCallback），其他模块通过 openModal /
// showModal / hideModal 使用，无需感知弹窗内部状态。
const $ = (id) => document.getElementById(id);

let modalCallback = null;

function showModalError(msg) {
  const el = $("modal-error");
  el.textContent = msg;
  el.hidden = false;
}

function clearModalError() {
  const el = $("modal-error");
  el.hidden = true;
  el.textContent = "";
}

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
  clearModalError();
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
  if (!cb || !cb.onOk) {
    hideModal();
    return;
  }
  clearModalError();
  try {
    await cb.onOk();
  } catch (err) {
    // onOk 校验 / 请求失败：弹窗不关闭、保留表单内容，错误内联提示——
    // 用户修正后可直接再次保存，不再因弹窗关闭丢失编辑进度（曾出现：
    // 新建评审 Profile 不合规弹错后，已填内容全部丢失）。
    showModalError(err.message || String(err));
    return;
  }
  hideModal();
});

$("modal-cancel").addEventListener("click", () => {
  const cb = modalCallback;
  hideModal();
  if (cb && cb.onCancel) cb.onCancel();
});

$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) hideModal();
});
