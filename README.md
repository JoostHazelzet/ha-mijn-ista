# ista Nederland — mijn.ista.nl

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/JoostHazelzet/mijn_ista_nl)](https://github.com/JoostHazelzet/mijn_ista_nl/releases)

Home Assistant integration for [mijn.ista.nl](https://mijn.ista.nl) — the Dutch ista energy monitoring portal. Exposes your heating, hot/cold water, electricity, and gas consumption as sensors you can track, automate, and display on your dashboard.

---

## Quick install

**Step 1 — Add this repository to HACS:**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=JoostHazelzet&repository=mijn_ista_nl&category=integration)

**Step 2 — Set up the integration:**

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=mijn_ista)

> Both buttons require the [My Home Assistant](https://my.home-assistant.io/) companion service to be configured.

---

## What it does

Reads your consumption data from mijn.ista.nl and creates sensors in Home Assistant for every service (Heating, Electricity, Hot water, Cold water, Gas — whatever your property has):

| Sensor | What it shows |
|---|---|
| `{Service} Current` | Annual total for the current billing year |
| `{Service} Previous` | Annual total for the previous billing year |
| `{Service} Change` | Year-over-year change in % |
| `{Service} Building Avg` | Average consumption across your building |
| `{Service} {serial}` | Annual reading for one physical meter |
| `{Service} Month` | Latest month's total (prior months in attributes) |
| `{Service} Month Avg` | Latest month's building average |
| `{Service} {serial} Month` | Latest month for one physical meter |
All sensors are grouped under a single **ista NL** device per property in the Home Assistant device registry.

---

## Requirements

- A valid [mijn.ista.nl](https://mijn.ista.nl) account (Dutch ista portal)
- Home Assistant 2023.x or newer
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

## Sensors in detail

### Annual sensors

The annual sensors show consumption for the current and previous billing year. The `Change` sensor is the relative difference in percent (positive = more consumption, negative = less).

Attributes on `{Service} Current` include the individual meter readings that make up the total.

### Monthly sensors

The `{Service} Month` sensor holds the most recent month's value. All prior months are available as attributes in the format:

```
attributes:
  2024-01: 42.3
  2024-02: 38.1
  ...
```

### Building averages

Building average sensors show the average consumption for your entire building or complex during the same period. Useful for comparing your own usage against your neighbours.

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
python -m pip install -e './packages/mijn-ista-api[test]' -e './packages/mijn-ista-client[test]'
python packages/mijn-ista-client/examples/read_consumption.py
cd packages/mijn-ista-client
python -m pytest -q
cd ../mijn-ista-api
python -m pytest -q
```

Python 3.13 or newer is required. The example prompts for credentials and contacts
the portal; the tests are offline and do not load Home Assistant. Use a separate
virtual environment as described in the package README. The integration continues
to use its current API client until the standalone package is stable and published;
see [REQUIREMENTS.md](REQUIREMENTS.md) for the adoption boundary.

### Community maintenance and history

Joost Hazelzet maintains this continuation of
[Ali Alaei's original integration](https://github.com/aalaei/ha-mijn-ista).
The original integration commits and subsequent development history are retained.
The [original MIT-declared API client](https://github.com/aalaei/mijn-ista-api)
has also been imported, with its complete Git history, into
[`packages/mijn-ista-api`](packages/mijn-ista-api/README.md). Its maintained
distribution is named `mijn-ista-api-community`; standalone development no longer
requires the original author's published package.

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
