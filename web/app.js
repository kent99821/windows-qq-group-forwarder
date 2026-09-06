const $ = (id) => document.getElementById(id);

function show(message) {
  $("toast").textContent = message;
  window.clearTimeout(show.timer);
  show.timer = window.setTimeout(() => { $("toast").textContent = ""; }, 5500);
}

let replayMode = null;
let replayListenerName = "";
let preflightBusy = false;

function setButtonBusy(button, busy, loadingText = "处理中…") {
  if (!button) return;
  if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
  if (busy) {
    button.dataset.wasDisabled = button.disabled ? "true" : "false";
    button.disabled = true;
  } else {
    button.disabled = button.dataset.wasDisabled === "true";
    delete button.dataset.wasDisabled;
  }
  button.classList.toggle("is-loading", busy);
  button.setAttribute("aria-busy", busy ? "true" : "false");
  button.innerHTML = busy
    ? `<span class="button-spinner" aria-hidden="true"></span>${loadingText}`
    : `<span class="button-label">${button.dataset.idleLabel}</span>`;
}

async function runButtonTask(button, loadingText, task) {
  setButtonBusy(button, true, loadingText);
  try {
    await task();
  } catch (error) {
    show(error.message);
  } finally {
    setButtonBusy(button, false);
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json();
  if (!response.ok || body.ok === false) throw new Error(body.error || `请求失败 (${response.status})`);
  return body;
}

async function refreshStatus() {
  try {
    const status = await request("/api/status");
    const running = status.running;
    $("status-badge").textContent = running ? "运行中" : "已停止";
    $("status-badge").className = `badge ${running ? "online" : "offline"}`;
    $("process-value").textContent = running
      ? (status.pid ? `PID ${status.pid}` : (status.external_instance ? "其他窗口启动" : "运行中"))
      : "未运行";
    $("pending-value").textContent = status.messages?.pending ?? 0;
    $("failed-value").textContent = status.messages?.failed ?? 0;
    $("failed-count").textContent = `${status.messages?.failed ?? 0} 条`;
    $("sent-value").textContent = status.messages?.sent ?? 0;
    $("discarded-value").textContent = status.messages?.discarded ?? 0;
    if (typeof status.dry_run === "boolean") {
      $("dry-run").checked = status.dry_run;
      $("dry-run-state").textContent = status.dry_run ? "只监听，不发送" : "真实发送到 QQ 群";
    }
    $("dry-run").disabled = running;
    $("dry-run-hint").textContent = running
      ? "转发服务运行中，运行模式已锁定；请先停止服务后再修改。"
      : ($("dry-run").checked ? "开启时只监听通知并入队，不会向 QQ 群真实发送消息。" : "关闭后会向 QQ 群真实发送消息。");
    const credentialWarning = !status.client_secret_configured && !$('dry-run').checked
      ? `当前 Web 控制面未读取 ${status.client_secret_env || "机器人密钥环境变量"}，请重新启动 Web 控制面。`
      : "";
    $("status-detail").textContent = status.config_error
      || credentialWarning
      || (status.external_instance ? "已有其他窗口启动的转发服务，请在原窗口停止后再操作。" : (status.config_exists ? status.config_path : "未找到 config.toml"));
    $("start-button").disabled = running;
    $("stop-button").disabled = !running || status.external_instance;
    $("restart-button").disabled = !running || status.external_instance;
    $("bind-button").disabled = running;
    $("view-failed-button").disabled = running || !(status.messages?.failed > 0);
    $("retry-all-failed-button").disabled = running || !(status.messages?.failed > 0);
    $("preview-history-button").disabled = running || !(status.listener_names || []).length;
    $("history-listener-select").disabled = running || !(status.listener_names || []).length;
    renderListenerNames(status.listener_names || status.listener_groups || [], running);
  } catch (error) {
    $("status-badge").textContent = "控制面异常";
    $("status-badge").className = "badge offline";
    show(error.message);
  }
}

function renderListenerNames(names, running) {
  const list = $("listener-names-list");
  const count = $("listener-names-count");
  count.textContent = `${names.length} 个`;
  list.replaceChildren();
  if (!names.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无监听会话";
    list.appendChild(empty);
  } else {
    names.forEach((nameValue) => {
      const row = document.createElement("div");
      row.className = "listener-group-row";
      const name = document.createElement("span");
      name.textContent = nameValue;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger-button";
      remove.textContent = "删除";
      remove.disabled = running || names.length <= 1;
      remove.addEventListener("click", () => removeListenerName(nameValue));
      row.append(name, remove);
      list.appendChild(row);
    });
  }
  $("add-listener-name").disabled = running;
  $("listener-name").disabled = running;
  const select = $("history-listener-select");
  const previous = select.value;
  select.replaceChildren();
  names.forEach((nameValue) => {
    const option = document.createElement("option");
    option.value = nameValue;
    option.textContent = nameValue;
    select.appendChild(option);
  });
  if (names.includes(previous)) select.value = previous;
  select.disabled = running || !names.length;
}

async function addListenerName() {
  const input = $("listener-name");
  const listenerName = input.value.trim();
  if (!listenerName) { show("请输入 QQ 群名或联系人昵称"); return; }
  try {
    const body = await request("/api/actions/listener-names", {
      method: "POST",
      body: JSON.stringify({ action: "add", listener_name: listenerName }),
    });
    input.value = "";
    show(`已添加监听会话：${listenerName}`);
    renderListenerNames(body.listener_names || body.listener_groups || [], false);
    await refreshStatus();
  } catch (error) { show(error.message); }
}

async function removeListenerName(listenerName) {
  if (!window.confirm(`确定删除监听会话“${listenerName}”吗？`)) return;
  try {
    const body = await request("/api/actions/listener-names", {
      method: "POST",
      body: JSON.stringify({ action: "remove", listener_name: listenerName }),
    });
    show(`已删除监听会话：${listenerName}`);
    renderListenerNames(body.listener_names || body.listener_groups || [], false);
    await refreshStatus();
  } catch (error) { show(error.message); }
}

async function action(path) {
  const button = path.endsWith("/start") ? $("start-button")
    : path.endsWith("/stop") ? $("stop-button")
      : $("restart-button");
  setButtonBusy(button, true, path.endsWith("/start") ? "检查并启动中…" : "处理中…");
  try {
    show(path.endsWith("/start") ? "正在执行运行前检查并启动…" : "正在执行操作…");
    await request(path, { method: "POST", body: JSON.stringify({ dry_run: $("dry-run").checked }) });
    show("操作已完成");
    await refreshLog();
  } catch (error) { show(error.message); }
  finally {
    setButtonBusy(button, false);
    await refreshStatus();
  }
}

function renderCheckItems(targetId, items, emptyText) {
  const target = $(targetId);
  target.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "muted small-copy";
    empty.textContent = emptyText;
    target.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "check-item";
    const label = document.createElement("strong");
    label.textContent = item.label;
    const detail = document.createElement("span");
    detail.textContent = item.detail;
    row.append(label, detail);
    target.appendChild(row);
  });
}

