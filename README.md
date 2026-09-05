# Windows QQ 消息转发器

这是一个独立的 Windows 项目，用于监听 Windows 通知栏中的 QQ 群或联系人通知，并将文本和可取得的图片转发到 B 群的 QQ 官方机器人。

本项目面向“个人 QQ 在 A 群/联系人中接收消息，官方 QQ 机器人在 B 群中转发消息”的场景。它不需要把机器人加入 A 群，但依赖 Windows 通知、QQ NT 客户端和本机 UI 能力，因此属于本地尽力转发工具。

当前版本定位为“单实例、单目标机器人”。单实例多机器人、多实例管理和资源评估已整理到[未来优化方向](docs/future-optimization-plan.md)，后续版本开发前应先参考该文档。

## 快速开始

1. 在 Windows 中登录 QQ，确保监听群或联系人能够产生通知。
2. 创建 Python 虚拟环境并安装依赖。
3. 复制 `config.example.toml` 为 `config.toml`，填写监听会话、机器人 AppID 和 B 群绑定信息。
4. 在启动 Web 控制面的同一个 PowerShell 窗口中设置 `QQ_BOT_CLIENT_SECRET`。
5. 启动 Web UI，依次执行“运行前检查”→“绑定 B 群”→“发送主动测试”→关闭 Dry-run 后启动转发。

默认 Web 地址：<http://127.0.0.1:8765/>

> [!IMPORTANT]
> `QQ_BOT_CLIENT_SECRET` 必须设置在启动 Web 控制面的同一个 PowerShell 进程中。已经打开的 Web 控制面不会自动读取后来新增的用户环境变量，修改环境变量后需要重启 Web 控制面。

## 目录

