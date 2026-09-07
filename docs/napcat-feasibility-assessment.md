# NapCat 监听 QQ 消息 + QQ 官方机器人转发：可行性与风险评估

> 本文档用于评估下一种消息来源方案，不代表已经接入 NapCat，也不改变当前 Windows 通知监听实现。
>
> 评估日期：2026-09-07

## 1. 结论摘要

技术上可行，而且 NapCat 在消息完整性方面明显优于 Windows 通知栏监听：它可以直接提供群消息、私聊消息、发送人昵称、消息 ID 和消息段，图片也可以通过消息段中的 `file` / `url` 或 NapCat 的文件接口取得。

但它的核心代价是：需要在 QQ 客户端登录一个由 NapCat 接管的 QQ 账号。该方式不是 QQ 官方机器人 API，存在账号风控、登录限制、QQ 更新导致兼容性中断以及本地接口暴露等风险。

综合判断：

| 维度 | 结论 |
| --- | --- |
| Windows 开发可行性 | 高 |
| 文本、群聊、私聊监听能力 | 高 |
| 图片获取能力 | 高，但受资源 URL 有效期和 NapCat 缓存影响 |
| 与当前 QQ 官方机器人转发端结合 | 高 |
| 长期稳定性 | 中 |
| 主 QQ 账号安全性 | 高风险 |
| 推荐用途 | 低风险账号 POC、个人内网使用 |

如果只能使用主 QQ 号，不建议直接把 NapCat 作为长期生产方案。最稳妥的做法是：先用低价值账号完成验证，再决定是否迁移。

## 2. 目标架构

当前项目的目标链路是：

```text
NapCat 接管的 QQ 账号
        ↓ OneBot 11 HTTP / WebSocket 事件
Windows QQ Forwarder 消息源适配器
        ↓ 统一 IncomingMessage
本地 SQLite 队列
        ↓
QQ 官方机器人 API
        ↓
B 群
```

其中：

- NapCat 使用用户自己的 QQ 账号加入并监听 A 群或联系人；
- NapCat 只负责获取消息，不负责向 B 群转发；
- B 群仍然由 QQ 官方机器人发送，保留现有 `group_openid`、主动发言权限和发送重试逻辑；
- 当前 Windows 通知监听可以保留为兼容模式或故障回退模式。

## 3. NapCat 能提供什么

### 3.1 群消息和私聊消息

NapCat 的 OneBot 11 消息事件区分群聊和私聊，并提供以下关键字段：

- `message_id`：消息唯一标识，适合做去重；
- `group_id`：群的稳定标识，适合做群过滤；
- `user_id`：联系人或群成员 QQ 号，适合做联系人过滤；
- `sender.nickname`：发送人昵称；
- `sender.card`：群名片；
- `message`：消息段数组；
- `raw_message`：原始消息内容；
- `time`：消息事件时间戳。

这比当前依赖通知标题和聊天窗口文本的方式可靠。配置可以优先保存 `group_id` / `user_id`，名称只作为页面展示，避免群名或联系人备注变化导致监听失效。

### 3.2 图片和其他媒体

OneBot 消息段中的图片通常包含：

```json
{
  "type": "image",
  "data": {
    "file": "内部文件标识或本地文件",
    "url": "可选的图片 URL",
    "summary": "图片描述"
  }
}
```

可行的取得方式按优先级建议如下：

1. 使用事件中的 `url` 下载到本地暂存目录；
2. URL 不可用或已过期时，调用 `get_image` / `get_file`；
3. 对大文件使用 NapCat 的 Stream API；
4. 下载成功后复用当前 QQ 官方机器人图片上传发送逻辑。

NapCat 文档提示图片链接通常有约 2 小时有效期，文件资源依赖 LRU 缓存，不能把 `file` 标识当作永久文件地址。因此收到事件后应尽快下载，不能把下载动作延迟到人工补发时再执行。

## 4. 开发可行性

### 4.1 推荐通信方式

第一阶段推荐使用本机 WebSocket：

```text
NapCat OneBot WebSocket 服务端
        ↑ ws://127.0.0.1:<port>
项目中的 NapCatSource WebSocket 客户端
```

原因：

- 适合持续接收事件；
- 仅绑定 `127.0.0.1` 时不需要开放公网端口；
- Python 现有异步架构可以直接接入；
- 可在连接断开后自动重连；
- 不依赖 Windows 通知栏和 UI Automation。

HTTP 事件上报也可作为备选，但需要本项目启动 HTTP 接收端、处理重复上报和鉴权。对于当前单机 Windows 场景，WebSocket 客户端更简单。

### 4.2 需要新增的模块

建议新增：

```text
app/source/napcat_onebot.py
app/source/napcat_models.py       # 可选，用于事件和消息段类型
```

