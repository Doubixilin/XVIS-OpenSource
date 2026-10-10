# Privacy and data flow

You operate the Hub and execution Node and choose the model provider. XVIS does not ship a maintainer endpoint, shared API key, analytics account or hosted backend. A fresh App has no configured connection.

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
