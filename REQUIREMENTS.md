# Requirements review for ha-mijn-ista

## 1. Purpose
This document captures a review of the existing Home Assistant custom integration for `mijn.ista.nl`, identifies optimization opportunities, and proposes a set of requirements for future enhancements.

## 2. Current design summary
The integration follows a standard Home Assistant pattern:
- config entry setup in `custom_components/mijn_ista/__init__.py`
- a single coordinator in `custom_components/mijn_ista/coordinator.py`
- sensor entity generation in `custom_components/mijn_ista/sensor.py`

The coordinator authenticates to the external API, fetches user data, and normalizes per-property `Cuid` data into typed structures. Sensors are then generated for annual, monthly, and temperature-related metrics with Home Assistant units and state classes.

## 3. Observed optimization opportunities

### 3.1 Parallelize per-property API work
The coordinator fetches per property data sequentially:
- `get_consumption_averages(...)`
- `get_month_values(...)`
- parse and merge results

This is safe but not ideal when the account contains many properties or a large number of services. A future improvement should collect per-Cuid work in parallel where the upstream API allows it, while respecting rate limits and avoiding excessive concurrent requests.

### 3.2 Reduce repeated dictionary and service lookups
In `sensor.py`, the sensor factory repeatedly resolves service metadata and building averages using multiple direct dictionary lookups and lambda closures. While this is readable, it creates repeated nested lookups during state and attribute generation. A future optimization should precompute a normalized service map and cache sensor metadata per service and per property.

### 3.3 Cache derived data for entity state attributes
The entity `native_value` and `extra_state_attributes` properties recompute attributes from coordinator data on every HA state read. This is acceptable for a small integration, but if the data set grows or entities are updated frequently, it may become inefficient. A future improvement should consider caching derived static metadata per entity or reducing per-read work in the attribute calculation path.

### 3.4 Add more robust partial-data handling
The coordinator already handles missing month values and unavailable KNMI values gracefully, which is good. The next phase should formalize the data contract for partial API responses and document exactly which sensors are unavailable or unknown under different outage scenarios, including partial authentication failures and incomplete billing periods.

### 3.5 Tighten refresh strategy around startup and rate limits
The coordinator intentionally uses a quick month fetch on first refresh, then full polling later. This is a solid optimization. The next requirement should be to document and test the startup behavior clearly, especially around the 24-hour update interval and do-not-poll-faster-than-necessary guidance.

### 3.6 Improve diagnostics and observability
The repo includes diagnostics support, which is useful. Future work should add clearer structured logging around API failures, skipped months, and per-Cuid processing. This would make support and debugging much easier for users with multiple properties or irregular API responses.

## 4. Functional requirements

### FR-1: Data refresh
The integration shall authenticate and refresh consumption data for each configured property using the external `mijn.ista.nl` API.

### FR-2: Multi-property support
The integration shall support accounts with multiple properties and create a distinct HA device grouping per `Cuid`.

### FR-3: Sensor generation
The integration shall generate sensors for annual totals, monthly totals, building averages, service metadata, and temperature data where the API provides them.

### FR-4: Locale handling
The integration shall preserve Dutch service names in Dutch HA language settings and translate to English where appropriate when Home Assistant is configured in English.

### FR-5: Data resilience
The integration shall continue to operate when a subset of monthly or averaged data is missing and shall surface unavailable state values instead of failing the whole entry.

### FR-6: Diagnostics support
The integration shall expose enough diagnostic detail to help troubleshoot API issues without leaking sensitive credentials.

## 5. Non-functional requirements

### NFR-1: Performance
The integration shall avoid unnecessary blocking or repeated API calls and shall keep coordinator refreshes within a predictable time window for normal-sized accounts.

### NFR-2: Reliability
The integration shall recover from authentication and connection failures by raising the appropriate Home Assistant exceptions and preventing stale or misleading data from being treated as valid.

### NFR-3: Maintainability
Code should remain readable and testable, with raw API parsing isolated from entity logic and data model transformations kept explicit.

### NFR-4: Compatibility
Changes must remain compatible with current Home Assistant custom integration patterns and with existing config entry migration behavior.

## 6. Proposed acceptance criteria
- The integration loads successfully for accounts with one or more properties.
- Sensors update on the configured interval with no repeated full-history fetches unless required.
- Missing service data does not crash the setup or entity creation flow.
- Unit conversion and device classes match supported Home Assistant sensor semantics.
- Diagnostics output excludes passwords and other sensitive values.

## 7. Suggested future work backlog
1. Add a lightweight profiling pass to confirm refresh latency and identify hottest code paths.
2. Improve per-Cuid parallelization of independent API calls while enforcing safe concurrency limits.
3. Precompute service metadata maps and reduce repeated lookups in sensor generation.
4. Document expected behavior for partial month and averaging data.
5. Add more regression tests for multi-property and missing-data scenarios.

## 8. Recommendation
The current integration is already well-structured and clearly aligned with Home Assistant conventions. The most valuable improvements are not architectural rewrites but targeted performance tuning, data-contract hardening, and better observability for partial API responses.
