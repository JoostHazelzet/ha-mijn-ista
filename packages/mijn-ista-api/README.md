# mijn-ista-api-community

Async Python client for the [mijn.ista.nl](https://mijn.ista.nl) energy monitoring portal.

## Community maintenance and attribution

This is the community-maintained source of
[Ali Alaei's mijn-ista-api](https://github.com/aalaei/mijn-ista-api),
now maintained by Joost Hazelzet in this repository. The upstream project
declares MIT in its packaging metadata and README. The [MIT license](LICENSE)
and original authorship are retained.

The complete upstream history through commit
`7ef27e16d60124d0f7f96aae9d8ccf063cbaf932` was imported with a **non-squashed
Git subtree** at `packages/mijn-ista-api`. Original commit IDs, authors, timestamps
and messages remain reachable from this repository's history. This import is
independent of GitHub's fork-network relationship; it needs no submodule checkout
or access to the original repository at runtime.

The distribution has a distinct name, `mijn-ista-api-community`, while Python
imports remain `mijn_ista_api` for compatibility. Do not install it alongside
the original `mijn-ista-api` distribution in the same environment: both own the
same Python module. Use a fresh environment or uninstall the original first.
Version 0.2.0 retains the imported protocol implementation unchanged.
The [resilient client](../mijn-ista-client/README.md) adds the maintained cookie,
retry and throttle layer.

## Installation

This distribution is local and **not yet published**. From the repository root:

```bash
python -m pip install -e ./packages/mijn-ista-api
```

For offline protocol tests, install `./packages/mijn-ista-api[test]`, then run
`python -m pytest -q` from this package directory. The API tests previously
located under the integration tests now live here, without Home Assistant
fixtures. They cover login, CSRF headers, session expiry, server retries,
month shards and endpoint payloads.

## Requirements

- Python 3.12+
- aiohttp 3.9+

## Usage

```python
import asyncio
import aiohttp
from mijn_ista_api import MijnIstaAPI, MijnIstaAuthError, MijnIstaConnectionError

async def main():
    async with aiohttp.ClientSession() as session:
        api = MijnIstaAPI(session, "you@example.com", "your-password", lang="nl-NL")

        # Log in (OpenID Connect flow against login.ista.com)
        await api.authenticate()

        # Fetch account + annual comparison data
        user_data = await api.get_user_values()
        for cus in user_data.get("Cus", []):
            cuid = cus["Cuid"]
            print(cus.get("Adress"), cus.get("City"))

            # Full monthly history (polls until all shards are loaded)
            month_data = await api.get_month_values(cuid)

            # Building averages for the current billing year
            periods = cus.get("curConsumption", {}).get("BillingPeriods", [])
            if periods:
                p = sorted(periods, key=lambda x: x["y"], reverse=True)[0]
                avg_data = await api.get_consumption_averages(
                    cuid, p["s"][:10], p["e"][:10]
                )

asyncio.run(main())
```

## API overview

| Method | Endpoint | Description |
|---|---|---|
| `authenticate()` | OIDC login via `login.ista.com` | Establish an authenticated session |
| `get_user_values()` | `POST /api/Values/UserValues` | Account info, annual comparison |
| `get_month_values(cuid)` | `POST /api/Consumption/MonthValues` | Full monthly history (auto-polls shards) |
| `get_consumption_values(cuid, billing_period)` | `POST /api/Values/ConsumptionValues` | Meter totals for one billing year |
| `get_consumption_averages(cuid, start, end)` | `POST /api/Values/ConsumptionAverages` | Building-wide normalised averages |

## Error handling

```python
from mijn_ista_api import MijnIstaAuthError, MijnIstaConnectionError

try:
    await api.authenticate()
except MijnIstaAuthError:
    # Bad credentials
    ...
except MijnIstaConnectionError:
    # Network error or API unavailable
    ...
```

## Notes

- `authenticate()` drives a full OpenID Connect Authorization Code + PKCE login against ista's
  Keycloak instance (`login.ista.com`), the same flow the website itself uses. It establishes an
  ASP.NET Core session cookie (held by the `aiohttp.ClientSession` you provide) plus a CSRF token
  that every data call must include; there is no bearer token to manage yourself.
- Accounts with two-factor authentication enabled are not supported — the login form fill-in
  assumes a single username/password step.
- A 401 from any data endpoint triggers a full re-login (`authenticate()`) followed by one retry.
- `get_month_values` polls until the server has loaded all data shards (the API streams results).
- Transient `503 Service Unavailable` responses are retried with exponential backoff.

## Home Assistant integration

The original library powers the Home Assistant integration. The integration still
pins that original published distribution until the community packages are stable
and published. Local standalone development instead uses this maintained source.
See [REQUIREMENTS.md](../../REQUIREMENTS.md) for the adoption boundary.

## License

MIT
