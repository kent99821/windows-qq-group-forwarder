const $ = (id) => document.getElementById(id);

function show(message) {
  $("toast").textContent = message;
  window.clearTimeout(show.timer);
  show.timer = window.setTimeout(() => { $("toast").textContent = ""; }, 3500);
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
    $("sent-value").textContent = status.messages?.sent ?? 0;
    $("discarded-value").textContent = status.messages?.discarded ?? 0;
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
  try {
    await request(path, { method: "POST", body: JSON.stringify({ dry_run: $("dry-run").checked }) });
    show("操作已完成");
    await refreshStatus();
    await refreshLog();
  } catch (error) { show(error.message); }
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
$("refresh-log").addEventListener("click", refreshLog);
$("inspect-button").addEventListener("click", async () => {
  try {
    const body = await request("/api/actions/inspect-window", { method: "POST", body: "{}" });
    const count = body.notifications?.length || 0;
    show(count ? `检测到 ${count} 个 QQ 通知弹窗` : "未检测到匹配的 QQ 通知弹窗");
  } catch (error) { show(error.message); }
});
$("inspect-image-cache-button").addEventListener("click", async () => {
  try {
    const body = await request("/api/actions/inspect-image-cache", { method: "POST", body: "{}" });
    const roots = body.roots?.length || 0;
    const images = body.recent_images?.length || 0;
    show(`找到 ${roots} 个 QQ 图片缓存目录，最近图片 ${images} 个`);
  } catch (error) { show(error.message); }
});
$("add-listener-name").addEventListener("click", addListenerName);
$("listener-name").addEventListener("keydown", (event) => {
  if (event.key === "Enter") addListenerName();
});
$("bind-button").addEventListener("click", async () => {
  if (!window.confirm("请先停止转发，并准备在 B 群 @机器人发送“绑定”。继续吗？")) return;
  try {
    show("正在连接 QQ 机器人，请在 B 群发送：@机器人 绑定");
    const body = await request("/api/actions/bind-group", { method: "POST", body: "{}" });
    show(`B 群绑定成功：${body.group_openid_preview}`);
    await refreshStatus();
  } catch (error) { show(error.message); }
});

refreshStatus();
refreshLog();
window.setInterval(refreshStatus, 2000);
window.setInterval(refreshLog, 5000);
