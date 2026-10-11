# Agent instructions for this repository

This project is a Home Assistant custom integration for `mijn.ista.nl`. Follow the architecture and constraints below when editing code or suggesting changes.

## Architecture
- `custom_components/mijn_ista/__init__.py`: setup, migration, reload, and platform forwarding.
- `custom_components/mijn_ista/config_flow.py`: config entry setup and validation.
- `custom_components/mijn_ista/coordinator.py`: API polling, normalization, and data shaping.
- `custom_components/mijn_ista/sensor.py`: sensor entity generation and state logic.

## Principles
- Prefer `DataUpdateCoordinator` patterns and async I/O.
- Keep raw API parsing separate from entity logic.
- Maintain partial-data tolerance: missing monthly values, absent averages, and temporary outages must not crash the integration.
- Preserve translation and unit mapping behavior for all service names and meter units.
- Keep changes minimal and testable.

## Review checklist
Before finalizing a change, confirm:
- the update flow still works when API data is incomplete
- sensor names remain stable for both Dutch and English HA languages
- no unnecessary extra polling or blocking work was introduced
- any migration is covered by tests and documentation
