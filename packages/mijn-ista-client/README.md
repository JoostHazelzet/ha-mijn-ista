# mijn-ista-client

An independently installable async Python client for the Dutch
`mijn.ista.nl` portal. No Home Assistant imports, installation, config entries,
or Home Assistant-managed event loop are required. This package extracts the
integration's resilient client and uses the
[locally maintained API core](../mijn-ista-api/README.md),
`mijn-ista-api-community==0.2.0`. It no longer depends on the original published
`mijn-ista-api` distribution. The imported authentication protocol is preserved.

## Install and run

From the repository root, with Python 3.13 or newer:

```bash
python -m venv .venv-api
source .venv-api/bin/activate
python -m pip install -e './packages/mijn-ista-api[test]' -e './packages/mijn-ista-client[test]'
python packages/mijn-ista-client/examples/read_consumption.py
```

The example prompts for credentials (the password is hidden), or reads
`ISTA_USERNAME` and `ISTA_PASSWORD` from the environment. It prints private
account data: do not commit or share its output. It makes live requests; the
tests below do not. No credentials or notebook outputs are packaged.

Both packages are unpublished, so install both local paths together. The API
core keeps the `mijn_ista_api` import namespace: do not co-install the original
`mijn-ista-api` distribution. If reusing an older development environment,
uninstall that distribution first and reinstall both local packages.
The repository's `uv sync` development environment also resolves both packages
from these local sources, so the existing notebook's `mijn_ista_api` imports use
the maintained core without notebook changes.

## Python console or script

```python
import asyncio
import getpass
from mijn_ista_client import MijnIstaShared, open_client

username = input("ista email: ")
password = getpass.getpass("ista password: ")


async def read():
    shared = MijnIstaShared()
    async with open_client(username, password, shared=shared, lang="nl-NL") as api:
        await api.authenticate()
        return await api.get_user_values()


data = asyncio.run(read())
```

In Jupyter, use `data = await read()` instead of `asyncio.run(read())`.
The [authentication playground](../../notebooks/auth_playground.ipynb) remains
the low-level protocol investigation tool; import this package there when testing
the production-style resilience layer instead of redefining it.

`open_client` closes its private aiohttp session on success, exceptions, and
cancellation. It does not log in automatically. The inherited
`get_user_values()`, `get_consumption_averages(cuid, start, end)` and
`get_consumption_values(cuid, billing_period)`, plus
`get_month_values(cuid, quick=False)`,
retain the upstream raw dictionary contracts. Use a `Cuid` from the user payload
for property calls. Full month polling may return partial data when its budget
expires; quick mode returns as soon as monthly data arrives. Home Assistant
models, normalization, sensor units and translation remain in the coordinator
and sensor layer, not in this transport package.

Reuse one `MijnIstaShared` across clients/accounts **in the same event loop**:
login starts are at least 120 seconds apart, data request starts at least
5 seconds apart, and credential-rejection confirmations survive client changes.
The default state is local to each `open_client` context. Independent processes
and separate contexts without shared state do not coordinate rate limits.
Avoid rapid restarts or repeated live logins.

For application-owned sessions, use `create_session(shared)` and
`ResilientMijnIstaAPI(session, username, password, shared=shared)`, and close the
session yourself. A custom session must use `IstaCookieJar` and
`throttle_middleware(shared.request_throttle)` to retain cookie and rate-limit
guarantees. `login_attempts`, `login_max_wait`, and `invalid_confirmations` remain
adjustable on the client. A gate exceeding `login_max_wait` raises
`MijnIstaRateLimited` with `retry_after`; transient failures raise
`MijnIstaConnectionError`, confirmed rejected credentials raise
`MijnIstaAuthError`. No errors are translated to Home Assistant exceptions.

## Offline tests and builds

```bash
cd packages/mijn-ista-client
python -m pytest -q
cd ../mijn-ista-api
python -m pytest -q
```

This package's tests have their own pytest root and do not load the repository's
Home Assistant fixtures. For distributable wheels, from this package directory:

```bash
python -m pip wheel --no-deps . --wheel-dir dist
python -m pip wheel --no-deps ../mijn-ista-api --wheel-dir dist
```

## Integration transition

This is a local, unpublished package. The integration still uses its existing
client and `mijn-ista-api==0.2.0` manifest requirement, so HACS/manual installations
do not depend on a local checkout or an unpublished PyPI version. Changes here
do **not** change Home Assistant behavior yet.

After standalone testing and publication, pin the released `mijn-ista-client`
version in the integration manifest and import the resilient client, policy
constants, cookie jar and middleware from this package. Keep Home Assistant
session creation/cleanup, shared-state storage, config-flow handoff, language
selection, coordinator normalization and exception translation in the integration.
Remove the old duplicated client implementation in that transition and run both
test suites. Until then, backport any urgent integration fixes explicitly.

Publish the maintained core as `mijn-ista-api-community` as well, so installing
the resilient client never pulls the abandoned distribution. These proposed
distribution names have not been reserved on PyPI.
