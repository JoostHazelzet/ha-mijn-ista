"""Diagnostics support for mijn.ista.nl."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MijnIstaConfigEntry

_REDACT = {"password", "username"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MijnIstaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry, with credentials redacted."""
    coordinator = entry.runtime_data

    properties: dict[str, Any] = {}
    for cuid, c in (coordinator.data or {}).items():
        properties[cuid[:8] + "…"] = {
            "location": f"{c.zip_code} {c.city}",
            "services": [
                {"id": s.id, "description": s.description, "unit": s.unit}
                for s in c.services
            ],
            "meter_count": len(c.meters),
            "unclassified_meter_count": sum(m.unit is None for m in c.meters.values()),
        }

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "last_update_success": coordinator.last_update_success,
                "properties": properties,
            },
        },
        _REDACT,
    )
