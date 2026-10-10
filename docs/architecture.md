# 技术架构与取舍

[返回首页](../README.md) · [工作流程](workflows.md) · [接口索引](protocol.md) · [部署指南](deployment.md)

## 四层结构，围绕一份可恢复的任务记录

```mermaid
flowchart TB
    subgraph MOBILE["iPhone"]
        APP["SwiftUI App<br/>输入、阅读、批准与领取"]
        LOCAL["Keychain / 本地容器<br/>连接凭证、草稿与缓存"]
        APP --- LOCAL
    end
    subgraph SERVER["自托管 Hub"]
        API["FastAPI<br/>HTTPS / NDJSON / WSS"]
        DB["SQLite<br/>任务、事件、快照、交互、outbox"]
        BLOBS["私有附件目录<br/>图片与一般文件"]
        API --- DB
        API --- BLOBS
    end
    subgraph COMPUTER["执行设备"]
        NODE["Python Node<br/>执行适配与状态核对"]
        JOURNAL["本地执行日志"]
        DSH["固定 DSH 运行包<br/>独立配置与会话目录"]
        DIR["已登记工作目录"]
        NODE --- JOURNAL
        NODE --> DSH
        DSH --> DIR
    end
    PROVIDER["使用者配置的模型服务"]
    APP <-->|HTTPS / 前台 NDJSON| API
    NODE <-->|Node 主动建立 WSS| API
    DSH <-->|模型请求| PROVIDER
```

手机负责意图、阅读和决定；Hub 保存跨连接存在的任务事实；Node 把任务接到真实设备与目录；DSH 负责模型循环和工具执行。Hub 可以部署在使用者自己的 HTTPS 服务器上，Node 主动向外连接，无需把本机执行端口直接暴露到公网。

## 为什么采用这条路线

| 技术选择 | 对当前目标的帮助 | 约束 |
| --- | --- | --- |
| SwiftUI 原生 App | 使用系统照片、文件预览、分享、Keychain、动态字体与材质 | 构建、签名与设备验收需要 Apple 工具链 |
| FastAPI + SQLite | 对个人规模提供清晰的 HTTP 协议和持久化记录 | 当前部署使用单 Hub worker，不能直接当多实例集群运行 |
| Node 主动 WSS 连接 | 适应执行设备在内网、移动端随时离开的结构 | 需要重连、心跳、幂等与状态核对 |
| 固定 DSH 单引擎 | 集中验证会话、事件、取消和交互语义 | 升级运行包需要重新检查适配契约 |
| 独立运行目录与模型配置 | 不依赖开发者桌面环境的隐式状态 | 使用者需准备自己的运行包和模型访问方式 |
| 任务／附件／交互分别保存 | 执行、传输与人工决定能独立恢复和展示 | 状态数量增加，需要保持协议一致 |

项目没有再实现一套模型规划引擎。适配层负责设备、身份、协议和交付；复杂度只在当前工作链确实需要的地方增加。

## 两种 DSH 执行模式

| | headless | Host |
| --- | --- | --- |
| 当前定位 | 默认执行路径 | 显式启用的持续交互路径 |
| 工作方式 | 启动 DSH 进程并解析输出事件 | 通过 Host 协议管理会话与双向交互 |
| 能力 | 任务执行、状态、结果、会话续接 | 另有中途问答、一次批准、正文增量、steer 与 queue |
| 当前范围 | 已登记并可用的工作区 | 限定显式配置的 `p-xvis-test` |
| 配置隔离 | 独立 DSH home | 单独 `host_home` |
| 旧会话 | 按原模式续接 | 不把旧 headless 会话静默转换成 Host |

当前固定 DSH 包为 `0.2.1-alpha.1`、Node.js 为 `24.21.0`；上游源码验证基线为 `5badb15009ae1756c3afe0ae0cef1faafc290ccc`。Python 使用 3.12+；iOS 最低部署目标 17，新材质 API 需要 iOS 26+ 并保留旧系统路径。准确配置见 [部署指南](deployment.md)。

## 数据分别留在哪里