- [工作原理](#工作原理)
- [项目结构](#项目结构)
- [安装与依赖](#windows-安装)
- [配置说明](#配置说明)
- [首次使用流程](#首次使用流程)
- [Web UI 操作](#web-ui-操作)
- [运行前检查与消息状态](#运行前检查的判定方式)
- [连续消息补读](#连续消息补读)
- [图片获取](#图片获取)
- [图片缓存探测](#图片缓存探测)
- [故障排查](#故障排查)
- [数据与安全](#数据与安全)
- [进程管理](#进程管理)
- [命令行运行](#命令行运行)
- [开发与测试](#开发与测试)
- [未来优化方向](docs/future-optimization-plan.md)

## 项目结构

```text
windows-qq-group-forwarder/
├─ app/
│  ├─ main.py                  # 转发服务入口
│  ├─ web.py                   # 本机 Web 控制面
│  ├─ config.py                # TOML 配置读取与保存
│  ├─ state_store.py           # SQLite 消息队列和状态
│  ├─ preflight.py             # 运行前检查
│  ├─ bot_gateway.py           # QQ 机器人绑定和网关保活
│  ├─ destination/qq_bot.py   # B 群文本/图片发送
│  └─ source/                  # 通知、QQ 历史和图片来源
├─ web/                        # Web UI 静态页面
├─ tests/                      # 自动化测试
├─ config.example.toml         # 配置模板
├─ start.ps1                   # Windows Web UI 启动脚本
└─ data/                       # 本地数据库、日志和图片暂存
```

`config.toml` 和 `data/` 包含本地敏感信息，默认已在 `.gitignore` 中排除，不要提交到公开仓库。

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

## 工作原理

消息处理链路如下：

```text
Windows QQ 通知
        ↓
UserNotificationListener / UI Automation 回退
        ↓
监听会话过滤
        ↓
QQ 聊天窗口历史补读与消息去重
        ↓
SQLite 本地队列
        ↓
QQ 官方机器人 API
        ↓
B 群
```

通知采集、历史补读、图片处理和机器人发送是相互独立的处理环节。这样机器人发送变慢时，不会直接暂停通知采集；发送失败的消息会进入失败队列，便于后续重试。

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

建议使用 Windows 10/11，并保持 QQ NT 已登录。项目不要求 QQ 聊天窗口处于前台，但历史补读和图片复制要求 QQ 主窗口已经创建，并且不能完全退出到系统托盘。

多个监听会话配置示例：

```toml
[source]
listener_names = ["发家致富", "第二个群", "联系人昵称"]
group_name = "发家致富" # 兼容旧配置，始终保持为列表第一个会话名称
group_names = ["发家致富", "第二个群", "联系人昵称"] # 兼容旧配置
```

新增或删除监听会话前需要先停止转发服务。保存后重启转发服务即可生效；每条消息会保留实际来源会话名称，图片复制也会切换到对应 QQ 会话。

转发到 B 群的文本消息会自动带上监听到消息时的本机时间，例如：`[A群转发] [2026-09-05 14:30:00] 小明: 你好`。时间取自通知被监听到的时间，并按 Windows 本地时区显示；已取得原图的图片消息仍以单独图片消息发送，图片通知未取得原图时会在占位文本中显示时间。

## 配置说明

| 配置项 | 作用 | 注意事项 |
| --- | --- | --- |
| `source.listener_names` | 监听的群名或联系人昵称 | 精确匹配，可配置多个；修改后需停止服务 |
| `source.app_name_contains` | 通知应用识别文本 | 通常填写 `QQ` |
| `source.poll_interval_seconds` | UI Automation 回退轮询间隔 | 通知事件不可用时生效，越小占用越高 |
| `source.exclude_texts` | 排除的界面文本 | 可根据通知诊断结果补充 |
| `source.image_cache_paths` | QQ 图片缓存目录 | 留空时自动探测 |
| `source.image_cache_match_seconds` | 图片缓存匹配时间范围 | 通知时间前后允许匹配的秒数 |
| `source.image_cache_settle_seconds` | 图片文件稳定等待时间 | 文件写入完成前的等待时间 |
| `source.image_cache_wait_seconds` | 等待图片缓存时间 | 收到图片通知后最多扫描多久 |
| `source.ui_image_wait_seconds` | QQ 窗口复制图片超时 | UI 自动复制图片的最长等待时间 |
| `destination.app_id` | QQ 官方机器人的 AppID | 不要填写机器人名称 |
| `destination.client_secret_env` | 保存密钥的环境变量名 | 只填写变量名，不要填写密钥本身 |
| `destination.group_openid` | B 群标识 | 通过 Web UI 绑定或手动填写 |
| `runtime.database_path` | SQLite 队列位置 | 建议保留在项目的 `data/` 目录 |
| `runtime.log_path` | 日志位置 | 日志包含消息正文，应妥善保护 |
| `runtime.dry_run` | 是否只监听不发送 | 运行中锁定，修改前需停止服务 |
| `runtime.max_send_attempts` | 单条消息最大尝试次数 | 达到后进入失败队列 |

最小可运行配置示例：

```toml
[source]
listener_names = ["QQ群名称", "联系人昵称"]
app_name_contains = "QQ"
poll_interval_seconds = 0.2
exclude_texts = []

[destination]
app_id = "你的机器人 AppID"
client_secret_env = "QQ_BOT_CLIENT_SECRET"
group_openid = "你的 B 群 group_openid"
message_prefix = "[A群转发]"

[runtime]
database_path = "data/forwarder.sqlite3"
log_path = "data/forwarder.log"
dry_run = true
max_send_attempts = 3
```

推荐优先使用 Web UI 添加监听会话和绑定 B 群，避免手动填写错误的群标识。`config.example.toml` 中包含图片缓存相关的完整可选配置。

`group_name` 和 `group_names` 是旧配置兼容字段，新的配置和 Web UI 使用通用名称 `listener_names`。名称既可以是群名，也可以是联系人昵称。

## 首次使用流程

### 1. 创建并配置机器人

在 QQ 开放平台创建机器人，记录 AppID 和 Client Secret，并为机器人开启目标 B 群的主动发言权限。Client Secret 不写入 `config.toml`，只设置为环境变量：

```powershell
$env:QQ_BOT_CLIENT_SECRET = "你的机器人 Client Secret"
```

### 2. 启动 Web 控制面

推荐使用：

```powershell
.\start.ps1
```

也可以直接运行：

```powershell
.\.venv\Scripts\python.exe -m app.main web --config config.toml
```

浏览器打开 <http://127.0.0.1:8765/>。

### 3. 运行前检查

点击“开始检查”。结果分为：

- **符合**：已经满足的运行条件；
- **缺少或异常**：会阻止真实发送或启动的项目；
- **提示**：不影响基础文本监听，但可能影响历史补读、图片复制或联网验证的项目。

启动按钮也会自动执行检查。真实发送模式下，缺少机器人密钥、AppID 或 B 群绑定时不会启动。

### 4. 绑定 B 群并主动测试

停止转发服务后点击“绑定 B 群”，再在 B 群中发送：

```text
@机器人 绑定
```

绑定成功后点击“发送主动测试”。该按钮会向 B 群真实发送一条测试消息，用于确认机器人权限、密钥和 `group_openid` 均可用。

### 5. 验证监听

首次建议保持 Dry-run 开启，先在监听群或联系人发送测试消息，确认日志和队列内容正确；确认无误后停止服务，关闭 Dry-run，再启动真实转发。

## Web UI 操作

Web UI 当前提供：

- 启动、停止、重启转发服务；
- Dry-run 模式切换；
- 运行前检查；
- 主动消息测试；
- B 群绑定；
- 添加和删除监听群/联系人；
- QQ 通知弹窗诊断；
- QQ 图片缓存诊断；
- 发送失败消息查看和选择性重试；
- 从 QQ 当前可见聊天记录中选择消息补发；
- 查看最近日志和 PID。

所有长耗时操作都有 loading 状态，并会在执行期间禁止重复点击。运行服务期间，Dry-run、监听会话、B 群绑定和补发相关操作会按需要锁定，避免修改正在使用的状态。

### 运行前检查的判定方式

| 分类 | 含义 | 是否阻止启动 |
| --- | --- | --- |
| 符合 | 配置或运行条件已满足 | 否 |
| 缺少或异常 | 必要条件不满足，例如真实发送模式缺少密钥 | 是 |
| 提示 | 不影响基础监听，但可能影响历史补读、图片处理或联网验证 | 否 |

运行前检查不会发送普通转发消息。检查机器人连接时只验证凭证和网关可用性；“发送主动测试”才会向 B 群发送实际消息。

### 消息状态

消息会在本地 SQLite 中按以下流程变化：

```text
监听到 → pending（待发送）
              ├─ 发送成功 → sent（已发送）
              └─ 达到最大尝试次数 → failed（发送失败）→ 手动重试
```

历史补发消息也会先进入 `pending`，需要启动真实发送模式后才会发送。Dry-run 下消息会保留在待发送队列，不会标记为已发送。

## 连续消息补读

Windows 通知可能在程序轮询前被后一条消息替换，例如先收到“你好”，随后收到“在吗”，通知层最终只剩“在吗”。项目会在通知到达后尝试打开对应 QQ 会话，读取当前可见的聊天记录，再与通知内容合并去重。

历史补读的使用方式：

1. 停止转发服务；
2. 在“从 QQ 窗口补发”选择群或联系人；
3. 点击“查看历史消息”；
4. 勾选确实漏发的消息；
5. 点击“将选中消息加入待发送”；
6. 启动转发服务发送。

QQ 主窗口可以最小化到任务栏，但不要完全退出到系统托盘。补读依赖 QQ UI Automation，不同 QQ 版本的界面暴露差异可能导致读取不完整。

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

## 故障排查

### 页面提示未读取机器人密钥

确认密钥是在启动 Web 控制面的同一个 PowerShell 窗口中设置的：

```powershell
echo $env:QQ_BOT_CLIENT_SECRET
```

如果刚设置过用户环境变量，已经运行的 Web 控制面不会自动刷新环境变量。请关闭旧 Web 控制面，在设置好环境变量的 PowerShell 中重新执行 `start.ps1`。

### 运行前检查提示 QQ 客户端不可用

文本通知仍可能正常监听，但以下功能会受影响：

- 连续消息历史补读；
- 图片自动复制；
- 从聊天窗口手动补发。

请确认 QQ NT 已登录，主窗口已经打开或最小化到任务栏，不要完全关闭到系统托盘。QQ 窗口标题或内部会话名称不需要固定，监听匹配使用 `listener_names`。

### 检查通知弹窗没有结果

1. 确认 Windows 已允许 QQ 发送通知；
2. 确认通知中显示的群名或联系人昵称与 `listener_names` 完全一致；
3. 发送一条新的 QQ 消息后再点击“检查通知弹窗”；
4. 执行以下命令查看原始诊断结果：

   ```powershell
   .\.venv\Scripts\python.exe -m app.main inspect-window --config config.toml
   ```

如果 UserNotificationListener 不可用，程序会尝试回退到 UI Automation 快速轮询。

### 机器人显示离线或主动测试失败

按以下顺序检查：

1. AppID 是否填写正确；
2. Client Secret 是否设置在当前 Web 控制面进程中；
3. `group_openid` 是否已经绑定到正确的 B 群；
4. 机器人是否已加入 B 群；
5. B 群是否开启机器人主动发言权限；
6. 先运行“运行前检查”，再点击“发送主动测试”。

主动测试是实际发送操作，不是 Dry-run 检查。

### 消息进入失败队列

停止转发服务后，打开“查看失败消息”，先查看每条消息的错误原因，再选择部分消息重试或点击“全部重新发送”。重试会把消息尝试次数清零，真实发送前请确认 B 群权限和机器人连接已经恢复。

### 连续消息仍然漏发

通知补读无法保证恢复 QQ 服务器上的全部历史消息。确认 QQ 主窗口没有退出到系统托盘，并在服务停止后使用“从 QQ 窗口补发”读取当前可见记录。补发操作应只选择确认漏发的消息，避免重复转发。

### 图片只能转发占位提示

这是当前实现的正常回退行为。程序会依次尝试 QQ 窗口复制和本地缓存匹配；如果原图无法取得，会发送带时间的 `[图片]` 占位提示。可使用“检查图片缓存”或以下命令确认缓存目录：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-image-cache --config config.toml
```

## 日志与数据

默认位置：

```text
data/forwarder.sqlite3
data/forwarder.log
data/image-cache/
```

日志会记录监听到的消息正文、来源会话、消息类型、发送结果和失败原因。数据库保存消息队列、去重状态、发送状态和失败记录。它们都可能包含敏感聊天内容，不要上传到公开仓库。

查看最近日志可以直接使用 Web UI，也可以在 PowerShell 中执行：

```powershell
Get-Content .\data\forwarder.log -Tail 100
```

## 开发与测试

安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

提交代码前建议执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check web\app.js
.\.venv\Scripts\python.exe -m compileall -q app tests
git diff --check
```

测试不需要真实 QQ 账号、机器人密钥或 B 群；涉及 Windows UI、通知和机器人 API 的行为仍需要在 Windows 实机上手动验证。

## 进程管理

查看项目相关进程：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match "windows-qq-group-forwarder" } |
  Select-Object ProcessId, ParentProcessId, Name, CommandLine
```

推荐优先在 Web UI 中停止服务。若需要停止整个进程树，可以在启动 Web 控制面的 PowerShell 窗口按 `Ctrl+C`，或使用页面的“停止”按钮。

如果页面显示“其他窗口启动”，说明另一个 Web 控制面或控制台已经持有转发服务锁。请找到原启动窗口并停止服务，不要重复启动多个 `app.main run` 实例。

## 命令行运行

通常推荐使用 Web UI。下面是直接运行和诊断命令：

### 启动 Web 控制面

```powershell
.\start.ps1
```

或：

```powershell
.\.venv\Scripts\python.exe -m app.main web --config config.toml
```

浏览器打开 <http://127.0.0.1:8765/>。Web 控制面会启动转发子进程，并在退出时清理它启动的进程树。按 `Ctrl+C` 会同时关闭 Web 控制面和转发服务；也可以只在页面点击“停止”。

### 直接启动转发服务

```powershell
.\.venv\Scripts\python.exe -m app.main run --config config.toml
```

直接运行时不会提供 Web UI。真实发送模式仍要求密钥已经存在于当前 PowerShell 会话；建议先在 Web UI 中执行运行前检查。

### Dry-run

```powershell
.\.venv\Scripts\python.exe -m app.main run --config config.toml --dry-run
```

Dry-run 只监听和入队，不向 B 群发送消息。Web UI 中修改运行模式后，需要停止转发服务再启动才会生效。

### 诊断命令

查看通知：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-window --config config.toml
```

查看图片缓存：

```powershell
.\.venv\Scripts\python.exe -m app.main inspect-image-cache --config config.toml
```

命令输出不会替代 Web UI 的运行前检查，但适合定位通知文本、监听会话名称和图片缓存目录问题。

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

## 未来优化方向

关于单实例多机器人、多实例管理、机器人和监听会话冲突校验、跨进程 QQ UI 锁以及系统资源评估，请参阅：

[docs/future-optimization-plan.md](docs/future-optimization-plan.md)

该文档是后续版本的规划依据。开始新版本开发时，应先确认当前代码是否仍符合其中的架构假设，再拆分实施任务和验收标准。
