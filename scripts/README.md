# Checks and tools

- `check.py`: public-tree guard, offline DSH parser fixtures and Hub/Node regression tests. Starts temporary loopback servers and synthetic child processes; no model call.
- `check_public_tree.py`: rejects obvious secret formats, personal home paths, private artifact paths and broken Markdown links. Does not certify absence of all sensitive content.
- `start_hub.py`, `start_node.py`: foreground services using your explicit private configuration.
- `pair_node.py`, `render_pairing_qr.swift`: short-lived device pairing, no automatic Node startup.
- `verify_dsh_headless.py`: default fixture-only mode. `--real` explicitly invokes the fixed model runtime.
- `verify_dsh_host.py`, `verify_h1_chain.py`, `verify_h2_chain.py`, `verify_h2_resilience.py`: opt-in integration probes; inspect `--help` and [validation](../docs/validation.md) first. Runtime locations refer to the documented isolated `local_private/dsh-validation/` layout.
- `install/`: optional macOS Node startup. It is never called by tests.

Use fresh test directories. Never point verification scripts at production data or arbitrary real projects. Tests should use synthetic credentials, not credentials inherited from your running deployment.
