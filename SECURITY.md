# Security

XVIS is a developer preview for one trusted owner. An App credential can submit tasks to authorized nodes and read their results. A compromised credential or model-driven tool invocation can affect the registered machine and files. Directory registration does not replace the operating system or DSH sandbox.

- Expose the Hub through HTTPS; keep its HTTP listener on loopback. Nodes require WSS except for explicit loopback testing.
- Use separate random App and Node credentials. Keep local credential files readable only by their owner.
- Run nodes as a non-administrator user with access only to the intended work. Use an independent DSH home; never reuse a desktop session directory.
- Keep backups and logs private. Task text, tools, images and generated files can contain sensitive material.
- Revoking a paired node prevents further authenticated connections; it does not prove that an already-running external process has stopped.
- Configure APNs only after matching the app's signing environment and notification topic. Model/API credentials stay on the execution node.

Do not post working credentials, private task data or an exploitable live endpoint in public issues. If GitHub private vulnerability reporting is available on the published repository, use **Security → Report a vulnerability**. Otherwise open a minimal issue requesting a private reporting channel without the exploit details. No response-time guarantee is offered.

This repository has not received an independent security audit and is not intended as a multi-tenant public execution service.
