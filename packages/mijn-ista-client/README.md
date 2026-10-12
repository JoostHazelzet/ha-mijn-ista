# mijn-ista-client

An independently installable async Python client for the Dutch
`mijn.ista.nl` portal. No Home Assistant imports, installation, config entries,
or Home Assistant-managed event loop are required. One distribution contains
both the low-level protocol (`mijn_ista_client.api`) and the resilient transport
(`mijn_ista_client.client`). It has no dependency on either `mijn-ista-api` or
`mijn-ista-api-community`. The imported authentication protocol is preserved.

## Install and run

From the repository root, with Python 3.13 or newer:

```bash
uv sync --locked
uv run python packages/mijn-ista-client/examples/read_consumption.py
```

The example prompts for credentials (the password is hidden), or reads
`ISTA_USERNAME` and `ISTA_PASSWORD` from the environment. It prints private
account data: do not commit or share its output. It makes live requests; the
tests below do not. No credentials or notebook outputs are packaged.

The package is unpublished. The root uv project installs it in editable mode
into the repository's `.venv`, also used for notebooks, examples, and tests.
No package-specific virtual environment is needed. Select the root `.venv`
kernel in Jupyter and restart existing kernels after migrating imports.
Older local imports should change from `mijn_ista_api` to `mijn_ista_client`,
and from `mijn_ista_api.client` to `mijn_ista_client.api`.

Outside this repository, installation requires only one path:

```bash
python -m pip install -e './packages/mijn-ista-client[test]'
```

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
uv run --locked pytest -q
uv run --locked ruff check packages/mijn-ista-client
```

Full lint currently reports four inherited `UP041`/`BLE001` warnings in
`api.py`; consolidation deliberately preserves the imported protocol behavior.

These run both protocol and resilience tests without loading the integration's
Home Assistant fixtures. The default repository test selection is standalone.
Home Assistant tests explicitly use the integration dependency group in the
same root environment (Python 3.13.2 or newer):

```bash
uv run --locked --group integration pytest -q tests
```

For a distributable wheel, from the repository root:

```bash
uv build packages/mijn-ista-client --wheel
```

## Community maintenance and attribution

The protocol originates from [Ali Alaei's mijn-ista-api](https://github.com/aalaei/mijn-ista-api).
Its complete history through `7ef27e16d60124d0f7f96aae9d8ccf063cbaf932` was
imported by a non-squashed Git subtree, originally at `packages/mijn-ista-api`.
Consolidation moves the current source into this package without rewriting
that history. Original commit IDs, authors, and timestamps remain reachable.
The [MIT license](LICENSE) and original author attribution are retained in
the source and packaged wheel.

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

Only `mijn-ista-client` needs publication; its distribution name has not been
reserved on PyPI. The optional integration test group still installs the
original published API to match the unchanged manifest. Its `mijn_ista_api`
namespace does not overlap this package's `mijn_ista_client` namespace.
