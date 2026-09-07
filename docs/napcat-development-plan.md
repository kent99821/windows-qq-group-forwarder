# NapCat 消息源接入开发方案

> 本方案基于 [NapCat 可行性与风险评估](./napcat-feasibility-assessment.md) 制定。
>
> 目标是在 Windows 环境中，用 NapCat/OneBot 11 直接获取 QQ 群聊和联系人消息，再复用当前 QQ 官方机器人发送端转发到目标群。
>
> 当前文档只定义方案，不代表 NapCat 已经接入主程序。

## 1. 开发目标

### 1.1 必须实现

- 使用 NapCat OneBot 11 WebSocket 接收 QQ 群消息和联系人消息；
- 使用群 `group_id`、联系人 `user_id` 进行稳定过滤，不依赖群名或联系人备注；
- 获取发送人昵称和群名片，正确生成转发消息格式；
- 支持文本、图片、`@`、回复等常见消息段；
- 使用 OneBot `message_id` 去重，避免通知和重连导致重复转发；
- 图片收到后立即下载到项目暂存目录；
- 图片下载失败直接进入失败队列，不发送占位文本；
- 继续复用当前 QQ 官方机器人 API、目标 `group_openid`、发送重试和 Web UI；
- 支持 Dry-run，只接收和入队，不向目标群发送；
- NapCat 断线后自动重连，并在 Web UI 和日志中显示连接状态。

### 1.2 暂不实现

- 不使用 NapCat 账号向目标群发送消息；
- 不实现 NapCat 群管理、撤回、加好友等与转发无关的 API；
- 不在第一阶段实现多个 NapCat QQ 账号；
- 不立即删除 Windows 通知监听代码；
- 不把 NapCat WebUI 或 OneBot 接口暴露到公网；
- 不把 NapCat 消息历史当作可靠数据库，历史补读仍依赖收到事件时立即处理。

## 2. 总体架构

```text
NapCat + NTQQ（监听账号）
          │
          │ OneBot 11 WebSocket 事件
          ▼
NapCatOneBotSource
          │ 过滤、解析、图片下载、生成稳定消息键
          ▼
统一 IncomingMessage
          ▼
StateStore（SQLite pending/failed/sent）
          ▼
OfficialQqBotSender
          │ QQ 官方机器人 API
          ▼
目标 QQ 群
```

当前 Windows 通知监听保留为另一种消息源：

```text
WindowsNotificationSource ─┐
NapCatOneBotSource         ├─> 统一消息队列 ─> QQ 官方机器人发送端
                            ┘
```

第一版不建议同时开启两个来源监听同一会话。若以后支持 `hybrid` 模式，两个来源必须共享统一去重规则。

## 3. 分阶段实施方案

## 阶段 0：只读 POC

### 目标

验证 NapCat 在当前 Windows 机器、当前 QQ 版本和测试账号上的真实事件质量，不接入真实转发。

### 实施内容

新增独立的 POC 程序或测试命令：

```text
python -m app.napcat_poc --config config.napcat-poc.toml
```

POC 只做以下事情：

1. 连接本机 NapCat WebSocket；
2. 输出脱敏后的事件摘要；
3. 保存消息段类型和图片资源可用性；
4. 统计收到、解析、过滤、重复、下载成功和下载失败数量；
5. 不调用 QQ 官方机器人发送接口。

### POC 测试矩阵

| 场景 | 验证内容 |
| --- | --- |
| 单条文本 | 正文、发送人、时间、群 ID |
| 连续 100 条文本 | 是否漏消息、重复消息 |
| 两条相同文本 | 是否按 message_id 保留两条 |
| 群图片 | 是否有 image 段、file/url 是否可下载 |
| 连续两张图片 | 图片与消息 ID 是否一一对应 |
| 联系人文本 | user_id、nickname 是否正确 |
| 联系人图片 | 图片资源是否可取得 |
| `@全体成员` | 是否能按消息段准确识别并过滤 |
| 回复消息 | reply 段是否保留或转为文本 |
| QQ/NapCat 重启 | 是否能自动重连 |
| WebSocket 断开 | 是否重连且不重复发送 |

### POC 通过标准

- 连续 100 条文本全部收到，允许顺序有延迟但不允许漏或重复；
- 两条相同正文按两个不同 `message_id` 保留；
- 图片事件收到后立即下载，成功率达到测试样本的 95% 以上；
- 断线后 30 秒内恢复连接；
- 重连不会重复处理已持久化的消息；
- POC 运行期间不产生官方机器人发送请求。