async function runPreflight() {
  const button = $("preflight-button");
  if (preflightBusy) return;
  preflightBusy = true;
  setButtonBusy(button, true, "检查中…");
  $("preflight-badge").textContent = "检查中";
  $("preflight-badge").className = "badge checking";
  $("preflight-progress").hidden = false;
  $("preflight-progress-text").textContent = "正在检查配置和本地运行条件…";
  $("preflight-summary").hidden = true;
  $("preflight-results").hidden = true;
  try {
    const body = await request("/api/actions/preflight", { method: "POST", body: "{}" });
    $("preflight-results").hidden = false;
    renderCheckItems("preflight-passed", body.passed || [], "暂无符合项");
    renderCheckItems("preflight-missing", body.missing || [], "没有缺少项");
    renderCheckItems("preflight-warnings", body.warnings || [], "没有额外提示");
    $("preflight-badge").textContent = body.ready ? "可以启动" : `缺少 ${(body.missing || []).length} 项`;
    $("preflight-badge").className = `badge ${body.ready ? "online" : "offline"}`;
    const passed = (body.passed || []).length;
    const missing = (body.missing || []).length;
    const warnings = (body.warnings || []).length;
    const summary = $("preflight-summary");
    summary.hidden = false;
    summary.className = `preflight-summary ${body.ready ? "is-ready" : "has-errors"}`;
    summary.innerHTML = `<strong>${body.ready ? "运行条件基本就绪" : "暂时无法启动"}</strong><span>符合 ${passed} 项</span><span>缺少或异常 ${missing} 项</span><span>提示 ${warnings} 项</span>`;
    $("preflight-last-check").textContent = `最近检查 ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`;
    show(body.ready ? "运行前检查通过" : "运行前检查未通过，请处理红色项目");
  } catch (error) {
    $("preflight-badge").textContent = "检查失败";
    $("preflight-badge").className = "badge offline";
    show(error.message);
  } finally {
    $("preflight-progress").hidden = true;
    preflightBusy = false;
    setButtonBusy(button, false);
  }
}

