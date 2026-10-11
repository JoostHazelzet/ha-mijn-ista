"""Conservative request policy for the undocumented mijn.ista.nl portal."""

from typing import Final

MIN_LOGIN_GAP_S: Final = 120
MIN_REQUEST_GAP_S: Final = 5
LOGIN_ATTEMPTS: Final = 3
INVALID_CREDENTIALS_CONFIRMATIONS: Final = 2
MONTH_POLL_INTERVAL_S: Final = MIN_REQUEST_GAP_S
MONTH_BUDGET_QUICK_S: Final = 30
MONTH_BUDGET_FULL_S: Final = 120
