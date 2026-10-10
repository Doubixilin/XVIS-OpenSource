# Contributing

Start with a small reproducible issue or focused pull request. Describe the observable behavior, the change and how it was checked. Use synthetic data in examples, logs and screenshots.

Python requires 3.12 or newer. Install `requirements-lock.txt` into a virtual environment and run `python scripts/check.py`. The suite starts temporary loopback servers and child processes. iOS instructions are in [ios/README.md](ios/README.md).

Keep protocol additions compatible, retain request identifiers for retries, and test stale responses, cancellation and reconnects when changing execution control. Do not silently relax directory, certificate or authentication checks to make a test pass.

Do not commit generated runtime data, model credentials, signing profiles or private project context. Run `python scripts/check_public_tree.py` before submitting. Its checks are a useful guard, not a guarantee that arbitrary private content will be detected.

Please submit only contributions you have the right to license under the project's MIT license. Preserve notices for third-party code and identify its source. Contributions do not require a new dependency, service or UI framework unless the change needs it.