function openReplayDialog(title, summary, items, mode) {
  replayMode = mode;
  $("replay-dialog-title").textContent = title;
  $("replay-dialog-summary").textContent = summary;
  const list = $("replay-dialog-list");
  list.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("label");
    row.className = "replay-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = mode === "failed" ? item.message_key : item.message_id;
    const copy = document.createElement("div");
    const meta = document.createElement("div");
    meta.className = "replay-item-meta";
    meta.textContent = mode === "failed"
      ? `${item.source_group} · 已尝试 ${item.attempts} 次 · ${item.kind}`
      : `${item.source_group} · ${item.sender || "未知发送者"} · ${item.display_time || "时间未知"}`;
    const content = document.createElement("div");
    content.className = "replay-item-content";
    content.textContent = item.content || "（空消息）";
    copy.append(meta, content);
    if (mode === "failed") {
      const error = document.createElement("div");
      error.className = "replay-item-error";
      error.textContent = item.last_error || "未知错误";
      copy.appendChild(error);
    }
    row.append(checkbox, copy);
    list.appendChild(row);
  });
  $("replay-confirm").textContent = mode === "failed" ? "重试选中消息" : "将选中消息加入待发送";
  $("replay-confirm").disabled = !items.length;
  $("replay-select-all").disabled = !items.length;
  $("replay-dialog").showModal();
}

async function viewFailedMessages() {
  try {
    const body = await request("/api/replay/failed");
    openReplayDialog("选择失败消息", `共 ${body.count || 0} 条发送失败消息。`, body.items || [], "failed");
  } catch (error) { show(error.message); }
}

async function retryFailed(messageKeys) {
  try {
    const body = await request("/api/actions/retry-failed", {
      method: "POST",
      body: JSON.stringify({ message_keys: messageKeys }),
    });
    show(body.message);
    if ($("replay-dialog").open) $("replay-dialog").close();
    await refreshStatus();
  } catch (error) { show(error.message); }
}

async function previewHistory() {
  const listenerName = $("history-listener-select").value;
  if (!listenerName) { show("请先选择一个 QQ 群或联系人"); return; }
  replayListenerName = listenerName;
  await runButtonTask($("preview-history-button"), "读取中…", async () => {
    show(`正在打开“${listenerName}”并读取可见聊天记录…`);
    const body = await request("/api/actions/history-preview", {
      method: "POST",
      body: JSON.stringify({ listener_name: listenerName }),
    });
    openReplayDialog(
      `补发：${listenerName}`,
      `读取到 ${body.count || 0} 条当前可见的接收消息，请只勾选漏掉的内容。`,
      body.items || [],
      "history",
    );
  });
}

async function confirmReplaySelection() {
  const ids = Array.from($("replay-dialog-list").querySelectorAll("input:checked")).map((input) => input.value);
  if (!ids.length) { show("请至少选择一条消息"); return; }
  if (replayMode === "failed") {
    await retryFailed(ids);
    return;
  }
  try {
    const body = await request("/api/actions/replay-history", {
      method: "POST",
      body: JSON.stringify({ listener_name: replayListenerName, message_ids: ids }),
    });
    show(body.message);
    $("replay-dialog").close();
    await refreshStatus();
  } catch (error) { show(error.message); }
}

