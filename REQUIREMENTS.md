# Requirements review for mijn_ista_nl

## 1. Purpose
This document captures a review of the existing Home Assistant custom integration for `mijn.ista.nl`, identifies optimization opportunities, and proposes a set of requirements for future enhancements.

The community repository is `JoostHazelzet/mijn_ista_nl`, detached from the original
GitHub fork network with Git history preserved. Repository renaming does not change
the Home Assistant domain `mijn_ista` or Python package import names.

## 2. Current design summary
The integration follows a standard Home Assistant pattern:
- config entry setup in `custom_components/mijn_ista/__init__.py`
- a single coordinator in `custom_components/mijn_ista/coordinator.py`
- sensor entity generation in `custom_components/mijn_ista/sensor.py`

The coordinator authenticates to the external API, fetches user data, and normalizes per-property `Cuid` data into typed structures. Sensors are then generated for annual and monthly consumption metrics with Home Assistant units and state classes. Outdoor-temperature data from KNMI is not used or exposed.

### 2.1 Standalone API package (local testing stage)
An independently installable package now lives in
[`packages/mijn-ista-client`](packages/mijn-ista-client/README.md), imported as
`mijn_ista_client`. It extracts cookie handling, shared rate limits, login retries,
credential-rejection classification and month-shard polling from the integration's
client. It now depends on the locally maintained
[`mijn-ista-api-community`](packages/mijn-ista-api/README.md) core instead of the
abandoned author's published `mijn-ista-api` distribution, and on ordinary Python
HTTP libraries, not Home Assistant. Install both local package paths together.
The repository's uv development environment resolves both packages to editable
local sources, so existing notebook imports use the maintained API core.

The original API repository was imported under `packages/mijn-ista-api` using a
non-squashed Git subtree, preserving its complete history through
`7ef27e16d60124d0f7f96aae9d8ccf063cbaf932`. The upstream MIT declaration and original
author attribution are retained. The core's Python import namespace remains
`mijn_ista_api`; the original and maintained distributions must not coexist in one
environment. The protocol implementation is unchanged at this stage.

The package can be installed locally in editable mode and exercised from a Python
console, script or notebook. Its own offline test suite runs without loading the
integration or the repository's Home Assistant fixtures. The authentication
playground remains available for low-level investigation of the undocumented
protocol; the package is the place to test production-style client changes.

**Publication boundary:** the package is not yet published. Home Assistant wiring
is intentionally deferred: the integration retains its existing client and manifest
dependency, so normal installations continue to work. Package fixes affect standalone
tests only until explicitly backported or until the published dependency is adopted.
See FR-9 and NFR-6 for the required transition.

## 3. Observed optimization opportunities

### 3.1 Parallelize per-property API work
The coordinator fetches per property data sequentially:
- `get_consumption_averages(...)`
- `get_month_values(...)`
- parse and merge results

This is safe but not ideal when the account contains many properties or a large number of services. A future improvement should collect per-Cuid work in parallel where the upstream API allows it, while respecting rate limits and avoiding excessive concurrent requests. Because the data is refreshed only every few hours, and NFR-5 requires at least 5 s between data requests, parallel fetching gains little. This is deprioritised; data requests stay sequential, and parallelism may only overlap local parsing with waiting.

### 3.2 Reduce repeated dictionary and service lookups
In `sensor.py`, the sensor factory repeatedly resolves service metadata and building averages using multiple direct dictionary lookups and lambda closures. While this is readable, it creates repeated nested lookups during state and attribute generation. A future optimization should precompute a normalized service map and cache sensor metadata per service and per property.

### 3.3 Cache derived data for entity state attributes
The entity `native_value` and `extra_state_attributes` properties recompute attributes from coordinator data on every HA state read. This is acceptable for a small integration, but if the data set grows or entities are updated frequently, it may become inefficient. A future improvement should consider caching derived static metadata per entity or reducing per-read work in the attribute calculation path.

### 3.4 Add more robust partial-data handling
The coordinator already handles missing month values gracefully. The next phase should formalize the data contract for partial API responses and document exactly which sensors are unavailable or unknown under different outage scenarios, including partial authentication failures and incomplete billing periods.

### 3.5 Tighten refresh strategy around startup and rate limits
The coordinator intentionally uses a quick month fetch on first refresh, then full polling later. This is a solid optimization. The next requirement should be to document and test the startup behavior clearly, especially around the update interval and do-not-poll-faster-than-necessary guidance.

ista reports home energy consumption; the data is not time-critical. Refreshing every **6 hours** is sufficient (the current default is 24 hours). See FR-7 and NFR-5 for the refresh, login and request rate-limit policy.

