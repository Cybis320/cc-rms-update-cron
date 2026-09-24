# rms_update_cron

Picks the best time of day to shut down, update and restart RMS, and installs it
as a cron job — computed from the station's own latitude/longitude so the update
never collides with capture or morning processing.

## Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-update-cron/master/install.sh | bash
```

Clones (or updates) the repo into `~/source/CC_Utils/rms_update_cron`, installs
the package into the RMS virtualenv at `~/vRMS` (or a local `.venv` if there
isn't one), and runs `rms-update-cron --install` to set the cron job. Idempotent —
re-run any time.

Updates install themselves: the installer schedules the shared hourly
[cc-utils](cc-utils/README.md) updater (one crontab line, tagged
`# cc-utils-update`). The older one-liner that curled `scripts/deploy.sh` still
works.

Set `CC_NO_INSTALL=1` to install the package but **not** touch your crontab
(this also skips scheduling the auto-updater):

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-update-cron/master/install.sh | CC_NO_INSTALL=1 bash
```

Other overrides: `CC_DEST` (checkout location), `CC_VENV` (virtualenv),
`CC_REPO_URL`, `CC_NO_AUTOUPDATE=1`.

## What it does

RMS captures at night and processes after sunrise. The update must run **after**
morning processing finishes and **before** the evening capture starts. This tool
reads the station coordinates, then over a full year finds:

- the **latest** morning capture-end (sunrise) `+ processing time` → earliest safe start
- the **earliest** evening capture-start (sunset) `− buffer` → latest safe start

and picks the **latest whole hour** in that window (maximising processing
headroom), in the system's **DST-aware local timezone** — the same clock cron
fires on. It matches RMS's own capture horizon (`-5:26`, `CAPTURE_HORIZON_DEG`),
so the windows line up exactly.

## Coordinates

Read from the first `.config` under `~/source/Stations/<stationID>/` (stations at
one site share coordinates), falling back to `~/source/RMS/.config`. Override
with `--config`.

## Static vs seasonal schedule

`--install` picks the right mode automatically:

- **Static** (most latitudes): one hour valid *every* day of the year → a single
  `GRMSUpdater` cron line.
- **Seasonal** (high latitude, where no single hour works year-round): installs a
  small **daily cron "tick"** that re-runs the tool in `--retune` mode. The tick
  recomputes the hour over a short rolling horizon and rewrites the cron line only
  when it changes — and during **polar night** (capture runs 24 h continuously) it
  comments the update line out, restoring it automatically when a daytime gap
  returns. Its output goes to `~/rms_update_cron_retune.log`.

## Run from a terminal

```bash
python3 -m rms_update_cron            # dry-run: print the recommendation
python3 -m rms_update_cron --install  # write it to your crontab
```

Useful flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--config` | auto-discover | Path to an RMS `.config` |
| `--command` | the `GRMSUpdater.sh` invocation | Command the cron job runs |
| `--processing-hours` | `2` | Post-sunrise processing time to reserve |
| `--presunset-buffer` | `1` | Hours before capture start to finish updating |
| `--horizon` | `-5:26` | Sun angle (deg:min) for capture start/stop |
| `--year` | `2027` | Reference (non-leap) year scanned for feasibility |
| `--retune-days` | `5` | Rolling horizon the seasonal tick tunes over |
| `--install` | | Write/update the cron job |
| `--retune` | | (used by the seasonal tick) recompute and update now |

## Uninstall

Remove the managed lines from your crontab (`crontab -e`) — the active
`GRMSUpdater.sh` line, any `# rms-update-cron-tick` line, and any
`#RMS_UPDATE_DISABLED` line — then:

```bash
pip uninstall rms_update_cron
rm -rf ~/source/CC_Utils/rms_update_cron
```

## Requirements

Python ≥ 3.9 (for `zoneinfo`) and `ephem` — both already present in the RMS
virtualenv.
