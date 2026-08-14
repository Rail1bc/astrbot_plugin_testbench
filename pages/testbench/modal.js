// modal.js — 自绘弹窗（iframe 沙箱禁用原生 alert/confirm，用自绘弹窗替代）
// 弹窗回调只在本模块内部持有（modalCallback），其他模块通过 openModal /
// showModal / hideModal 使用，无需感知弹窗内部状态。
// 交互细节（TB-03）：dirty 弹窗（表单类）在取消/遮罩点击/Esc 时先经内联
// 确认条（不销毁表单 DOM，避免重建丢失输入事件监听），确认后才关闭；
// Esc 可关闭、Tab 焦点圈定在弹窗内、关闭后焦点还原到打开前元素。
const $ = (id) => document.getElementById(id);

let modalCallback = null;
// dirty 确认条确认后要执行的动作（关闭后调用，如 onCancel 回调）
let pendingCloseAction = null;
// 打开弹窗前处于焦点的元素：关闭时还原
let lastFocused = null;

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

// 弹窗内可聚焦元素（供焦点圈定：Tab 循环不跳出弹窗）
function focusableIn(scope) {
  return [...scope.querySelectorAll("button, input, select, textarea, [tabindex]")].filter(
    (el) =>
      !el.disabled &&
      el.tabIndex >= 0 &&
      el.offsetParent !== null && // 过滤 display:none / hidden 子树
      !el.hidden,
  );
}

// 关闭请求的统一入口：dirty 弹窗先显示内联确认条，确认后才真正关闭；
// 无 dirty 直接关闭并执行 afterClose（如 onCancel 回调）
function requestClose(afterClose) {
  const cb = modalCallback;
  if (cb && cb.dirty) {
    pendingCloseAction = afterClose || null;
    $("modal-confirm").hidden = false;
    $("modal-actions").hidden = true;
    $("modal-confirm-discard").focus();
    return;
  }
  hideModal();
  if (afterClose) afterClose();
}

export function openModal({
  title,
  content,
  okText = "确定",
  cancelText = "取消",
  danger = false,
  showCancel,
  wide = false,
  // dirty=true 的表单弹窗：取消/遮罩点击/Esc 关闭前须经「放弃修改？」确认，
  // 防止误关丢失正在编辑的长表单（评审 Profile、测试集配置等）
  dirty = false,
  onOk,
  onCancel,
} = {}) {
  const body = $("modal-body");
  body.innerHTML = "";
  clearModalError();
  // dirty 弹窗的确认条状态复位（再次打开时从干净态开始）
  $("modal-confirm").hidden = true;
  $("modal-actions").hidden = false;
  lastFocused = document.activeElement;
  if (title) {
    const h = document.createElement("div");
    h.className = "modal-title";
    h.id = "modal-title"; // aria-labelledby 关联（index.html 的 .modal）
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
  modalCallback = { onOk: onOk || null, onCancel: onCancel || null, dirty: Boolean(dirty) };
  $("modal-mask").hidden = false;
  const first = body.querySelector("input, select, textarea");
  if (first) first.focus();
}

export function showModal(text, opts = {}) {
  openModal({ content: text, ...opts });
}

export function hideModal() {
  $("modal-mask").hidden = true;
  $("modal-confirm").hidden = true;
  $("modal-actions").hidden = false;
  pendingCloseAction = null;
  modalCallback = null;
  // 焦点还原到打开弹窗前的元素（元素可能已随视图刷新被移除，防御式取）
  const target = lastFocused;
  lastFocused = null;
  if (target && typeof target.focus === "function" && target.isConnected) {
    target.focus();
  }
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
  requestClose(() => {
    if (cb && cb.onCancel) cb.onCancel();
  });
});

$("modal-mask").addEventListener("click", (e) => {
  if (e.target === $("modal-mask")) requestClose(null);
});

// dirty 确认条：「继续编辑」返回表单（焦点回主操作），「放弃修改」关闭弹窗
$("modal-confirm-keep").addEventListener("click", () => {
  pendingCloseAction = null;
  $("modal-confirm").hidden = true;
  $("modal-actions").hidden = false;
  $("modal-ok").focus();
});

$("modal-confirm-discard").addEventListener("click", () => {
  const action = pendingCloseAction;
  hideModal();
  if (action) action();
});

// Esc 关闭弹窗（dirty 时走确认条）；确认条已打开时 Esc = 继续编辑
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("modal-mask").hidden) return;
  if (!$("modal-confirm").hidden) {
    $("modal-confirm-keep").click();
    return;
  }
  e.preventDefault();
  const cb = modalCallback;
  requestClose(() => {
    if (cb && cb.onCancel) cb.onCancel();
  });
});

// Tab 焦点圈定：焦点在弹窗内循环（Shift+Tab 反向），不逃逸到页面背景
document.addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  if ($("modal-mask").hidden) return;
  const focusables = focusableIn($("modal-mask").querySelector(".modal"));
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});