如果 POC 无法通过，不进入后续真实转发开发。

## 阶段 1：消息源适配器

### 4.1 新增模块

建议新增：

```text
app/source/napcat_onebot.py
app/source/napcat_protocol.py       # 事件、消息段和 API 响应解析
tests/test_napcat_onebot.py
tests/fixtures/napcat/*.json
```

如果协议数据结构保持简单，也可以将 `napcat_protocol.py` 合并到适配器中，但解析函数应保持独立、可单测。

### 4.2 适配器接口

当前通知读取器是同步 `poll()` 模型，NapCat 是异步 WebSocket 模型。建议抽象一个异步消息源接口：

```python
class MessageSource(Protocol):
    async def run(self, output: asyncio.Queue[list[IncomingMessage]], stop_event: asyncio.Event) -> None: ...
    async def close(self) -> None: ...
```

现有 Windows 通知读取器可以用兼容包装器接入，不要在 NapCat 适配器中模拟 200ms 轮询。

NapCat 适配器职责：

1. 连接 `ws://127.0.0.1:<port>`；
2. 在连接握手时发送 Token；
3. 接收并解析 JSON；
4. 仅处理 `post_type == "message"`；
5. 忽略 `message_sent`，避免机器人自身事件形成回路；
6. 按配置过滤群或联系人；
7. 解析消息段并下载媒体；
8. 将统一消息放入现有队列；
9. 处理心跳、超时、断线和重连。

### 4.3 稳定消息键

建议使用：

```text
napcat:{self_id}:{message_type}:{message_id}
```

如果 NapCat 在特定版本中可能重复使用消息 ID，则补充：

```text
napcat:{self_id}:{message_type}:{source_id}:{message_id}
```

`StateStore.message_key` 继续作为 SQLite 主键。不要使用正文、发送时间或随机 UUID 作为 NapCat 消息的唯一去重键。

### 4.4 群和联系人过滤

建议配置模型：

```toml
[[source.sessions]]
type = "group"
id = "987654321"
name = "发家致富"

[[source.sessions]]
type = "private"
id = "123456789"
name = "家欣"
```

运行时建立两个集合：

```python
configured_group_ids: set[str]
configured_user_ids: set[str]
```

匹配规则：

- 群消息只比较 `group_id`；
- 私聊只比较 `user_id`；
- `name` 只用于 Web UI 展示和人工确认；
- 不从通知标题、群名片或消息正文推断目标会话；
- 配置只填写名称而没有 ID 时，NapCat 模式应在运行前检查中判定为缺少配置。

## 阶段 2：消息段与图片处理

### 5.1 文本消息

消息段按原顺序转换：

| OneBot 段 | 转换方式 |
| --- | --- |
| `text` | 直接拼接文本 |
| `at` | 转为 `@昵称` 或 `@全体成员` |
| `face` | 转为可读占位文本 |
| `reply` | 转为引用消息提示，或按配置忽略 |
| `json` / `xml` | 转为摘要文本，禁止直接注入 HTML |
| 未知段 | 日志记录类型，正文使用安全占位文本 |

`@全体成员` 的过滤应基于消息段类型和 `qq` 值，而不是只依赖最终拼接文本。这样可以准确过滤单独的全体提醒，同时保留“@全体成员 请参加会议”这类有实际内容的消息。

### 5.2 图片处理顺序

```text
收到 image 段
   ↓
立即尝试 url 下载
   ↓失败
调用 get_image / get_file
   ↓失败
记录 failed，不进入 pending
   ↓成功
复制到 data/image-cache
   ↓
生成 kind=image 的 IncomingMessage
   ↓
官方机器人图片上传和发送
```

要求：

- 图片下载和消息入队应尽量在同一处理流程中完成；
- 不把 NapCat 的临时 `file` 标识直接交给延迟发送任务；
- 下载文件必须校验真实文件存在、文件大小和图片格式；
- 下载失败原因需区分 URL 过期、NapCat API 失败、网络失败、文件不可读；
- 暂存文件名使用稳定消息键的哈希，不能使用用户可控原文件名；
- 只有所有目标发送成功后才删除暂存文件；
- 失败消息不发送“无法取得原图”的普通文本。

### 5.3 大文件

