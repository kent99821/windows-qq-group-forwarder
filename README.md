# Windows QQ 消息转发器

这是一个独立的 Windows 项目，用于监听 Windows 通知栏中的 QQ 群或联系人通知，并将文本和可取得的图片转发到 B 群的 QQ 官方机器人。

```text
QQ Windows 通知栏
        │ Windows UserNotificationListener API
        ▼
通知触发器 ──→ 自动打开对应 QQ 会话并补读最近可见聊天记录
        │       （补回通知被覆盖的连续消息）
        ▼
本项目：按顺序去重、本地队列、重试
        │ QQ 窗口复制 / 图片缓存探测（图片通知）
        ▼
本地图片暂存与 QQ 官方机器人 API
        ▼
B 群
```

## 重要限制

这是个人 QQ 的“尽力转发”实现。QQ Windows 客户端没有向第三方公开个人账号群消息 API，因此本项目通过 Windows `UserNotificationListener` API 读取系统通知历史，并优先使用通知变化事件、事件不可用时使用 200ms 快速轮询；API 不可用或没有返回可匹配通知、但屏幕上存在可见通知时，自动回退到 Windows UI Automation。通知到达后，程序会临时切换到对应 QQ 会话，从聊天窗口补读最近可见记录，再与通知合并去重，用于补回通知被后一条覆盖的连续消息。通知采集、聊天补读、图片处理和机器人发送相互独立，避免较慢的 UI 操作暂停后续通知读取。图片通知会进一步尝试复制最新图片，存在以下限制：

- 只能读取 Windows 通知栏中实际出现的实时通知；
- `UserNotificationListener` 读取的是 Windows 通知记录，不是 QQ 服务器消息；服务启动时会把已有通知作为基线，不会补发启动前的通知；
- Windows 通知仍是补读触发器；通知关闭或 QQ 免打扰时不会主动扫描聊天记录；
- 聊天补读要求 QQ 会话窗口已创建，可以正常打开或最小化到任务栏；如果关闭到系统托盘、QQ 未登录或 Windows 锁屏，则回退为仅转发通知；
- 服务启动时会依次读取各监听会话作为基线，不会把当前可见的旧聊天记录重新转发；
- Windows 图片通知通常只包含 `[图片]` 占位符；程序会尝试从 QQ NT 本地图片缓存中匹配通知前后新写入的原图，匹配不到时回退为图片提示；
- 图片自动复制需要 QQ 窗口可操作，并可能短暂抢占前台焦点；Windows 锁屏、QQ 未登录、QQ UIA 未暴露图片控件时会回退；
- 文件、语音、复杂表情和引用消息暂不保证；
- 同样正文连续出现时，UI 自动化很难 100% 区分两条不同消息；
- 不要把主 QQ 账号密码交给本项目，也不要使用模拟登录协议。

如果后续确认漏消息不可接受，应改为让官方机器人加入 A 群，或重新评估账号和平台规则允许的消息接入方式。

## Windows 安装

建议 Python 3.12 或更高版本。

```powershell
cd windows-qq-group-forwarder
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.toml config.toml
```

项目优先使用 Windows `UserNotificationListener` API。首次使用或权限变化后，请在 Windows 设置中允许应用访问通知；如果通知变化事件不可用，程序会自动使用 200ms 快速轮询；如果 API 返回权限不足，则使用 UI Automation 读取可见通知。运行 `inspect-window` 后，输出中的 `backend` 应为 `windows-user-notification-listener`，表示当前使用的是 API。

运行测试时额外安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

将 B 群机器人的 `client_secret` 放进当前 PowerShell 会话，不要写入配置文件：

```powershell
$env:QQ_BOT_CLIENT_SECRET = "替换为机器人密钥"
```

然后编辑 `config.toml`，至少填写 Windows 通知中显示的 QQ 会话名称和 B 群 `group_openid`。会话名称可以是群名，也可以是联系人昵称，使用精确匹配；也可以在 Web UI 的“监听 QQ 会话”区域维护多个监听对象。

多个监听会话配置示例：

```toml
[source]
listener_names = ["发家致富", "第二个群", "联系人昵称"]
group_name = "发家致富" # 兼容旧配置，始终保持为列表第一个会话名称
group_names = ["发家致富", "第二个群", "联系人昵称"] # 兼容旧配置
```

新增或删除监听会话前需要先停止转发服务。保存后重启转发服务即可生效；每条消息会保留实际来源会话名称，图片复制也会切换到对应 QQ 会话。