async function changeDryRun() {
  const toggle = $("dry-run");
  const enabled = toggle.checked;
  toggle.disabled = true;
  try {
    const body = await request("/api/actions/dry-run", {
      method: "POST",
      body: JSON.stringify({ dry_run: enabled }),
    });
    show("Dry-run 设置已保存，下次启动转发服务时生效");
    await refreshStatus();
  } catch (error) {
    toggle.checked = !enabled;
    show(error.message);
  } finally {
    toggle.disabled = false;
  }
}

async function refreshLog() {
  try {
    const body = await request("/api/log");
    $("log-output").textContent = body.lines?.join("\n") || "暂无日志";
    $("log-output").scrollTop = $("log-output").scrollHeight;
  } catch (error) { show(error.message); }
}

$("start-button").addEventListener("click", () => action("/api/actions/start"));
$("stop-button").addEventListener("click", () => action("/api/actions/stop"));
$("restart-button").addEventListener("click", () => action("/api/actions/restart"));
$("preflight-button").addEventListener("click", runPreflight);
$("dry-run").addEventListener("change", changeDryRun);
$("refresh-log").addEventListener("click", refreshLog);
$("inspect-button").addEventListener("click", async () => {
  await runButtonTask($("inspect-button"), "读取中…", async () => {
    const body = await request("/api/actions/inspect-window", { method: "POST", body: "{}" });
    const count = body.notifications?.length || 0;
    show(count ? `检测到 ${count} 个 QQ 通知弹窗` : "未检测到匹配的 QQ 通知弹窗");
  });
});
$("inspect-image-cache-button").addEventListener("click", async () => {
  await runButtonTask($("inspect-image-cache-button"), "扫描中…", async () => {
    const body = await request("/api/actions/inspect-image-cache", { method: "POST", body: "{}" });
    const roots = body.roots?.length || 0;
    const images = body.recent_images?.length || 0;
    show(`找到 ${roots} 个 QQ 图片缓存目录，最近图片 ${images} 个`);
  });
});
$("add-listener-name").addEventListener("click", addListenerName);
$("listener-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter") addListenerName();
});
$("bind-button").addEventListener("click", async () => {
  if (!window.confirm("请先停止转发，并准备在 QQ 群 @机器人发送“绑定”。继续吗？")) return;
  await runButtonTask($("bind-button"), "连接中…", async () => {
    show("正在连接 QQ 机器人，请在 QQ 群发送：@机器人 绑定");
    const body = await request("/api/actions/bind-group", { method: "POST", body: "{}" });
    show(`QQ 群绑定成功：${body.group_openid_preview}`);
    await refreshStatus();
  });
});
$("test-message-button").addEventListener("click", async () => {
  if (!window.confirm("将立即向已绑定的 QQ 群真实发送一条测试消息，继续吗？")) return;
  await runButtonTask($("test-message-button"), "发送中…", async () => {
    show("正在向 QQ 群发送主动测试消息…");
    const body = await request("/api/actions/test-message", { method: "POST", body: "{}" });
    show(body.message || "主动测试消息已发送");
  });
});
$("view-failed-button").addEventListener("click", viewFailedMessages);
$("retry-all-failed-button").addEventListener("click", () => {
  if (window.confirm("确定将全部失败消息放回待发送队列吗？")) retryFailed(null);
});
$("preview-history-button").addEventListener("click", previewHistory);
$("replay-dialog-close").addEventListener("click", () => $("replay-dialog").close());
$("replay-select-all").addEventListener("click", () => {
  const boxes = Array.from($("replay-dialog-list").querySelectorAll("input[type=checkbox]"));
  const allChecked = boxes.length > 0 && boxes.every((box) => box.checked);
  boxes.forEach((box) => { box.checked = !allChecked; });
  $("replay-select-all").textContent = allChecked ? "全选" : "全不选";
});
$("replay-confirm").addEventListener("click", confirmReplaySelection);

refreshStatus();
refreshLog();
window.setInterval(refreshStatus, 2000);
window.setInterval(refreshLog, 5000);
