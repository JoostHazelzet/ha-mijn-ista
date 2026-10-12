# ista Nederland — mijn.ista.nl

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/JoostHazelzet/mijn_ista_nl)](https://github.com/JoostHazelzet/mijn_ista_nl/releases)

Home Assistant integration for [mijn.ista.nl](https://mijn.ista.nl) — the Dutch ista energy monitoring portal. Publishes physical meter readings alongside Ista's billing-period consumption, current-month consumption, building benchmarks, and climate diagnostics.

---

## Quick install

**Step 1 — Add this repository to HACS:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JoostHazelzet&repository=mijn_ista_nl&category=integration)

**Step 2 — Set up the integration:**

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mijn_ista)

> Both buttons require the [My Home Assistant](https://my.home-assistant.io/) companion service to be configured.

---

## What it does

Reads all four consumption endpoints, preserving the original physical meter
sensors and adding source-provided metrics for each billing service.

| Sensor | What it shows |
|---|---|
| `{Service} {serial}` | Current raw meter register (`EndValue`), not period consumption (`CValue`) |
| `{Service} total` | Active billing-period consumption (`TotalNow`), with meter number, beginning and ending values |
| `{Service} current month` | Calendar month's `TotalConsumption`, with `has_approximation` and month attributes |
| `Building average {Service}` | Calendar month's `BuildingAverage` / `BuldingAverage` |
| `Normalized Heating` | Ista's `NormalizedValue` for heating over the active billing period |
| `Outdoor average temperature` | Calendar month's `at` in °C |
| `Last API sync` | Source `ed` timestamp, not the local polling time |

For a property with heating, warm water, and cold water this provides **12 new
entities**, in addition to its existing meter entities. Names are localized for
English and Dutch; Home Assistant assigns entity IDs, which can be renamed.

All sensors are grouped under a single **ista NL** device per property in the Home Assistant device registry.
Use Home Assistant's Energy Dashboard, Utility Meters, or Grafana for historical
tracking and comparisons.

---

## Architecture: Ista remains the source of truth

The coordinator fetches and validates Ista's registers, service totals, monthly
values, and benchmarks. Sensors do not calculate substitute consumption or
weather corrections. Home Assistant Recorder owns history from installation
onward; this integration does not backfill monthly API history into statistics.
See the [architecture requirements](REQUIREMENTS.md#11-architecture-rule-source-provided-measurements-and-benchmarks).

---

## Requirements

- A valid [mijn.ista.nl](https://mijn.ista.nl) account (Dutch ista portal)
- Home Assistant 2024.1.0 or newer
- [HACS](https://hacs.xyz/) (recommended) or manual installation

---

## Installation

### Option A — HACS (recommended)

1. Click the **Add repository** button above, or go to **HACS → Integrations → ⋮ → Custom repositories** and add:
   ```
   https://github.com/JoostHazelzet/mijn_ista_nl
   ```
   with category **Integration**.
2. Search for **ista Nederland (mijn.ista.nl)** in HACS and install it.
3. Restart Home Assistant.
4. Click the **Set up integration** button above, or go to **Settings → Devices & Services → Add Integration** and search for **ista Nederland**.

### Option B — Manual

1. Download the [latest release](https://github.com/JoostHazelzet/mijn_ista_nl/releases/latest).
2. Copy the `custom_components/mijn_ista/` folder into your `<config>/custom_components/` directory.
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **ista Nederland**.

---

## Configuration

Fill in the following fields when adding the integration:

| Field | Description |
|---|---|
| **Email** | Your mijn.ista.nl login e-mail address |
| **Password** | Your mijn.ista.nl password |
| **Language** | `English` or `Nederlands` — affects sensor names |
| **Update interval** | How often to poll the API (1–24 hours, default 6 h) |

> Credentials are validated against the live API before the entry is created.

The update interval can be changed later via **Settings → Devices & Services → ista Nederland → Configure** without re-adding the integration.

---

## Home Assistant entities

The following **12 additional entities** are available for a property with hot
water, heating, and cold water, alongside its existing physical meter sensors.
Names below use English service labels; Dutch names are localized. Home Assistant
generates entity IDs from the device and entity names, so IDs vary by property,
language, and any user renaming.
The **Example entity ID** column shows suggested IDs you can assign in Home
Assistant; these are not guaranteed default IDs.

Units follow the API's billing-service metadata. The tables show m³ for water
and GJ for heating; heating services reporting kWh use kWh instead.

### Billing-period totals

These sensors expose consumption in the active billing period and can be used
in the applicable Energy Dashboard section. They use
`state_class: total_increasing`, with `device_class: water` or `energy`.

| Sensor | Example entity ID | Unit | Attributes |
|---|---|---|---|
| Hot water total | `sensor.ista_warm_water_total` | m³ | `meter_number`, `begin_value`, `end_value` |
| Heating total | `sensor.ista_heating_total` | GJ | `meter_number`, `begin_value`, `end_value` |
| Cold water total | `sensor.ista_cold_water_total` | m³ | `meter_number`, `begin_value`, `end_value` |

For services with multiple meters, these attributes are provided in a `meters`
list instead. Select either a physical meter or its service total for the same
supply in the Energy Dashboard, not both.

### Current-month consumption

The integration selects the `mc` entry matching the current calendar year and
month, regardless of array order. Missing current-month data is unavailable.
These sensors use `state_class: total` with a monthly `last_reset` to accommodate
revised estimates; `has_approximation` reflects the API value rather than always
being `true`.

| Sensor | Example entity ID | Unit | Attributes |
|---|---|---|---|
| Hot water current month | `sensor.ista_warm_water_current_month` | m³ | `month`, `has_approximation` |
| Heating current month | `sensor.ista_heating_current_month` | GJ | `month`, `has_approximation` |
| Cold water current month | `sensor.ista_cold_water_current_month` | m³ | `month`, `has_approximation` |

### Efficiency and benchmarks

These contextual sensors publish Ista's building averages and normalized heating
baseline unchanged. They are not cumulative Energy Dashboard sources.

| Sensor | Example entity ID | Unit | What it shows |
|---|---|---|---|
| Building average Hot water | `sensor.ista_building_average_warm_water` | m³ | Current month's building average, with a `month` attribute |
| Building average Heating | `sensor.ista_building_average_heating` | GJ | Current month's building average, with a `month` attribute |
| Building average Cold water | `sensor.ista_building_average_cold_water` | m³ | Current month's building average, with a `month` attribute |
| Normalized Heating | `sensor.ista_normalized_heating` | GJ | Ista's `NormalizedValue` for the active billing period, not a monthly value |

### Climate and diagnostics

| Sensor | Example entity ID | Unit | Device class | What it shows |
|---|---|---|---|---|
| Outdoor average temperature | `sensor.ista_outdoor_average_temperature` | °C | `temperature` | Current month's average outdoor temperature (`at`) |
| Last API sync | `sensor.ista_last_sync` | — | `timestamp` | Source `ed` timestamp, not the local polling time; diagnostic entity |

---

## Sensors in detail

### Raw readings and statistics

The integration preserves `CurMeters[].EndValue` from `UserValues`.
`ConsumptionValues` supplies active-period service totals, with `UserValues`
as the source when the optional endpoint is unavailable. `MonthValues` supplies
current-month consumption, building averages, temperature, and its source
timestamp. `ConsumptionAverages` supplies the normalized heating benchmark.
API values are already decimal numbers; `DecPos` does not rescale them.

Verified physical register families are currently **ultego** heat meters (GJ or
kWh, `device_class: energy`) and **domaqua** water meters (m³,
`device_class: water`). These use `state_class: total_increasing`, enabling
Home Assistant consumption statistics and selection in the applicable Energy
Dashboard section. There is no billing-period `last_reset`: a billing rollover
does not reset a physical register.

Other meter types, missing service metadata, and unknown units remain visible as
raw values **without an inferred unit, device class, or state class**. In
particular, heat-cost allocator registers must not inherit a service's GJ unit.
These sensors are not Energy Dashboard sources until their register semantics
are verified. Electricity and gas meter families have not yet been verified.

Attributes contain only meter identity, meter type, and `reading_date` when
provided. Dates are normalized to **`YYYY-MM-DD`**, without inventing a time or
timezone. Missing or invalid readings become unavailable, never zero. Meters
that first appear in a later refresh are added automatically.

### Consumption and benchmarks

Service totals use `state_class: total_increasing` and water or energy device
classes according to the service unit. They reset with billing periods; physical
registers do not. Choose **either** a physical register or its service total for
the same supply in the Energy Dashboard to avoid double counting.

Current-month consumption uses `state_class: total` with a calendar-month
`last_reset`, since estimates can be revised downward. Month entries are matched
by year and month, not array order. If the API only returns older months, the
current-month sensors are unavailable rather than showing stale history.
Benchmarks are contextual values, not cumulative Energy Dashboard sources.
Ista's normalized value is published unchanged, without assuming it is a
monthly measurement. For services with multiple meters, totals expose a
`meters` attribute instead of attributing the combined value to one meter.

Missing values, missing KNMI temperature, and invalid or timezone-less source
timestamps remain unavailable, never zero or an invented timestamp. Optional
endpoint failures are logged without hiding valid physical readings. Startup
uses quick month-shard polling; subsequent updates request complete shards.
All requests retain the shared rate limiter and configured refresh frequency.

### Upgrading from period-based sensors

Raw-reading sensors use new unique IDs ending in `_reading`, rather than
reusing annual-consumption IDs. The old annual, monthly, comparison, building
average, and previously retired temperature entities are removed from the
entity registry. Existing recorder history is not rewritten or transferred;
update dashboards, automations, Utility Meters, and Energy Dashboard selections
to use the new sensors. Entity IDs may receive a suffix if an old name is still
reserved when the new entity is registered.

Home Assistant records readings from installation onward. This integration
does not backfill historical measurements, and delayed API updates cannot
provide precise daily or hourly consumption attribution.

---

## Notes and limitations

- **Update frequency**: mijn.ista.nl data is updated every 3 days for smart meters, or monthly for traditional meters. The default poll interval is 6 hours; polling more often does not produce more up-to-date data. After a failed refresh the integration retries after 30 minutes and then returns to the regular interval.
- **Gentle on the ista servers**: logins to mijn.ista.nl are at least 120 s apart and data requests at least 5 s apart, shared by all ista NL entries. A refresh can therefore take a few minutes, especially with several properties.
- **No cost sensors**: The NL portal API does not expose billing amounts in euros — only consumption units (GJ, kWh, m³, etc.).
- **Dutch API**: Service names returned by the API are always in Dutch. When language is set to English, the integration translates them client-side (Verwarming → Heating, Warm water → Hot water, etc.). Unknown service names are passed through as-is.
- **Single property**: If your account has multiple properties (Cuid), each creates its own ista NL device.
- **German ista**: This integration is for the Dutch portal only. For the German ista portal, use the [ecotrend-ista](https://github.com/Ludy87/ecotrend-ista) integration.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Integration fails to set up | Wrong credentials — verify by logging in at mijn.ista.nl. A temporary login problem makes Home Assistant retry the setup automatically |
| "Reauthentication required" repair | mijn.ista.nl rejected the stored password twice in a row. Open the repair and enter the current password |
| "Too many login attempts" in the setup dialog | Logins are spaced at least 120 s apart. Wait the indicated number of seconds and submit again |
| Warnings about "session cookie chunks not sent" | The HTTP client dropped part of the login cookie. Update the integration and report the aiohttp version in an issue |
| All sensors unavailable | API unreachable or account suspended |
| Sensor values stale | mijn.ista.nl data updates every 3 days at most |

Enable debug logging to capture API responses:

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.mijn_ista: debug
```

---

## Contributing

### Standalone API testing

For API experiments without Home Assistant, install the independently packaged
[`mijn-ista-client`](packages/mijn-ista-client/README.md):

```bash
uv sync --locked
uv run pytest -q
uv run python packages/mijn-ista-client/examples/read_consumption.py
```

Python 3.13 or newer is required. The example prompts for credentials and contacts
the portal; the tests are offline and do not load Home Assistant. Notebooks,
examples and standalone tests share the repository's root `.venv`; no
package-specific environment is required. Select that interpreter/kernel in
VS Code. Run integration tests in the same environment with
`uv run --locked --group integration pytest -q tests` (Python 3.13.2+).
The integration continues
to use its current API client until the standalone package is stable and published;
see [REQUIREMENTS.md](REQUIREMENTS.md) for the adoption boundary.

### Community maintenance and history

Joost Hazelzet maintains this continuation of
[Ali Alaei's original integration](https://github.com/aalaei/ha-mijn-ista).
The original integration commits and subsequent development history are retained.
The [original MIT-declared API client](https://github.com/aalaei/mijn-ista-api)
has also been imported with its complete Git history. Its protocol and the
resilient wrapper now live in one
[`packages/mijn-ista-client`](packages/mijn-ista-client/README.md) distribution,
including the original MIT attribution. Standalone development no longer
requires the original author's published package. The original subtree import
and its commit history remain preserved.

The repository is detached from GitHub's fork network and maintained independently
as `JoostHazelzet/mijn_ista_nl`. Detachment and renaming preserved the Git commit
history. The Home Assistant domain remains `mijn_ista`, so existing config entries
and Python import names are unchanged.

Bug reports and pull requests are welcome at [github.com/JoostHazelzet/mijn_ista_nl](https://github.com/JoostHazelzet/mijn_ista_nl/issues).

Before submitting a PR, run:

```bash
ruff check --fix custom_components/
ruff format custom_components/
```
