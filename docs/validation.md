# Validation and limitations

The repository distinguishes synthetic tests, real model integration, simulator interaction and physical-device behavior. A successful build is not physical-device acceptance, and a model's success message is not verification of its output.

## Reproducible local checks

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python scripts/check.py
```

This checks repository hygiene, parser fixtures and the Hub/Node suite with temporary directories, synthetic credentials, loopback sockets and fake child processes. Tests must not read your deployment config or invoke real DSH. Some process-tree tests use POSIX tools and are not Windows certification.

iOS tests use injected network transports and isolated temporary storage. See [ios/README.md](../ios/README.md). Fresh-install checks cover an empty server address and no network-client creation before configuration. CI runs Python tests and simulator XCTest without deployment credentials.

## Opt-in real DSH checks

`verify_dsh_headless.py --real`, `verify_dsh_host.py --real` and the H1/H2 chain probes invoke a real model and can consume credits. They require the pinned runtime, your own model configuration and fresh ignored test directories. Read each script's help before use. The default runtime layout is `local_private/dsh-validation/` with `node-runtime/bin/node` and `runtime/node_modules/@deepseek-ai/dsh/lib/bin.js`; Host probe also accepts explicit runtime paths.

Upstream integration has previously been exercised for question/approval/resume, rejection, cancellation, image/file return, streaming, steer/queue and reconnection. That is not a claim that a fresh operator's credentials, proxy, permissions or model will work without validation.

## Remaining boundaries

- Real Apple APNs delivery needs an operator certificate/key, correct signing and a physical-device test. Payload injection only checks app routing.
- Pairing via links and isolated local nodes does not prove camera scanning or a second physical machine's permissions and tools.
- Windows Node operation is not validated by this macOS-focused adapter.
- iOS background time is limited. The Node can continue work, while the App refreshes on reopening; permanent background listening/streaming is not promised.
- Host is gated to an explicit test workspace; restart interrupts work safely rather than restoring stale approvals.
- Memory is user-maintained and snapshots are retained in old tasks. Automatic extraction, full erasure and cross-device session migration are not implemented.
- General file **output** is implemented; arbitrary input documents, automatic voice conversations and system share extension are roadmap items.

## Verified source snapshot — 2026-10-10

- A fresh Python 3.12.10 virtual environment installed all 22 locked distributions; `pip check` passed.
- `scripts/check.py` passed: 22 offline parser fixture checks and 212 Hub/Node regression test invocations. Default test discovery contains no real-model cases; `verify_dsh_chain.py --real` remains an explicit opt-in.
- Signed simulator XCTest passed **95/95** on an iPhone simulator running iOS 27. The ordinary app was installed and launched without a debugger; the welcome screen showed an unconfigured connection and no prefilled live server.
- The 32-file Hub release was extracted into an independent temporary directory. Manifest hashes, import location, startup, health and authenticated/unauthenticated requests passed. Archive ownership metadata contains no builder account name.
- Public file-path/content checks, local documentation links, shell syntax and Xcode property-list checks passed. These checks are not an independent security audit.

This source snapshot has not been deployed to a production Hub or installed on a physical iPhone, and it did not repeat paid real-DSH integration or real APNs delivery tests.

## Hosted CI — 2026-10-10

[GitHub Actions run 38022818930](https://github.com/Doubixilin/XVIS-OpenSource/actions/runs/38022818930) passed for source commit `a316d5aff87f1a7a77e745afe88ffb8bfea32141`:

- Ubuntu: public-tree checks, offline parser fixtures and all **212** Hub/Node regression test invocations passed.
- macOS with Xcode 26.6: signed iOS simulator XCTest passed **95/95**, with zero failures.

The follow-up documentation commit records this result without changing the tested source or workflow.

## Documentation and App gallery — 2026-10-10

The expanded [README](../README.md), [product explanation](product.md), [design notes](design.md), [architecture](architecture.md), [workflows](workflows.md), [progress](progress.md), [roadmap](roadmap.md), [FAQ](faq.md) and [references](references.md) document the existing source and distinguish it from planned work.

- The [gallery](gallery.md) adds **11 original simulator screenshots**, including both appearances, Markdown reading, history, approval, queue, file delivery/preview, assistant settings and project context. The App uses the same tested business source, with a loopback synthetic demonstration service; no paid model or production Hub is used.
- Local Markdown links and HTML image targets were checked. All **10 Mermaid diagrams** passed syntax parsing with Mermaid 11.12.0.
- New screenshot metadata contains color-space and dimension information, with no location, account or device identifier fields. Visible content was reviewed as synthetic demonstration data. This is a bounded publication check, not a general privacy or security audit.
- Public-tree checks and Git whitespace checks passed. This update changes documentation and screenshots only, so the business and iOS suites were not repeated; the source CI evidence above remains the applicable baseline.

The gallery demonstrates the actual App UI, not real DSH execution of the displayed tasks. It does not establish physical-device, camera, cellular, haptic or APNs acceptance.

## Origin and decoupling clarification — 2026-10-10

The introduction now records the author's account-loss experience and the original goal of keeping core records independent of a model account or cloud application. Product, architecture, progress, roadmap, FAQ, privacy and deployment notes distinguish current self-managed storage from planned portable archives, local copies, model replacement and complete backup/recovery.

Current implementation boundaries were checked against the Hub task store, Node journal, pinned-runtime configuration, Host model selection and Linux installer's database-backup/rollback path. The new product diagram depicts a design goal, not shipped migration functionality. The documentation set now contains 11 Mermaid diagrams.

This is a documentation-only update. Local links, public-tree/whitespace checks and Mermaid syntax validation were repeated; business and iOS suites were not rerun. No model provider, backup service, deployment or execution mode was changed, and no full disaster-recovery acceptance is claimed.
