# Copilot instructions for ha-mijn-ista

## Project context
This repository is a Home Assistant custom integration for the Dutch `mijn.ista.nl` energy portal. The implementation lives under `custom_components/mijn_ista` and follows Home Assistant conventions: config entries, `DataUpdateCoordinator`, and sensor entities.

## Coding expectations
- Keep changes aligned with Home Assistant integration best practices.
- Prefer async, non-blocking code and reuse `DataUpdateCoordinator` for refresh logic.
- Keep APIs and config flow behavior backwards-compatible unless a migration is explicitly required.
- Favor clear, typed data models and small pure helper functions over repeated inline logic.
- Preserve user-facing sensor naming and translation behavior from `config_flow.py` and `sensor.py`.

## Repository-specific guidance
- `__init__.py` owns setup, migration, and reload behavior for the config entry.
- `coordinator.py` should remain the single place that fetches and normalizes raw API payloads.
- `sensor.py` should remain focused on entity creation and state/attribute derivation from coordinator data.
- Treat the raw `mijn_ista_api` responses as external data; normalize and validate them before exposing them in Home Assistant entities.

## Quality bar
- Prefer small, focused PRs with tests covering both normal and partial-data scenarios.
- Validate edge cases such as missing monthly data, unavailable KNMI values, and stale or empty responses.
- Keep logging useful but low-noise; avoid excessive debug output in hot paths.
- If optimizations affect API polling, respect the project's expected update-frequency limits and avoid unnecessary repeated calls.

## Test and validation
When making code changes, validate with the relevant existing tests and linting commands, for example:

```bash
pytest -q
ruff check custom_components/
```

## Constraints
- Do not introduce unsupported Home Assistant patterns or remove existing migration logic without updating tests.
- Do not hard-code service names in a way that breaks English/Dutch locale handling.
- Do not change the external behavior of sensors unless the change is clearly required and documented.
