# Third-party software and references

The XVIS Python and Swift source is licensed under the root MIT license. Dependencies are installed separately; this repository does not redistribute a DSH, Node.js or Python runtime, dependency wheelhouse, Apple SDK, or model weights. Their licenses are not replaced by the XVIS license.

## Runtime dependencies

| Component | Version used by this integration | License / source |
|---|---|---|
| DeepSeek Harness | npm `@deepseek-ai/dsh@0.2.1-alpha.1`; source `5badb15009ae1756c3afe0ae0cef1faafc290ccc` | [MIT](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/LICENSE); retain its own third-party notices when redistributing a runtime |
| Node.js | 24.21.0 | [Node license and bundled notices](https://github.com/nodejs/node/blob/v24.21.0/LICENSE) |
| FastAPI | 0.142.2 | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn | 0.54.0 | [BSD-3-Clause](https://github.com/Kludex/uvicorn/blob/main/LICENSE.md) |
| Pydantic | 2.13.5 | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| HTTPX | 0.28.1 | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) |
| websockets | 17.2 | [BSD-3-Clause](https://github.com/python-websockets/websockets/blob/main/LICENSE) |
| PyYAML | 6.0.3 | [MIT](https://github.com/yaml/pyyaml/blob/main/LICENSE) |

The resolved Python dependency versions are recorded in `requirements-lock.txt`, with their installed license metadata in [dependencies.json](docs/dependencies.json). Installed distributions contain their own license metadata and notices, including transitive packages such as certifi (MPL-2.0). If distributing a prebuilt server, wheel bundle or executable, include the actual installed packages' licenses and fulfill their respective redistribution requirements. The source-only archive is not a license bundle for a future binary distribution.

The iOS app uses Apple frameworks and system symbols through the SDK; there are no third-party Swift package dependencies. Apple SDKs, signing credentials and system assets are not included.

## Design and integration references

The [DeepSeek Harness mobile project](https://github.com/yukiykchen/deepseek-harness-mobile) and [scan-remote plugin](https://github.com/yukiykchen/dsh-scan-remote) informed the mobile interaction and pairing research. XVIS implements its own SwiftUI app and Node–Hub protocol against the pinned official DSH package. These upstream projects are not vendored or presented as XVIS code. No article text, PDF, third-party screenshot or model-generated private result is included.

Brand asset licensing is documented in [assets.md](docs/assets.md).