### 3.6 Improve diagnostics and observability
The repo includes diagnostics support, which is useful. Future work should add clearer structured logging around API failures, skipped months, and per-Cuid processing. This would make support and debugging much easier for users with multiple properties or irregular API responses.

### 3.7 Authentication reliability
Authentication still fails intermittently despite the clean-cookie-jar workaround in `__init__.py` (aalaei/mijn-ista-api#1). Findings, reproduced with `notebooks/auth_playground.ipynb` against the live portal:

- **Dropped auth-cookie chunk (main cause).** After login, ASP.NET sets its session ticket as a chunked cookie: `.AspNetCore.Cookies` ("chunks-3") plus `C1`/`C2`/`C3` of about 4 kB, 4 kB and 0.8 kB. Since aiohttp 3.14.4, `CookieJar.filter_cookies` caps the `Cookie` request header at 8190 bytes and silently leaves out cookies that don't fit, here `C1`. ASP.NET can't rebuild the ticket, so `GET /Home` returns 401 without a CSRF token, and the library raises `MijnIstaAuthError("Session was not accepted after login")`. With aiohttp ≥ 3.14.4 (Home Assistant 2026.10 ships 3.14.4) this happens on every login. Sending all chunks (a jar with the limit disabled) gives `/Home` 200 and a working data session. This was confirmed live in the notebook (section 1, step 6).
- **Login rate limiting.** `login.ista.com` answers HTTP 403 after a few logins in quick succession; 10 s between attempts was too short. Each 403 surfaces as a connection error and leaves sensors unavailable until the next interval.
- **Shared cookie jar.** The integration uses Home Assistant's shared `aiohttp` session. The workaround clears all ista cookies, so overlapping logins (config flow plus running entry, or two accounts) wipe each other's in-progress login state.
- **Transient failures become reauth.** The library raises `MijnIstaAuthError` whenever the login form is re-rendered (also for "cookie not found" or "login timed out") and when `/Home` is not 200 after login (observed: 401 after a rate-limited login). The coordinator turns that into `ConfigEntryAuthFailed`, which stops polling.
- **No reauth flow.** The config flow has no `async_step_reauth`, so a `ConfigEntryAuthFailed` cannot be resolved through the standard reauth dialog.
- **Uncontrolled re-logins.** The library logs in on every coordinator refresh and again on any HTTP 401, including inside the 2 s month-shard polling loop.
- **Fixed timeout.** The coordinator's flat 100 s timeout does not cover login retries or month polling for several properties.

## 4. Functional requirements

### FR-1: Data refresh
The integration shall authenticate and refresh consumption data for each configured property using the external `mijn.ista.nl` API.

### FR-2: Multi-property support
The integration shall support accounts with multiple properties and create a distinct HA device grouping per `Cuid`.

### FR-3: Sensor generation
The integration shall generate sensors for annual totals, monthly totals, and building averages. It shall not retrieve or expose outdoor-temperature data.

### FR-4: Locale handling
The integration shall preserve Dutch service names in Dutch HA language settings and translate to English where appropriate when Home Assistant is configured in English.

### FR-5: Data resilience
The integration shall continue to operate when a subset of monthly or averaged data is missing and shall surface unavailable state values instead of failing the whole entry.

### FR-6: Diagnostics support
The integration shall expose enough diagnostic detail to help troubleshoot API issues without leaking sensitive credentials.

### FR-7: Refresh interval
- The default refresh interval shall be **6 hours** (currently 24 hours). Existing config entries keep their configured interval.
- After a failed refresh, the integration shall retry sooner than the regular interval (for example after 30 minutes) so data is not stale for a whole interval. It shall return to the regular interval after the next successful refresh. Retries remain subject to NFR-5.

### FR-8: Reauthentication
The config flow shall provide `async_step_reauth` so users can enter new credentials when the stored ones are genuinely rejected. Dutch and English strings shall be provided.

### FR-9: Standalone API development
- The API client shall be an independently installable Python package that can
  authenticate and fetch raw user, consumption, average and monthly payloads
  without importing, installing or running Home Assistant.
- Plain Python scripts and the authentication notebook shall be able to use the
  same standalone client. A documented console example shall prompt for credentials
  or read them from the environment, without embedding secrets in source code.
- HTTP sessions shall be private per client. The package shall support both
  caller-owned sessions and a context manager that closes its owned session on
  success, exceptions and cancellation.
- Cookie handling, login retries, error classification and request spacing shall
  retain the existing behavior. Shared rate-limit state shall be reusable across
  clients in one event loop; separate processes are not coordinated.
- After standalone regression testing and publication, the integration shall pin
  and import the released package as a dependency. This wiring is deferred until
  publication; no unpublished requirement or checkout-relative import shall be
  added to the Home Assistant runtime.

## 5. Non-functional requirements

### NFR-1: Performance
The integration shall avoid unnecessary blocking or repeated API calls and shall keep coordinator refreshes within a predictable time window for normal-sized accounts.

### NFR-2: Reliability
The integration shall recover from authentication and connection failures by raising the appropriate Home Assistant exceptions and preventing stale or misleading data from being treated as valid.

- Each config entry shall use its own `aiohttp` session and cookie jar (`async_create_clientsession`), closed on unload. The config flow shall use a temporary session of its own.
- Each login attempt shall start from an empty cookie jar.
- The cookie jar shall send every `.AspNetCore.Cookies*` chunk to `mijn.ista.nl`, regardless of aiohttp's `Cookie`-header size limit (aiohttp ≥ 3.14.4). A missing chunk shall be detectable in debug logs.
- A login shall be retried up to 3 attempts, subject to NFR-5.
- Only a confirmed credential rejection (two consecutive "invalid credentials" responses) shall raise `ConfigEntryAuthFailed`. All other login failures shall be treated as transient (`UpdateFailed`).

### NFR-3: Maintainability
Code should remain readable and testable, with raw API parsing isolated from entity logic and data model transformations kept explicit.

### NFR-4: Compatibility
Changes must remain compatible with current Home Assistant custom integration patterns and with existing config entry migration behavior.

### NFR-5: Rate limiting
ista publishes no rate limits for `mijn.ista.nl` or `login.ista.com`. The limits below are a conservative, self-imposed policy based on observed behaviour: `login.ista.com` returned HTTP 403 after a few logins within seconds. They shall be defined as constants so they can be tuned later.

**Logins (authentication):**
- A login retry shall start at least **120 seconds** after the previous attempt. More generally, no two login attempts shall be less than 120 seconds apart.
- This applies to every login path: config flow validation, entry setup, scheduled refreshes, retries, and library re-logins after HTTP 401.
- The limit applies across all `mijn_ista` config entries in the Home Assistant instance, since ista's rate limiting is not per entry.

**Data requests:**
- Data requests to `mijn.ista.nl/api/*` shall start at least **5 seconds** apart, across all `mijn_ista` config entries. This covers `UserValues`, `ConsumptionAverages`, `ConsumptionValues`, every `MonthValues` shard poll, and the library's internal retries after HTTP 503 or 401.
- The requests that make up a login are covered by the login limit and are not throttled individually.
- The library polls `MonthValues` shards every 2 s within a 15 s budget (quick mode) or 60 s budget (full mode). With a 5 s gap this allows only about 3 or 12 polls, so the month polling interval and budgets shall be adjusted to the 5 s gap. This shall be done in the integration's own client or in the library, and verified so that full history still loads.

**General:**
- Waiting for the login gap shall not block Home Assistant startup. If a login is needed while the gap has not elapsed during setup, the integration shall reuse a still-valid session or defer setup (for example with `ConfigEntryNotReady`) rather than sleep.
- The integration should minimise the number of logins. For example, it can reuse a still-valid session and log in only when ista rejects it. The session lifetime probe in `notebooks/auth_playground.ipynb` (section 4) checks whether a session survives a 6-hour interval.
- The coordinator's refresh timeout shall cover the login attempts including their waits, plus all data requests at the 5 s spacing (month polling per property), instead of a fixed 100 seconds.

### NFR-6: API isolation and adoption
- Standalone packaging and tests shall not depend on Home Assistant, its pytest
  plugin, config entries, `hass.data`, or application-managed session cleanup.
- The standalone package shall preserve upstream raw payload and exception
  contracts. The coordinator remains responsible for normalization into integration
  models; sensors remain responsible for units, localization and entity state.
- Home Assistant exception translation, language selection, shared-state storage
  and config-flow session handoff shall remain in an integration adapter.
- The standalone client shall have offline regressions covering realistic chunked
  cookies, rate limits, transient and genuine credential failures, full and partial
  month history, empty data, session ownership and forbidden Home Assistant imports.
- Adoption of a published release shall remove the duplicated integration client,
  update manifest/development dependencies and policy-constant imports together,
  and pass both standalone and integration tests without changing sensor behavior.
- Community maintenance shall preserve the original integration history and the
  imported API commit IDs, authorship and MIT attribution; GitHub fork-network
  detachment shall not use history rewriting or repository deletion.
- The standalone dependency graph shall resolve to the locally maintained API
  source and not install the abandoned `mijn-ista-api` distribution. The maintained
  core and resilient client shall both be published before runtime adoption.
- Detachment from GitHub's fork network is an owner-managed operation, separate
  from source imports. Its irreversible metadata loss must be acknowledged first.

## 6. Proposed acceptance criteria
- The integration loads successfully for accounts with one or more properties.
- Sensors update on the configured interval with no repeated full-history fetches unless required.
- Missing service data does not crash the setup or entity creation flow.
- Unit conversion and device classes match supported Home Assistant sensor semantics.
- Diagnostics output excludes passwords and other sensitive values.
- Login succeeds on aiohttp ≥ 3.14.4: all `.AspNetCore.Cookies*` chunks are sent and `/Home` returns 200 with a CSRF token.
- No two login attempts are less than 120 s apart, including config flow → setup, reloads, multiple entries and retries (NFR-5).
- No two data requests to `mijn.ista.nl/api/*` are less than 5 s apart, including month-shard polls and library-internal retries (NFR-5), and full month history still loads.
- A transient login failure (rate-limit 403, Keycloak "cookie not found", `/Home` 401) results in `UpdateFailed` and a sooner retry, never in a reauth prompt.
- Genuinely wrong credentials result in a reauth prompt that can be resolved through the UI (FR-8).
- Two config entries refreshing concurrently do not interfere with each other's login.
- Installing and importing `mijn_ista_client` in a clean Python environment works
  without Home Assistant; its example can fetch raw data without a HA restart.
- The standalone offline suite passes without importing Home Assistant or contacting
  ista. A wheel contains the client, policy constants and typing marker, but no
  credentials or notebook outputs.
- Until publication, the existing integration manifest and runtime remain unchanged.

## 7. Suggested future work backlog

### Cycle 1 — authentication reliability (first)
Status (v1.1.0): steps 1–8 are implemented in `custom_components/mijn_ista/api.py`, `coordinator.py`, `__init__.py` and `config_flow.py`, with tests in `tests/components/mijn_ista/test_resilient_api.py`. Still open: the upstream reports from step 2, and step 9.

1. ✅ Use a separate `aiohttp` session and cookie jar for each config entry, and a temporary one for the config flow.
2. ✅ Use a cookie jar that sends all session-cookie chunks, for example a `CookieJar` subclass with aiohttp's header limit disabled, passed to `async_create_clientsession(..., cookie_jar=...)`. Add a test that `filter_cookies` returns all four chunks for realistic sizes. Report the problem upstream to aio-libs/aiohttp and aalaei/mijn-ista-api. This is the most important fix. *(Upstream reports still open.)*
3. ✅ Add `api.py` with a `MijnIstaAPI` subclass that starts each login from an empty cookie jar, retries up to 3 attempts, and classifies failures. It replaces the cookie-clearing workaround in `__init__.py`.
4. ✅ Add a lock and timestamp shared by all `mijn_ista` entries that enforces the 120 s gap between logins. Add a second one that enforces the 5 s gap between data requests, for example as a session middleware so the library's internal polling and retries are covered too. Adjust the month polling interval and budgets accordingly (NFR-5).
5. ✅ Avoid the second login when an entry is set up right after the config flow, by reusing the flow's session or deferring setup.
6. ✅ Change the default refresh interval to 6 h, retry sooner after a failed refresh, and size the refresh timeout to fit the login waits (FR-7, NFR-5).
7. ✅ Add `async_step_reauth` (FR-8).
8. ✅ Add tests for the transient and genuine credential failure scenarios and for the 120 s and 5 s gaps.
9. ⏳ Run the session lifetime probe (notebook section 4). If a session survives 6 h, reuse it and log in only when ista returns 401.

### Later
0. Stabilize the standalone `mijn-ista-client` package using pure Python scripts and
   offline payload regressions, publish it, then replace the integration's duplicate
   client with a thin adapter and a pinned released dependency (FR-9, NFR-6).
1. Add a lightweight profiling pass to confirm refresh latency and identify hottest code paths.
2. Improve per-Cuid parallelization of independent API calls while enforcing safe concurrency limits.
3. Precompute service metadata maps and reduce repeated lookups in sensor generation.
4. Document expected behavior for partial month and averaging data.
5. Add more regression tests for multi-property and missing-data scenarios.

## 8. Recommendation
Use the standalone package to iterate on API bugs without Home Assistant restarts.
Once stable and published, adopt it through a thin integration adapter while
preserving coordinator normalization and sensor behavior. Further improvements
should remain targeted: data-contract hardening, observability and measured tuning.