适配器职责：

1. 建立 WebSocket 连接并鉴权；
2. 接收 OneBot 事件并忽略心跳、系统通知等非消息事件；
3. 按配置的 `group_id` / `user_id` 过滤消息；
4. 将 text、image、at、reply 等消息段转换成统一消息模型；
5. 使用 `message_id` 生成幂等消息键；
6. 立即下载图片并写入现有暂存目录；
7. 记录连接、重连、解析和资源下载日志。

现有以下模块可以复用：

- `IncomingMessage`；
- `StateStore`；
- 失败队列和重试流程；
- `OfficialQqBotSender`；
- QQ 官方机器人主动测试；
- Web UI 的运行前检查和日志页面。

### 4.3 消息转换建议

群消息建议保存：

```text
source_type = napcat
source_id = group_id
source_name = 群名
sender = card 优先，否则 nickname
message_id = OneBot message_id
content = 文本段拼接结果
kind = text / image / mixed
media_path = 本地暂存图片路径
```

联系人消息建议保存：

```text
source_type = napcat
source_id = user_id
source_name = 联系人备注或昵称
sender = nickname
```

不建议只用群名或联系人名称做唯一标识。名称用于展示，数字 ID 用于过滤和去重。

## 5. 主要风险

### 5.1 QQ 账号风控风险：高

NapCat 官方安全文档明确提醒账号安全问题，包括避免 Bot 账号与常用账号在同一 IP/设备上同时登录、频繁掉线和社交风控等情况。NapCat 本质上依赖 NTQQ 客户端和协议侧接入，并非 QQ 官方机器人通道。

风险表现可能包括：

- 登录验证、设备验证或短信验证；
- 频繁掉线；
- 群消息能力受限；
- 临时限制或账号风控；
- QQ 客户端更新后无法启动或无法收消息；
- 主 QQ 号受到连带影响。

缓解措施：

- 优先使用低价值、专门用于监听的 QQ 号；
- 不要在主 QQ 号上直接做长期生产验证；
- 不要与大量自动化行为叠加；
- 固定 Windows 设备和网络环境，避免频繁切换；
- 将 NapCat 和转发器运行在本机回环地址，不暴露到公网。

### 5.2 QQ 更新兼容性：中高

NapCat 依赖 NTQQ 的内部行为，QQ 更新后可能出现：

- NapCat 无法注入或启动；
- OneBot 事件字段变化；
- 图片资源获取失败；
- WebUI 或通信端口配置变化。

缓解措施：

- 固定经过验证的 QQ 和 NapCat 版本；
- 禁止在未验证时自动更新 QQ；
- 在运行前检查中增加版本和连接检查；
- 保留当前 Windows 通知监听作为临时回退方案。

### 5.3 本地接口安全：中高

OneBot HTTP/WebSocket 接口可以读取消息，也可能执行发消息、撤回、群管理等操作。若端口监听在 `0.0.0.0` 或未配置 Token，局域网内其他设备甚至公网都可能调用接口。

最低安全要求：

- 只绑定 `127.0.0.1`；
- 启用 OneBot Token；
- 配置 WebSocket 连接超时和重连上限；
- 不把 NapCat WebUI 或 OneBot 端口映射到公网；
- 日志中禁止记录 Token、Cookie 和完整鉴权头；
- 在运行前检查中明确显示绑定地址和鉴权状态。

### 5.4 媒体资源时效性：中

图片 URL 会过期，NapCat 的文件标识也受 LRU 缓存影响。若先入队、后下载，可能在发送前已经无法取得原图。

建议：

- 收到图片事件后立即下载；
- 下载失败立即进入失败队列，不发送占位文本；
- 记录原始 `file` 标识、URL 是否存在、下载耗时和失败原因；
- 将原图复制到项目自己的 `data/image-cache` 后再排队发送。

### 5.5 隐私和合规风险：中高

NapCat 能读取 QQ 账号可见的群聊和联系人消息，范围远大于 Windows 通知栏。需要明确：

- 监听范围只包含用户明确配置的会话；
- 日志尽量不保存完整消息原文；
- 图片暂存目录设置访问权限和清理策略；
- 对涉及个人信息的消息遵循适用的法律法规和群规则；
- 不把消息发送到未授权的目标群。

## 6. 与当前方案对比