第一版只支持项目当前 QQ 官方机器人接口允许的图片大小。超过阈值时：

- 记录失败原因和文件大小；
- 不在内存中一次性读取整个文件；
- 后续再接入 NapCat Stream API 和分块传输。

## 阶段 3：统一运行主流程

### 6.1 配置兼容策略

保留当前配置可运行，新增显式消息源配置：

```toml
[source]
backend = "windows_notification" # windows_notification | napcat

[napcat]
enabled = false
ws_url = "ws://127.0.0.1:3001"
token_env = "NAPCAT_ONEBOT_TOKEN"
connect_timeout_seconds = 10.0
heartbeat_timeout_seconds = 30.0
reconnect_min_seconds = 1.0
reconnect_max_seconds = 30.0
download_timeout_seconds = 20.0
```

NapCat 会话配置可先兼容现有 `listener_names`，但正式启用 NapCat 时必须要求数字 ID：

```toml
[[source.sessions]]
type = "group"
id = "987654321"
name = "发家致富"
```

迁移规则：

1. `backend` 缺失时默认为 `windows_notification`；
2. 旧的 `listener_names` 继续供 Windows 通知模式使用；
3. NapCat 模式不自动把名称转换成 ID；
4. Web UI 保存 NapCat 会话时同时保存 ID 和名称；
5. 切换 `backend` 必须停止服务后生效。

### 6.2 主流程调整

将当前 `run()` 中硬编码的通知采集任务改为来源工厂：

```python
source = create_message_source(config)
source_task = asyncio.create_task(source.run(notification_queue, stop_event))
```

队列、图片处理器、发送器和失败状态继续复用。目标是让以下两种来源拥有相同的后续链路：

```text
source.run()
  → notification_queue
  → route_notification_batches()
  → process_image_batches()
  → process_pending()
```

NapCat 模式不再调用 QQ 聊天窗口历史补读和窗口复制图片；这些是 Windows 通知模式的补偿机制。

### 6.3 连接状态

运行状态至少包括：

```text
source_backend
source_status: starting / connected / reconnecting / stopped / error
last_event_at
last_event_type
reconnect_count
last_error
```

Web UI 中“服务运行中”不能等同于“NapCat 已连接”。NapCat 未连接时应显示警告，并禁止误以为消息正在正常监听。

## 阶段 4：运行前检查和 Web UI

### 7.1 运行前检查项目

NapCat 模式至少检查：

| 检查项 | 合格条件 |
| --- | --- |
| NapCat 开关 | 配置明确启用或明确关闭 |
| WebSocket 地址 | 仅允许 `127.0.0.1` 或用户明确确认的内网地址 |
| 端口 | 地址可连接且端口未被其他服务错误占用 |
| Token | 环境变量存在，但日志不显示密钥内容 |
| NapCat 登录状态 | `get_login_info` 成功并显示在线 |
| 监听群 | `group_id` 非空且可通过 `get_group_info` 查询 |
| 监听联系人 | `user_id` 非空且格式正确 |
| 官方机器人 | AppID、密钥、`group_openid` 检查通过 |
| 发送模式 | Dry-run 与真实发送状态明确 |
| 目录权限 | 图片暂存目录可读写 |
| 版本 | NapCat/QQ 版本记录并处于已验证组合 |

检查结果分为：

- 通过：可以启动；
- 警告：可以启动，但明确展示风险；
- 缺失/失败：禁止启动，给出修复建议。

### 7.2 Web UI 交互

新增或调整：

- 消息源选择：Windows 通知 / NapCat OneBot；
- NapCat 地址、Token 环境变量、重连参数；
- 群 ID/联系人 ID 和显示名称的增删改；
- NapCat 连接测试；
- NapCat 登录信息测试；
- 只读 POC 测试入口；
- 连接状态、最近事件时间、重连次数；
- 运行中锁定消息源和连接参数；
- 真实发送前必须通过运行前检查。

## 8. 测试方案

### 8.1 单元测试

覆盖：

- 群/私聊事件识别；
- 群 ID 和联系人 ID 过滤；
- 发送人 `card` 优先于 `nickname`；
- `message_id` 生成稳定键；
- OneBot 消息段拼接；
- 单独 `@全体成员` 过滤；
- 带正文的 `@全体成员` 保留；
- 图片段 URL/file 提取；
- 错误事件和未知消息段不导致进程退出；
- Token 不出现在日志和异常字符串中。

