# Windows QQ 群消息转发器

这是一个独立的 Windows 项目，用于监听 Windows 通知栏中的 QQ A 群通知，并将文本和可取得的图片转发到 B 群的 QQ 官方机器人。

```text
QQ Windows 通知栏
        │ Windows UI Automation 实时 toast
        ▼
本项目：解析、去重、本地队列、重试
        │ QQ 图片缓存探测（图片通知）
        ▼
本地图片暂存与 QQ 官方机器人 API
        ▼
B 群
```

## 重要限制

这是个人 QQ 的“尽力转发”实现。QQ Windows 客户端没有向第三方公开个人账号群消息 API，因此本项目先通过 Windows UI Automation 读取实时通知；图片通知会进一步自动打开目标群并尝试复制最新图片，存在以下限制：

- 只能读取 Windows 通知栏中实际出现的实时通知；
- Windows 通知关闭、QQ 免打扰或通知被系统聚合时可能漏消息；
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

运行测试时额外安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

将 B 群机器人的 `client_secret` 放进当前 PowerShell 会话，不要写入配置文件：

```powershell
$env:QQ_BOT_CLIENT_SECRET = "替换为机器人密钥"
```

然后编辑 `config.toml`，至少填写通知弹窗中的目标 A 群名称 `group_name` 和 B 群 `group_openid`。群名使用精确匹配；A 群改名后只需修改 `group_name`。

## 校准 QQ 窗口

让个人 QQ 登录，打开 Windows 通知，并让 A 群产生一条新消息，然后运行：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-window --config config.toml
```

命令会输出当前可见通知弹窗和文本。先根据输出确认 QQ 通知是否包含 `QQ`、群名和消息正文。通知监听不需要打开 A 群聊天窗口。

## 图片获取

图片通知的处理顺序为：

1. 自动打开或切换到 `group_name` 对应的 QQ 群；
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
image_cache_match_seconds = 20.0
image_cache_settle_seconds = 0.25
image_cache_wait_seconds = 5.0
```

匹配到的图片会先复制到 `data/image-cache`，发送成功后自动删除；发送失败会保留，以便重试。程序只选择图片文件，并优先选择同一时间窗口中体积较大的文件，通常可以避开缩略图。

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

`start.ps1` 会启动本机 Web 控制面，默认地址为 `http://127.0.0.1:8765`。页面可以启动、停止、重启转发进程，切换 dry-run，查看队列统计和日志，并执行 QQ 通知弹窗诊断。

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

先用 `dry_run = true` 或命令行 `--dry-run` 验证读取结果。确认能读到新通知后，再改为 `dry_run = false` 开启真实发送；dry-run 不会把消息标记为已发送，队列会保留。

## 数据与安全

- 消息队列保存在 `data/forwarder.sqlite3`；
- 已发送消息会保留状态，用于防止程序重启后重复发送；
- 日志会记录监听到及准备转发的消息正文，请注意 `data/forwarder.log` 属于本地敏感数据；
- 机器人密钥只从环境变量读取；
- SQLite 队列中的正文属于本地敏感数据，请将 `data/` 加入备份和访问控制策略；
- `config.toml` 和 `data/` 不应提交到 Git。

## 当前实现范围

已包含：

- Windows UI Automation 读取 QQ 实时通知弹窗，不读取 QQ 聊天主窗口；
- 使用 `group_name` 精确过滤 A 群；
- Windows 聚合多个 QQ 通知时只提取目标群紧邻的消息正文；
- 短时间内容去重，避免通知控件重建导致重复转发；
- 图片通知自动通过 QQ 窗口复制，失败时匹配 QQ 本地缓存，再失败时转发 `[图片]` 提示；
- 启动时基线初始化；
- SQLite 持久化队列和去重；
- 官方 QQ 机器人 B 群文本发送；
- 有限重试；
- 发送成功后标记；
- 通知弹窗诊断命令；
- 纯逻辑单元测试。

暂未包含：

- 非图片文件、语音和复杂消息类型转发；
- 复杂消息卡片原样复制；
- 可靠的个人 QQ 消息 ID；
- Windows 服务安装器；
- 对所有 QQ 版本和所有通知样式的通用适配；