| 项目 | Windows 通知监听 | NapCat OneBot 监听 |
| --- | --- | --- |
| 是否需要接管 QQ 账号 | 否 | 是 |
| 账号风控 | 低 | 高 |
| 前台聊天窗口时是否容易漏消息 | 是 | 否，通常直接收到事件 |
| 群/联系人稳定识别 | 依赖名称和通知结构 | 可使用数字 ID |
| 发送人昵称 | 可能需要聊天记录补读 | 事件通常直接提供 |
| 图片原图 | 需要窗口复制或缓存猜测 | 通常可直接取得资源 |
| QQ 更新影响 | 主要影响通知/UIA | 可能直接影响 NapCat 接入 |
| 开发复杂度 | 已完成，但补偿逻辑复杂 | 中等，需要新增 OneBot 适配器 |
| 适合长期生产 | 受通知机制限制 | 受账号和版本风险限制 |

## 7. 推荐实施步骤

### 阶段 0：只读 POC

目标是不向 B 群发送任何消息：


- 安装并启动经过验证的 NapCat 版本；
- 配置本机 WebSocket 服务和 Token；
- 监听一个测试群和一个测试联系人；
- 记录脱敏后的事件类型、群/联系人 ID、message_id、sender.nickname；
- 分别测试文本、连续两条文本、图片、`@全体成员`、联系人消息；
- 观察断线重连和 QQ 重启后的恢复情况。

验收标准：连续发送 100 条文本不漏、不重；图片在事件到达后 2 秒内成功下载；重启 NapCat 后可以自动恢复连接。

### 阶段 1：接入统一消息队列

- 增加 `NapCatSource`；
- 配置监听会话的 `source_type`、`group_id`、`user_id` 和显示名称；
- 使用 OneBot `message_id` 去重；
- 复用当前 SQLite 队列；
- 保持 Dry-run 开关；
- 增加来源和连接状态日志。

### 阶段 2：接入图片下载

- 收到图片事件后立即下载；
- 先使用事件 URL，失败后调用 NapCat 文件接口；
- 下载失败直接标记失败；
- 复用 QQ 官方机器人图片上传发送；
- 图片发送成功后再清理暂存文件。

### 阶段 3：接入 Web UI 和运行前检查

增加以下配置和检查项：

- NapCat 监听开关；
- WebSocket 地址和端口；
- OneBot Token 是否配置；
- NapCat 连接是否成功；
- QQ 登录状态是否在线；
- 监听群/联系人 ID 是否存在；
- 官方机器人密钥和目标 `group_openid` 是否可用；
- NapCat 与官方机器人是否配置了不同的账号职责；
- 本地端口是否只绑定回环地址。

### 阶段 4：稳定性和回退

- NapCat 断线自动重连；
- 重连期间消息状态明确记录；
- 连接失败时 Web UI 显示“监听不可用”，而不是显示运行正常；
- 可选保留 Windows 通知监听作为回退来源；
- 两个来源必须使用统一去重键，避免同一消息重复转发。

## 8. 是否值得实施

如果主要目标是解决以下问题，NapCat 值得做 POC：

- QQ 前台时 Windows 通知不产生，导致消息漏监听；
- 多条消息快速到达时通知被覆盖；
- 图片原图无法从通知栏取得；
- 联系人昵称和群成员信息不完整；
- 需要使用群 ID 而不是容易变化的群名。

如果主要目标是“长期无人值守、主账号零风控风险”，NapCat 不满足这个前提，应该继续寻找 QQ 官方开放能力或接受通知监听的覆盖边界。

最终建议：在本分支先实施“只读 POC”，不要一开始接入真实转发和主 QQ 账号。POC 通过后，再决定是否实现 NapCat 消息源适配器；QQ 官方机器人转发端可以继续复用，不需要重写。

## 9. 参考资料

以下为评估时查阅的 NapCat 官方仓库和文档：

- [NapCatQQ GitHub](https://github.com/NapNeko/NapCatQQ)：项目定位、版本和账号安全提示。
- [NapCat 安装文档](https://napneko.github.io/guide/install)：Windows/NTQQ/NapCat/WebUI 的基本安装链路。
- [OneBot 11 事件基础结构](https://napneko.github.io/onebot/basic_event)：群消息、私聊消息、发送人和消息 ID 字段。
- [OneBot 11 事件系统](https://napneko.github.io/onebot/event)：消息事件和群聊事件模型。
- [OneBot 消息段类型](https://napneko.github.io/onebot/segment)：文本、图片、@ 等消息段格式。
- [NapCat 网络通信](https://napneko.github.io/onebot/network)：HTTP、WebSocket 和事件推送模式。
- [NapCat API](https://napneko.github.io/onebot/api)：`get_msg`、`get_image`、`get_file` 等接口。
- [NapCat 资源与消息 ID](https://napneko.github.io/onebot/napcat)：消息/文件 LRU 缓存和资源时效说明。
- [NapCat 文件处理](https://napneko.github.io/develop/file)：图片 URL 过期和 Stream API 说明。
- [NapCat 安全相关](https://napneko.github.io/other/security)：WebUI、OneBot 接口和 QQ 账号安全提示。

