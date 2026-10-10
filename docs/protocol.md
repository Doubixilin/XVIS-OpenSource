# Protocol reference

The current API is v1. Paths below are relative to the configured Hub base URL, which may have a reverse-proxy prefix such as `/xvis/`. JSON errors have `{"error":{"code":"...","message":"..."}}`. Unknown fields and invalid identifiers must not be treated as successful operations.

The App uses its Bearer credential. Static and paired Node credentials have their own role and scope. `/health` is public; pairing request creation/status use short-lived secrets with separate validation and limits. The remaining endpoints require their appropriate authenticated role. Pairing is not an App-login mechanism.

| Area | Main routes | Implementation / schema |
|---|---|---|
| Devices and projects | `GET api/v1/nodes`, `GET api/v1/projects` | `hub/gateway/app.py` |
| Tasks | `POST/GET api/v1/tasks`, `GET api/v1/tasks/{task_id}`, `POST .../followup`, `POST .../cancel` | `shared/v1_schemas.py`, `hub/gateway/v1_repo.py` |
| Events | `GET api/v1/tasks/{task_id}/events`, `GET .../stream` | `hub/gateway/app.py`, `continuous.py` |
| Questions / approval | `GET api/v1/interactions`, `POST api/v1/tasks/{task_id}/interactions/{interaction_id}/respond` | `shared/interaction_contract.py` |
| Steer / queue | `POST api/v1/tasks/{task_id}/controls` | `shared/control_contract.py` |
| Persona / manual memory | `GET/PUT api/v1/assistant-profile` | `shared/v1_schemas.py`, `assistant_profile.py` |
| Project context | Project-scoped context, sources and summary routes | `hub/gateway/project_context.py` |
| Images / output files | Authenticated image upload/download and task file manifest/content/retry | `images.py`, `artifacts.py`, `shared/file_contract.py` |
| Results / notifications | `api/v1/inbox`, notification settings and device registration | `inbox.py`, `notifications.py` |
| Pairing / revocation | `api/v1/pairing/...`, `api/v1/paired-nodes` | `hub/gateway/pairing.py` |

The Node connects outward to `ws/node`. Hello capabilities, task assignments, acknowledgements, event cursors, control results and reconnect reconciliation are implemented in `hub/gateway/app.py` and `node/dsh_agent.py`.

## Compatibility rules

- A client request ID identifies one immutable submission. A lost response is retried with that same request and content, not a new task.
- Task status, final result, command receipt and actual command consumption are different facts. A transport-level success cannot stand in for a confirmed tool response.
- Interaction replies include their original task, interaction and execution epoch. Stale, handled, cancelled or expired requests cannot authorize a new operation.
- Queue modifications use exact revisions. Unknown receipts must be reconciled before retrying a potentially different operation.
- Streaming is newline-delimited JSON with replay cursors and reconnects. It carries assistant text, not reasoning. Final task state remains authoritative.
- Persona and project context are snapshotted when a new conversation begins. Follow-up and retry preserve that snapshot. Saving generated summaries requires an explicit user action and revision match.
- Pairing expires after five minutes and uses separate scan, poll and Node secrets; the Hub stores hashes. App approval displays device identity/fingerprint. Revocation does not claim to have killed an unreachable process.

Exact field limits and response shapes are encoded in the referenced validators. [Offline tests](validation.md) cover their compatibility behavior. Freeze new additions and add contract fixtures before changing Node, Hub and App together.