| 数据或身份 | 主要位置 | 边界 |
| --- | --- | --- |
| App 连接凭证 | iOS Keychain，按服务器绑定 | 不是模型 API Key |
| 草稿、阅读状态、下载缓存 | App 本地容器 | 不是服务端任务事实的替代品 |
| 任务、事件、交互、快照、通知队列 | Hub SQLite | 中枢可读取业务内容；没有端到端加密承诺 |
| 图片与文件成果 | Hub 私有附件目录与 App 领取缓存 | 需要鉴权；数据库与附件一起备份 |
| Node 身份 | 节点私有配置；Hub 保留凭证校验材料 | 每个节点独立，可撤销 |
| 执行日志与 DSH 会话 | 执行设备私有目录 | 重启时用于核对；不得把未知任务静默重跑 |
| 模型配置与凭证 | 执行端独立 DSH 环境 | 不通过 App 或 Hub 分发给模型服务 |
| APNs provider 证书／私钥 | 运营者配置的 Hub 私有位置 | 当前支持证书路径；真实送达另行验收 |

这是一套单个可信所有者使用的系统。多个 App 凭证不是独立租户账号，项目上下文和共享资料也没有多租户隔离设计。TLS 保护传输不等于中枢无法读取内容；自托管不等于本地推理。完整说明见 [隐私与数据流](privacy.md)。

## 关键一致性规则

1. **提交有稳定身份。** 同一次任务提交在回执丢失后可以查询／重试，不因网络重发创建重复工作。
2. **任务绑定设备和项目。** 使用 `(node_id, project_id)` 标识工作区。默认目录不可用时停止并报告，不自动换项目或节点。
3. **控制命令有独立回执。** Hub 接收、Node 转发和引擎实际消费分别确认。批准绑定运行实例、会话和调用，过期请求不会进入新实例。
4. **上下文快照固定。** 新根会话采用当时的共享资料与项目背景；续接和重试沿用原快照。项目资料保存使用版本检查，冲突时不丢弃编辑稿。
5. **交付独立于执行终态。** 文件需要类型、大小与 SHA-256 核验，缓存损坏可以补传。
6. **恢复承认未知。** 节点断线、进程退出与任务停止不能互相替代。Host 重启中断旧交互，避免把旧批准恢复到新运行中。
7. **移动端允许断开。** 前台增量通道失效时回退轮询；重开从服务端补齐。没有永久 iOS 后台监听承诺。

目录登记用于路由与配置检查，不能代替操作系统沙箱。工具能访问什么，仍受 DSH 工具实现、执行账户与系统权限影响。参见 [安全说明](../SECURITY.md)。

## 从代码哪里开始

| 关注点 | 入口 |
| --- | --- |
| App 状态、任务与恢复 | [AppStore.swift](../ios/XVIS/AppStore.swift)、[APIClient.swift](../ios/XVIS/APIClient.swift)、[Persistence.swift](../ios/XVIS/Persistence.swift) |
| 聊天、阅读与设计系统 | [ConversationViews.swift](../ios/XVIS/ConversationViews.swift)、[DesignSystem.swift](../ios/XVIS/DesignSystem.swift) |
| 问答、批准与持续控制 | [InteractionViews.swift](../ios/XVIS/InteractionViews.swift)、[interactions.py](../hub/gateway/interactions.py)、[continuous.py](../hub/gateway/continuous.py) |
| Hub HTTP 与持久化 | [app.py](../hub/gateway/app.py)、[v1_repo.py](../hub/gateway/v1_repo.py) |
| 图片与文件交付 | [images.py](../hub/gateway/images.py)、[artifacts.py](../hub/gateway/artifacts.py)、[FileViews.swift](../ios/XVIS/FileViews.swift) |
| 助理与项目资料 | [assistant_profile.py](../hub/gateway/assistant_profile.py)、[project_context.py](../hub/gateway/project_context.py) |
| 通知与未查看结果 | [notifications.py](../hub/gateway/notifications.py)、[inbox.py](../hub/gateway/inbox.py) |
| 设备配对与登记 | [pairing.py](../hub/gateway/pairing.py)、[node_registry.py](../hub/gateway/node_registry.py) |
| Node 执行适配 | [dsh_agent.py](../node/dsh_agent.py)、[dsh_runner.py](../node/dsh_runner.py)、[dsh_host.py](../node/dsh_host.py) |
| 执行日志与进程控制 | [dsh_store.py](../node/dsh_store.py)、[dsh_process.py](../node/dsh_process.py) |

## 部署和扩展时保持的边界

Hub 使用一个 worker 和持久私有数据目录；数据库及附件必须一致备份。Node 使用明确登记的目录与固定运行包。协议扩展先约定兼容行为，再更新各端，避免静默改动线上契约。

目前经过重点适配的是 macOS。两个隔离本地节点的配对验证不能替代第二物理设备、Windows 的进程树终止或平台权限验收。把摘要带到新设备创建任务，也不是迁移原来的 DSH 会话。后续工作见 [路线图](roadmap.md) 和 [验证边界](validation.md)。