转发到 B 群的文本消息会自动带上监听到消息时的本机时间，例如：`[A群转发] [2026-09-05 14:30:00] 小明: 你好`。时间取自通知被监听到的时间，并按 Windows 本地时区显示；已取得原图的图片消息仍以单独图片消息发送，图片通知未取得原图时会在占位文本中显示时间。

## 校准 QQ 窗口

让个人 QQ 登录，打开 Windows 通知，并让 A 群产生一条新消息，然后运行：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-window --config config.toml
```

命令会输出当前可见通知弹窗和文本。先根据输出确认 QQ 通知是否包含 `QQ`、会话名称和消息正文。通知采集本身不要求聊天窗口保持前台，但连续消息补读要求 QQ 主窗口已打开或最小化到任务栏，不能关闭到系统托盘。

## 图片获取

图片通知的处理顺序为：

1. 自动打开或切换到监听会话名称对应的 QQ 群或联系人会话；
2. 选择聊天区最下方可见的图片控件并发送复制操作；
3. 从 Windows 图片剪贴板保存 PNG，上传到 B 群；
4. UI 自动化失败时，再尝试 QQ 本地缓存；最后回退为 `[图片]` 占位提示。

QQ 窗口需要保持登录，Windows 不能锁屏。复制图片时可能影响当前鼠标键盘焦点。

## 图片缓存探测

程序默认探测以下 QQ NT 常见缓存位置：

- `%LOCALAPPDATA%\Tencent\QQ\nt_qq\nt_data\Pic`
- `%APPDATA%\Tencent\QQ\nt_qq\nt_data\Pic`
- `%USERPROFILE%\Documents\Tencent Files\<QQ号>\nt_qq\nt_data\Pic`

启动日志会打印实际发现的目录。收到图片通知后，日志会显示“匹配到 QQ 图片缓存”以及候选数量；如果未找到，则会显示“图片通知已收到，但 QQ 缓存中未发现近期图片文件”。

如果自动探测不到目录，可以在 `config.toml` 中手动填写：

```toml
[source]
image_cache_paths = ["C:/Users/你的用户名/Documents/Tencent Files/你的QQ号/nt_qq/nt_data/Pic"]
poll_interval_seconds = 0.2
image_cache_match_seconds = 60.0
image_cache_settle_seconds = 0.25
image_cache_wait_seconds = 45.0
```

匹配到的图片会先复制到 `data/image-cache`，发送成功后自动删除；发送失败会保留，以便重试。QQ NT 缓存匹配遵循以下规则：只扫描 `Pic/<月份>/Ori` 和 `Pic/<月份>/Thumb`，忽略 `OriTemp`、`ThumbTemp` 及普通图片文件；`Ori` 使用 32 位十六进制哈希文件名，`Thumb` 使用同一哈希加 `_0` 或 `_720` 后缀；同一哈希只计为一条图片，原图优先，多个候选按进入缓存的时间顺序处理。若缩略图对应的原图已在历史缓存中，也会通过共享哈希关联到原图。

日志中的 `kind`、`hash`、`mtime` 和 `age_seconds` 可用于确认缓存文件类型、关联键和进入缓存的时间。缓存只能判断文件是否与当前通知时间接近，不能从哈希反推出来源群/联系人；同一张图片重复发送且 QQ 不重新写入缓存时，仍无法仅靠缓存区分两条消息。

也可以单独检查缓存目录和最近图片：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-image-cache --config config.toml
```

## 启动

启动后，程序只观察 Windows 桌面上实际出现的 QQ 通知弹窗，并将启动前已经显示的弹窗作为基线，不补发已经显示的通知。请在设置机器人密钥的同一个 PowerShell 窗口中启动 Web 控制面，否则子进程不会继承密钥：

```powershell
.\.venv\Scripts\python.exe -m app.main run --config config.toml
```

也可以使用：

```powershell
.\start.ps1
```

`start.ps1` 会启动本机 Web 控制面，默认地址为 `http://127.0.0.1:8765`。页面可以启动、停止、重启转发进程，使用 Dry-run 开关切换运行模式，查看队列统计和日志，并执行 QQ 通知弹窗诊断。新增“运行前检查”会把配置、依赖、QQ 客户端、通知监听、缓存目录和机器人连接分为“符合”“缺少或异常”“提示”；启动按钮也会自动执行同一检查。B 群绑定后可点击“发送主动测试”验证机器人主动发言权限。

