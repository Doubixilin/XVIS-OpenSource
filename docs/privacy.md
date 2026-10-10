# Privacy and data flow

You operate the Hub and execution Node and configure supported model access. XVIS does not ship a maintainer endpoint, shared API key, analytics account or hosted backend. A fresh App has no configured connection. Execution currently uses pinned DSH; Host explicitly selects `deepseek-official / deepseek-flash`. Provider independence is a design goal, not a claim of arbitrary model compatibility.

## 数据自持与本地存储

XVIS 起于作者希望在模型账号或云端应用不可用后仍能保有自己的资料。“自持”指使用者掌握存储与管理权；当前 Hub 可以部署在云服务器，不能据此声称所有资料均已位于本地电脑。完整本地档案、通用导出导入和自动备份恢复仍在计划中，见 [产品思路](product.md) 和 [路线图](roadmap.md)。

持久记录、手机缓存、原始档案与精选记忆具有不同范围。删除设置中的资料不等于删除所有历史副本，保存了当前缓存也不等于已完成备份。模型服务失效不会自动删除自有存储中的记录，但资料能否持续访问仍取决于自己的存储、备份和恢复安排。

| Data | Where it is stored or sent |
|---|---|
| App access credential | iOS Keychain, bound to the configured endpoint; sent only for authenticated Hub requests |
| Instructions, task state and results | App cache, Hub SQLite/events, Node journal and DSH session history |
| Images and returned files | App cache, authenticated Hub storage and execution workspace as needed |
| Persona, manual memory and project context | Hub records and immutable task snapshots; relevant text is sent to DSH/model execution |
| Model credential | Execution Node environment / independent DSH configuration; not entered into the App |
| Push device token | Hub notification registration; enabling APNs adds Apple as a notification delivery service |

Instructions and relevant files may be sent to the configured model provider. Self-hosting the Hub does not make remote model inference local. Consult the provider's policies separately.

The App prepares selected input photos as JPEG, normalizes orientation and strips source metadata. That does not redact private content visible in the image. Text, tool output and arbitrary returned files are not automatically anonymized.

Removing a memory entry affects future snapshots after saving; previous tasks, DSH histories and backups can retain old content. Clearing the App connection removes local connection/cache records but does not erase Hub or Node history. A complete data-erasure workflow is not implemented. Attachment cache cleanup and retention policies require operator management.

APNs payloads use identifiers and generic status descriptions rather than task content or tool arguments. Notifications are optional; refusing permission leaves ordinary tasks and results available. Local logs and screenshots can still contain sensitive data and should not be published as bug reports without review.
