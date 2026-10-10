# XVIS development

- Preserve the DSH-only, single-user topology and existing v1 compatibility.
- Implement the smallest change that meets the requested behavior. Freeze protocol changes before changing both ends.
- Never add local configuration, credentials, task data, signing material, hostnames, personal paths or device identifiers to Git.
- Run `python scripts/check.py` for Python or protocol changes. Run iOS tests for Swift changes; simulator execution requires `CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=YES`.
- Keep source tests, simulator checks, real-model checks and physical-device acceptance separate.
- Real DSH tests require explicit opt-in. Do not contact a production Hub or use real project directories as test fixtures.
- Defaults must not point to a maintainer service. An unavailable workspace must not silently fall back to another project.
- The app, model provider, Hub and Node have distinct data and credential boundaries; document changes to them.