页面的补发功能分为两类：发送失败的消息会进入失败队列，停止服务后可选择重试；通知曾经漏掉但消息仍在 QQ 聊天窗口中的内容，可在“从 QQ 窗口补发”中选择会话、读取当前可见记录并勾选补发。历史补发使用独立的本地消息键，重复点击不会重复入队；图片历史记录在无法取得原图时会按 `[图片]` 占位提示发送。

如果转发服务是从其他控制台或旧的 Web 控制面启动的，页面会通过单实例锁识别它，并显示“其他窗口启动”；此时启动按钮会给出明确提示，不会重复创建进程。请先关闭原控制台中的转发服务，再刷新页面操作。

Web 控制面和转发服务都带有单实例锁，重复启动时会提示已有实例运行。Windows 虚拟环境可能为一个服务显示“启动器 + 实际解释器”两个 Python 进程，这是正常现象；不应同时出现两个 `app.main run` 实例。

在启动 `start.ps1` 的 PowerShell 中按 `Ctrl+C` 会同时关闭 Web 控制面和它启动的整棵转发进程树。也可以先在网页点击“停止”只关闭转发服务。

查看本项目当前进程：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "windows-qq-group-forwarder" } |
  Select-Object ProcessId, ParentProcessId, Name, CommandLine
```

页面中的“绑定 B 群”会连接 QQ 机器人 WebSocket。请先停止转发服务，再点击绑定，并在 B 群发送 `@机器人 绑定`。程序收到事件后会自动回复并将 `group_openid` 写入 `config.toml`。

转发服务启动后会持续保持 QQ 机器人网关连接，QQ 开放平台中的机器人状态应显示为在线。Dry-run 模式也会尝试保持网关在线，但如果没有设置机器人密钥，会只记录警告并继续运行窗口诊断。

也可以直接启动控制面：

```powershell
.\.venv\Scripts\python.exe -m app.main web --config config.toml
```

先打开 Web UI 中的 Dry-run 开关验证读取结果。转发服务运行期间该开关会被锁定；如需切换，请先停止服务，修改开关后再启动。Dry-run 不会把消息标记为已发送，队列会保留。开关状态会保存到 `config.toml`，下次启动继续使用。

## 数据与安全

- 消息队列保存在 `data/forwarder.sqlite3`；
- 已发送消息会保留状态，用于防止程序重启后重复发送；
- 日志会记录监听到及准备转发的消息正文，请注意 `data/forwarder.log` 属于本地敏感数据；
- 机器人密钥只从环境变量读取；
- SQLite 队列中的正文属于本地敏感数据，请将 `data/` 加入备份和访问控制策略；
- `config.toml` 和 `data/` 不应提交到 Git。

## 当前实现范围

已包含：

- Windows `UserNotificationListener` API 读取 QQ 通知并触发聊天窗口补读；通知变化事件不可用时使用 200ms 快速轮询，API 不可用时回退到通知 UI Automation；
- 自动从对应 QQ 群或联系人聊天窗口补读最近可见消息，按发送顺序补回被 Windows 通知覆盖的前序消息；
- 通过 QQ 左侧会话列表精确切换目标会话，并在通知后合并短时间内的多次聊天快照，覆盖连续消息稍晚渲染和列表虚拟化场景；
- 聊天补读失败时保留原始通知，不会因为 UI Automation 失败而吞掉当前消息；
- 聊天补读与图片复制共用界面操作锁，避免同时切换会话导致读错或复制错图片；
- 通知采集、图片处理和机器人发送分离运行，减少发送延迟导致的通知丢失；
- 使用 `listener_names` 精确过滤 QQ 群或联系人会话；
- Windows 聚合多个 QQ 通知时只提取目标群紧邻的消息正文；
- 短时间内容去重，避免通知控件重建导致重复转发；
- 图片通知自动通过 QQ 窗口复制，失败时匹配 QQ 本地缓存，再失败时转发 `[图片]` 提示；
- 启动时基线初始化；
- SQLite 持久化队列和去重；
- 官方 QQ 机器人 B 群文本发送；
- 有限重试；
- 发送失败队列和 Web UI 选择性重试；
- Web UI 主动消息测试；
- Web UI 运行前检查，区分通过项、缺少项和警告；
- 从 QQ 当前可见聊天记录中选择历史消息补发；
- 发送成功后标记；
- 通知弹窗诊断命令；
- 纯逻辑单元测试。

暂未包含：

- 非图片文件、语音和复杂消息类型转发；
- 复杂消息卡片原样复制；
- 可靠的个人 QQ 消息 ID；
- Windows 服务安装器；
- 对所有 QQ 版本和所有通知样式的通用适配；
