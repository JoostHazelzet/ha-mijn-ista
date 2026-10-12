# Requirements review for mijn_ista_nl

## 1. Purpose
This document captures a review of the existing Home Assistant custom integration for `mijn.ista.nl`, identifies optimization opportunities, and proposes a set of requirements for future enhancements.

The community repository is `JoostHazelzet/mijn_ista_nl`, detached from the original
GitHub fork network with Git history preserved. Repository renaming does not change
the Home Assistant domain `mijn_ista` or Python package import names.

### 1.1 Architecture rule: source-provided measurements and benchmarks

**Mandatory design rule:** Ista remains the source of truth for physical readings,
billing-period and current-month consumption, and source-provided benchmarks.
Users shall handle historical tracking and comparisons through the native
Home Assistant Energy Dashboard, Utility Meters, or Grafana with a
user-managed time-series backend.

This rule governs all future features and refactors, not only the current
implementation. The responsibility boundary is:

| Layer | Owns | Shall not own |
|---|---|---|
| Ista API | Authoritative measured values and source metadata | Integration-generated substitutes for measurements |
| Integration | Authentication, rate-limited acquisition, validation, normalization, and publication of API measurements and benchmarks with verified HA metadata | Historical storage/backfill or independently calculated consumption and estimates |
| Home Assistant / user-managed tools | Recorder history, Energy Dashboard statistics, Utility Meter cycles, Grafana visualization and comparisons | A requirement for this integration to duplicate their analytics |

In particular:

- The integration shall publish measured registers unchanged in meaning.
  A cumulative physical meter register is an atomic measurement and is
  permitted; calculating a counter from period consumption is not.
- Normalization shall preserve measurement meaning and precision. Formatting
  source reading dates as `YYYY-MM-DD`, validating numeric values, and mapping
  verified units and device classes are permitted. Missing data shall not
  become invented readings or estimates.
- The integration shall preserve existing physical meter sensors and publish
  service totals, calendar-month consumption and building averages, normalized
  heating, monthly outdoor temperature, and the source update timestamp.
- These metrics shall come from UserValues, ConsumptionValues, MonthValues,
  and ConsumptionAverages. The integration shall not synthesize readings or
  assume the first month or billing period in a response is current.
- Recorder history and historical backfill remain outside the integration.
  Optional endpoint failures shall be logged and shall not hide valid registers.

This establishes the **Single Source of Truth** philosophy applied by this
project to Home Assistant: Ista remains authoritative for measurements, while
Home Assistant or the user's chosen backend owns recorded history and derived
analytics. It aligns with modern Home Assistant integration and microservice
design through separation of concerns, single responsibility, and avoiding
duplicate statistics engines or competing consumption histories.

## 2. Current design summary
The integration follows a standard Home Assistant pattern:
- config entry setup in `custom_components/mijn_ista/__init__.py`
- a single coordinator in `custom_components/mijn_ista/coordinator.py`
- sensor entity generation in `custom_components/mijn_ista/sensor.py`

The coordinator normalizes all four API datasets into per-property `Cuid`
structures. Sensors publish physical readings and source-provided consumption,
benchmarks, monthly temperature, and update timestamps. Home Assistant owns
recorded history; no API history is backfilled.

### 2.1 Standalone API package (local testing stage)
An independently installable package now lives in
[`packages/mijn-ista-client`](packages/mijn-ista-client/README.md), imported as
`mijn_ista_client`. It extracts cookie handling, shared rate limits, login retries,
credential-rejection classification and month-shard polling from the integration's
client. The same distribution now owns the low-level protocol in
`mijn_ista_client.api` and the resilient wrapper in `mijn_ista_client.client`.
It depends only on ordinary Python HTTP libraries, not Home Assistant or
another Ista distribution. The repository's uv environment installs this single
package in editable mode. Notebooks, examples, and standalone tests share the
root `.venv`; Home Assistant test dependencies are an optional `integration`
group in that same environment.

The original API repository was imported under `packages/mijn-ista-api` using a
non-squashed Git subtree, preserving its complete history through
`7ef27e16d60124d0f7f96aae9d8ccf063cbaf932`. The upstream MIT declaration and original
author attribution are retained in the consolidated package. Moving the source
does not rewrite the imported history. Local imports now use `mijn_ista_client`;
the original `mijn_ista_api` namespace remains exclusive to the integration's
published dependency, so the two can coexist during transition. The protocol
implementation is unchanged at this stage.

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