### 8.2 集成测试

新增本地 Mock OneBot WebSocket 服务，模拟：

- 连接成功和鉴权失败；
- 心跳；
- 单条和批量消息；
- 相同正文不同 message_id；
- 重复推送相同 message_id；
- 图片 URL 下载成功/过期；
- `get_image` 回退成功/失败；
- 连接断开和重连。

集成测试禁止调用真实 QQ 官方机器人 API，发送端使用 FakeSender。

### 8.3 Windows 人工验收

必须在真实 Windows 环境验证：

1. QQ 前台打开 A 群时仍能收到消息；
2. QQ 最小化时仍能收到消息；
3. 连续快速发送 100 条消息不漏；
4. 图片、联系人消息格式正确；
5. NapCat 重启后自动恢复；
6. 官方机器人只发送到配置的目标群；
7. 停止服务后不再接收和发送；
8. Dry-run 不产生真实发送请求。

## 9. 安全和运维要求

- NapCat OneBot 只监听 `127.0.0.1`；
- 必须使用 Token 鉴权；
- 不记录完整事件原文、Cookie、Token 或鉴权请求头；
- NapCat 监听账号与官方机器人账号职责分离；
- 固定 QQ/NapCat 版本，升级前先在 Dry-run 验证；
- 只使用低价值测试账号进行 POC；
- 图片缓存目录采用最小访问权限并定期清理；
- 记录 NapCat 版本、QQ 版本、项目版本和启动时间；
- 断线重连不能无限快速重试，采用指数退避并设置上限；
- 账号出现验证、掉线或风控时停止自动化，不进行激进重试。

## 10. 开发任务拆分

建议按以下顺序拆分提交：

1. `NapCat` 配置模型和运行前检查，不改变默认 Windows 模式；
2. OneBot 事件/消息段解析函数与 JSON fixture；
3. 本地 Mock WebSocket 和 POC 命令；
4. `NapCatOneBotSource` 连接、鉴权、心跳和重连；
5. ID 过滤、昵称提取和稳定去重键；
6. 图片下载、暂存和失败队列处理；
7. 消息源工厂和主流程接入；
8. Web UI 配置、连接测试和状态展示；
9. Windows 人工验收和文档更新；
10. 通过验收后再开放真实发送模式。

每一步都应保持：

- 现有 Windows 通知模式可运行；
- 全套自动化测试通过；
- 不提交真实 Token、QQ 账号信息或本地缓存；
- 能够通过切换 `backend` 回退到 Windows 通知模式。

## 11. 里程碑和放行条件

### M1：POC 完成

- NapCat WebSocket 可连接；
- 群聊、联系人、文本、图片事件可解析；
- 100 条连续文本不漏不重；
- 不调用官方机器人发送接口。

### M2：Dry-run 消息链路完成

- NapCat 消息进入 SQLite；
- 过滤、去重、图片下载和失败状态正确；
- Web UI 能看到来源和连接状态；
- 所有测试通过。

### M3：测试群真实转发

- 通过运行前检查；
- 使用低价值 NapCat 账号；
- 目标为测试群；
- 文本和图片连续运行至少 24 小时；
- 无重复转发、无占位文本误发。

### M4：有限生产使用

只有在 M3 满足以下条件后才能考虑：

- NapCat/QQ 版本组合已固定；
- 断线、重连、重启、图片过期均有明确处理；
- 账号无异常验证或风控提示；
- 已准备一键回退到 Windows 通知模式的操作步骤；
- 用户明确接受 NapCat 账号风险。

## 12. 最终建议

优先实施阶段 0 和阶段 1，不要直接改造当前真实转发流程。原因是 NapCat 的主要不确定性不在 Python WebSocket 编程，而在当前 Windows、QQ、NapCat 版本组合下的事件完整性、图片资源可用性和账号稳定性。

推荐的实施顺序是：

```text
只读 POC
  → Dry-run 入队
  → 图片下载和失败处理
  → Mock 集成测试
  → 测试群真实转发
  → 有限运行
```

如果阶段 0 能稳定收到消息，NapCat 方案可以解决当前 Windows 通知监听的主要问题：前台窗口漏消息、通知覆盖、多消息漏发、图片原图难以取得以及名称过滤不稳定。QQ 账号风控风险仍然存在，不能通过代码完全消除。

