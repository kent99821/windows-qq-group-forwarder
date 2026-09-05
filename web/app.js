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
    $("process-value").textContent = running ? `PID ${status.pid}` : "未运行";
    $("pending-value").textContent = status.messages?.pending ?? 0;
    $("sent-value").textContent = status.messages?.sent ?? 0;
    $("discarded-value").textContent = status.messages?.discarded ?? 0;
    $("status-detail").textContent = status.config_error || (status.config_exists ? status.config_path : "未找到 config.toml");
    $("start-button").disabled = running;
    $("stop-button").disabled = !running;
    $("restart-button").disabled = !running;
    $("bind-button").disabled = running;
  } catch (error) {
    $("status-badge").textContent = "控制面异常";
    $("status-badge").className = "badge offline";
    show(error.message);
  }
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
