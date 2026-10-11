"""Constants for mijn.ista.nl integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "mijn_ista"
MANUFACTURER: Final = "ista Nederland B.V."
DEVICE_NAME: Final = "mijn.ista.nl"

DEFAULT_UPDATE_INTERVAL: Final = 6  # hours
CONF_UPDATE_INTERVAL: Final = "update_interval"
RETRY_INTERVAL_MINUTES: Final = 30  # after a failed refresh (FR-7)

# Rate limiting (REQUIREMENTS.md NFR-5). ista publishes no limits; this policy is self-imposed
# and applies across all mijn_ista config entries.
MIN_LOGIN_GAP_S: Final = 120  # between login attempts, including retries
MIN_REQUEST_GAP_S: Final = 5  # between data requests to mijn.ista.nl/api/*
LOGIN_ATTEMPTS: Final = 3  # per scheduled refresh
INVALID_CREDENTIALS_CONFIRMATIONS: Final = 2  # consecutive rejections before reauth
FLOW_MAX_LOGIN_WAIT_S: Final = 30  # longer waits show a "try again later" error instead

# MonthValues shard polling, adapted to MIN_REQUEST_GAP_S (library: 2 s / 15 s / 60 s).
MONTH_POLL_INTERVAL_S: Final = MIN_REQUEST_GAP_S
MONTH_BUDGET_QUICK_S: Final = 30
MONTH_BUDGET_FULL_S: Final = 120

# Refresh timeout building blocks (NFR-5: timeout covers login waits and request spacing).
LOGIN_ATTEMPT_TIMEOUT_S: Final = 90  # one OIDC login, about six HTTP requests
REQUEST_BUDGET_S: Final = 60  # one data request incl. throttle wait and library 503 retries

# A session validated by the config flow is reused by setup when it is at most this old.
HANDOFF_MAX_AGE_S: Final = 300

# Client-side translation for service names returned by the API in Dutch
SERVICE_NAME_TRANSLATIONS: Final[dict[str, str]] = {
    "Verwarming": "Heating",
    "Elektriciteit": "Electricity",
    "Koud water": "Cold water",
    "Warm water": "Hot water",
    "Gas": "Gas",
}
