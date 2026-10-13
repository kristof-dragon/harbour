# Stack versions and dependency updates

Release 0.1.32 dependency review: **13 October 2026**. Versions were checked
against Python.org, PyPI, npm, the official Docker image registry and ESnet.
Only stable releases were selected.

## Runtime and test versions

| Component | Previous pin | Selected stable version | Source |
| --- | --- | --- | --- |
| Python | Floating `3.12-slim` | **3.15.0**, Debian Trixie slim, image digest pinned | [Python release](https://www.python.org/downloads/release/python-3150/), [official image](https://hub.docker.com/_/python) |
| FastAPI | 0.135.2 | **0.143.0** | [PyPI](https://pypi.org/project/fastapi/0.143.0/) |
| Uvicorn | 0.42.0 | **0.54.0** | [PyPI](https://pypi.org/project/uvicorn/0.54.0/) |
| Paramiko | 4.0.0 | **5.0.0** | [PyPI](https://pypi.org/project/paramiko/5.0.0/) |
| Cryptography | 46.0.5 | **50.0.2** | [PyPI](https://pypi.org/project/cryptography/50.0.2/) |
| PyOTP | 2.9.0 | **2.10.0** | [PyPI](https://pypi.org/project/pyotp/2.10.0/) |
| QRCode | 8.2 | **8.2**, already current | [PyPI](https://pypi.org/project/qrcode/8.2/) |
| Pytest | 9.0.2 | **9.1.1** | [PyPI](https://pypi.org/project/pytest/9.1.1/) |
| HTTPX | 0.28.1 | **0.28.1**, already current | [PyPI](https://pypi.org/project/httpx/0.28.1/) |
| iperf3, both images | Unversioned distribution package | **3.22**, upstream source with SHA-256 verification | [ESnet releases and checksum](https://software.es.net/iperf/news.html) |
| Playwright, JavaScript tests | External developer environment | **1.64.0**, npm lockfile | [npm](https://www.npmjs.com/package/playwright/v/1.64.0) |

Python 3.15.0 became stable on 9 October 2026. The container uses the standard
interpreter, not an experimental JIT or a free-threaded variant. The chosen
official image digest is a multi-architecture index, so ARM64 and AMD64 select
their respective images rather than sharing an architecture-specific digest.

The generated [runtime lock](../requirements.txt) also fixes every resolved
indirect dependency and its permitted distribution hashes. This includes
Starlette 1.8.0, Pydantic 2.14.1, Pydantic Core 2.50.1, AnyIO 4.15.1 and CFFI
2.1.1. The [test lock](../requirements-dev.txt) uses the same runtime versions.
All 31 packages in the combined lock matched PyPI's current stable releases at
review time. Installation enforces hashes and checks dependency compatibility.

## Compatibility and deployment boundaries

- **Older SSH servers:** Paramiko 5 removes SHA-1 signatures/key exchanges and
  GSSAPI support. Servers that only negotiate those methods will no longer
  connect. RSA keys can still work with SHA-2 signatures; RSA key material is not
  itself prohibited. [Paramiko release notes](https://www.paramiko.org/changelog.html).
- **Python 3.15 and CFFI:** the input requirements retain a CFFI 2.1.1 minimum to
  avoid the ABI issue described by the Cryptography project. No unreleased
  Cryptography version is used. [Cryptography changelog](https://cryptography.io/en/latest/changelog/).
- **Traffic endpoint:** the two images build the same checksum-verified iperf3
  source using [the shared build script](../deploy/call-test-endpoint/build-iperf.sh).
  Only the executable enters the final image. Its optional OpenSSL/RSA
  authentication is disabled; traffic listeners require source-restricted
  firewall access. In 0.1.34 the separate HTTP log service adds token authentication;
  this does not authenticate iperf traffic. The endpoint container now also uses
  the same pinned Python base as Harbour. TCP and UDP 5201/5202
  remain the defaults. See [the endpoint deployment guide](CALL_TESTS.md).
- **Host tooling:** Docker Engine/Compose and the host Python that runs
  `scripts/start.py` are supplied by the administrator. This release does not
  install or upgrade them. The application and recorder run Python 3.15.0
  inside Docker. `.python-version` records that version for native development.
- **Monitored hosts:** their standard-library collectors retain the existing
  Python 3.9+ compatibility floor; OpenWrt collection does not require Python.
  Upgrading Harbour does not change any monitored host's packages or SSH setup.
- **Browser tooling:** `package-lock.json` fixes Playwright and Playwright Core.
  Node is only needed by developers running the browser checks, not by Harbour
  at runtime. Install the browser matching Playwright with `npx playwright
  install chromium`, or explicitly select an existing browser in the test
  environment where supported.

## Reproducing and refreshing dependencies

Regular deployment uses the committed locks; it does not resolve new versions:

```sh
git pull --ff-only
python3 scripts/start.py
```

Deploy or update the remote endpoint through **Call tests → Deploy remote
endpoint** in the WebUI. It includes the shared build script, service, pinned
image and protected configuration in `~/harbour-c-remote`.

For a dependency update, check official stable releases and edit
`requirements.in` / `requirements-dev.in`, then run:

```sh
sh scripts/lock_dependencies.sh
```

The script uses the exact Python image from the Dockerfile, with pip 26.2.1 and
pip-tools 7.6.2 in a disposable environment. It upgrades allowed indirect
dependencies and regenerates both hash locks. Direct exact pins only change
when their input files are edited. The locks target the deployment's Linux
Python 3.15 environment; do not assume they are a universal lock for every
Python version or operating system. Native developers should use Python 3.15
and validate their platform separately.

Browser test setup is `npm ci`; use an explicit new Playwright version and
regenerate the npm lock when upgrading. Recheck the official image digests in
both Dockerfiles and the iperf3 version/checksum in the shared script. Test the
new images, authentication/SSH, recorder processes and traffic results before
publishing. Pin updates, including security fixes, must be deliberately applied
and tested. [pip repeatable installations](https://pip.pypa.io/en/stable/topics/repeatable-installs/),
[Docker digest pinning](https://docs.docker.com/build/building/best-practices/#pin-base-image-versions).

The final base images and Python distributions are fixed, but build-stage
compiler/download packages still come from Debian's configured repositories.
This is not a claim of byte-for-byte reproducible compiler output or that host
Docker, the kernel and the surrounding network are pinned.

## Validation for 0.1.32

- Both production images built successfully on Linux ARM64. Harbour reports
  Python 3.15.0, pip 26.2.1 and iperf3 3.22; dependency checks pass.
- Full Python suite on the upgraded Linux stack, including the incident-brief
  release merged from GitHub: **471 passed, 1 skipped**.
  The skipped macOS launcher test passed separately on the development Mac's
  existing Python 3.11 environment. This is not a full native macOS 3.15 test.
- Chart/grid checks and all five browser suites passed with Playwright 1.64.0
  and the installed Chrome browser, including desktop/mobile OpenWrt controls.
- Production-container smoke checks passed for initial account creation,
  authentication, SSH key generation, two-factor authentication/recovery,
  permissions, persistent settings and restart with a read-only filesystem.
- Linux process integration passed for kernel receive timestamps, stalled
  writers/archives, restart, buffering, independent probes and orderly shutdown.
- An isolated ten-second simultaneous UDP test at 128/256 kbps returned ten
  receiver intervals in each direction, with no packet loss, using iperf3 3.22.
- Hash-verified binary wheels resolved for every runtime package on Linux
  AMD64 too. The full application was exercised on ARM64; AMD64 runtime and
  live-router/WAN validation were not performed.
- Both Compose configurations, shell syntax, local documentation links and
  the existing Python 3.9 source-syntax compatibility check passed.

The test suite still emits Starlette's deprecation warning for its HTTPX test
client adapter. HTTPX 0.28.1 remains its latest stable release and the tests pass;
switching the test adapter to HTTPX2 is outside this version-pin update.