### 3.1 Minimize API work
One `UserValues` request supplies all properties and current meter readings.
Each property gets one monthly-shard operation and, when an active billing period
exists, one ConsumptionValues and one ConsumptionAverages operation per refresh.
All requests use the existing shared throttle; startup uses quick shard polling.

### 3.2 Normalize raw registers once
The coordinator owns payload validation and date normalization. Sensors use
direct service-and-meter lookups rather than recomputing derived values.

### 3.3 Keep attributes atomic
Raw meter attributes retain identity/type and reading date. Service totals expose
begin/end meter values; monthly sensors expose month and approximation status.
There are no unbounded history lists.

### 3.4 Handle partial readings explicitly
Missing or invalid `EndValue` values must be unavailable, not zero. Missing
meters are retained as unavailable entities and newly discovered meters are
added on subsequent refreshes. Malformed response structures fail the refresh
explicitly.

### 3.5 Preserve startup and rate-limit policy
The first refresh uses one login attempt without waiting for the login gate;
later refreshes retain the established retry policy. Timeout budgets cover
the data request and its possible reauthentication and retry.

ista reports home energy consumption; the data is not time-critical. Refreshing every **6 hours** is sufficient (the current default is 24 hours). See FR-7 and NFR-5 for the refresh, login and request rate-limit policy.

### 3.6 Improve diagnostics and observability
Diagnostics report raw meter counts and unclassified meter counts. Logging
identifies invalid readings and unverified register metadata without dumping
raw payloads.

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
The integration shall authenticate and refresh measured values and benchmarks
for each configured property using the external `mijn.ista.nl` API, within
the architecture boundary defined in section 1.1.

### FR-2: Multi-property support
The integration shall support accounts with multiple properties and create a distinct HA device grouping per `Cuid`.

### FR-3: Sensor generation
The integration shall generate one sensor per physical meter using the raw
`EndValue` register. In addition, it shall publish source-provided service totals,
calendar-month consumption and building averages, normalized heating, outdoor
average temperature, and the source `ed` update timestamp.
Verified ultego energy and domaqua water registers shall use their physical
units, appropriate device classes, and `total_increasing`. Unverified meter
families shall remain raw, without inferred units or statistics metadata.
New unique IDs shall prevent mixing register statistics with old period totals;
only explicitly retired entity families shall be removed from the registry.

### FR-4: Locale handling
The integration shall preserve Dutch service names in Dutch HA language settings and translate to English where appropriate when Home Assistant is configured in English.

### FR-5: Data resilience
The integration shall tolerate missing individual readings, dates, and service
metadata without substituting zero or publishing invented measurements.
Invalid numeric readings shall be logged and unavailable; invalid dates shall
be logged and omitted. Structural UserValues errors shall raise `UpdateFailed`.
Optional dataset failures shall be logged and leave affected metrics unavailable,
while UserValues totals and registers remain usable.

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
- MonthValues shard polling shall use the same shared request throttle.

**General:**
- Waiting for the login gap shall not block Home Assistant startup. If a login is needed while the gap has not elapsed during setup, the integration shall reuse a still-valid session or defer setup (for example with `ConfigEntryNotReady`) rather than sleep.
- The integration should minimise the number of logins. For example, it can reuse a still-valid session and log in only when ista rejects it. The session lifetime probe in `notebooks/auth_playground.ipynb` (section 4) checks whether a session survives a 6-hour interval.
- The coordinator's refresh timeout shall cover login attempts including waits,
  the single `UserValues` request, and its possible HTTP 401 reauthentication
  and data retry. Optional per-property operations shall have separate bounded
  budgets covering shard polling and potential reauthentication.

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
- The standalone dependency graph shall resolve to a single locally maintained
  `mijn-ista-client` distribution containing both protocol and resilience layers,
  and shall not install the abandoned `mijn-ista-api` distribution. Only the
  consolidated client requires publication before runtime adoption. Integration
  tests retain the old dependency until that adoption, without namespace overlap.
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
