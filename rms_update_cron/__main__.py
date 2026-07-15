#!/usr/bin/env python3
"""
rms_update_cron.py

Pick the best daily time to shut down, update and restart RMS, and (optionally)
install it as a cron job.

RMS captures at night and processes after sunrise. The update must run:
  * AFTER morning processing finishes  (latest sunrise-end + processing time)
  * BEFORE the evening capture starts   (earliest sunset-start - a safety buffer)

The chosen time is a whole clock hour, expressed in the system's local timezone
(the timezone cron itself uses), read from the RMS .config coordinates.

Two scheduling modes:
  * Static (default): one hour valid EVERY day of the year. Works up to ~mid-high
    latitude. `--install` writes a single GRMSUpdater cron line.
  * Seasonal: at high latitude no single hour is valid year-round (the daylight
    gap moves, and during polar night capture runs continuously). `--install`
    then installs a small daily cron "tick" that re-runs this tool in `--retune`
    mode, keeping the GRMSUpdater line tracking the season -- and commenting it
    out during polar night, auto-restoring it when a safe gap returns.

RMS starts/stops capture when the Sun is 5:26 below the horizon (CAPTURE_HORIZON_DEG
in RMS/CaptureDuration.py); this tool matches that so the windows line up.
"""

import os
import sys
import math
import shlex
import argparse
import datetime
import subprocess

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    import ephem
except ImportError:
    sys.exit("ERROR: the 'ephem' package is required (pip install ephem, "
             "or run inside the RMS conda/venv environment).")

# Matches CAPTURE_HORIZON_DEG in RMS/CaptureDuration.py (standard capture start/stop).
DEFAULT_HORIZON = "-5:26"

STATIONS_DIR = os.path.expanduser("~/source/Stations")
RMS_FALLBACK_CONFIG = os.path.expanduser("~/source/RMS/.config")
DEFAULT_COMMAND = ("/home/ops/source/RMS/Scripts/MultiCamLinux/GRMSUpdater.sh "
                   "--term gnome-terminal --profile StartCapture --reboot-if-needed")

# Markers used to find our own lines in the crontab. Each is unambiguous:
CRON_MARKER = "GRMSUpdater.sh"        # the active update command
TICK_TAG = "rms-update-cron-tick"     # trailing shell-comment tag on the seasonal tick line
DISABLED_MARKER = "#RMS_UPDATE_DISABLED"  # sentinel for a temporarily-removed update line
TICK_SCHEDULE = "20 0 * * *"         # seasonal tick runs daily at 00:20 local
RETUNE_LOG = os.path.expanduser("~/rms_update_cron_retune.log")

UTC = datetime.timezone.utc


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def parse_config(path):
    """Read latitude, longitude, elevation from an RMS .config file."""
    wanted = {"latitude": None, "longitude": None, "elevation": None}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith((";", "#", "[")):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            if key in wanted and wanted[key] is None:
                try:
                    wanted[key] = float(val.split(";")[0].strip())
                except ValueError:
                    pass
    missing = [k for k, v in wanted.items() if v is None]
    if missing:
        sys.exit("ERROR: could not read %s from %s" % (", ".join(missing), path))
    return wanted["latitude"], wanted["longitude"], wanted["elevation"]


def discover_config():
    """
    Find the RMS .config to read coordinates from:
      1. the first .config under ~/source/Stations/<stationID>/ (stations at one
         site share coordinates, so any one is fine; sorted for determinism)
      2. else fall back to ~/source/RMS/.config
    """
    if os.path.isdir(STATIONS_DIR):
        for station in sorted(os.listdir(STATIONS_DIR)):
            cfg = os.path.join(STATIONS_DIR, station, ".config")
            if os.path.isfile(cfg):
                return cfg
    if os.path.isfile(RMS_FALLBACK_CONFIG):
        return RMS_FALLBACK_CONFIG
    sys.exit("ERROR: no .config found under %s and no fallback at %s"
             % (STATIONS_DIR, RMS_FALLBACK_CONFIG))


def local_tz():
    """
    The system's DST-aware local timezone -- the one cron uses.

    Must be DST-aware (a real zone, not a frozen current offset): the binding day
    may fall on the opposite side of a DST change from 'now', and cron fires at a
    fixed wall-clock hour using each day's actual offset. Resolve the zone the same
    way the system does, falling back to a fixed offset only if we truly can't.
    """
    if ZoneInfo is not None:
        tzenv = os.environ.get("TZ")
        if tzenv:
            try:
                return ZoneInfo(tzenv)
            except Exception:
                pass
        try:
            target = os.path.realpath("/etc/localtime")
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
        except Exception:
            pass
        try:
            with open("/etc/timezone") as f:
                return ZoneInfo(f.read().strip())
        except Exception:
            pass
    sys.stderr.write("WARNING: could not resolve a DST-aware timezone; using the "
                     "current fixed UTC offset. Results may be off by 1h across a "
                     "DST boundary.\n")
    return datetime.datetime.now().astimezone().tzinfo


# --------------------------------------------------------------------------- #
# Sun geometry
# --------------------------------------------------------------------------- #
def scan_days(lat, lon, elev, horizon, dates, tz):
    """
    For each date in `dates`, find (in local time, as hours-since-local-midnight):
      - morning capture-end   = Sun rising past the horizon before local noon
      - evening capture-start = Sun setting past the horizon after local noon
    Evening values can exceed 24 (past midnight) so they stay comparable.

    Polar days count two ways, because they mean opposite things:
      - polar_day   (Sun never drops below horizon): no capture -> any time safe
      - polar_night (Sun never rises above horizon): capture runs 24h -> no gap
    Returns (latest_morning_end, earliest_evening_start, n_polar_day, n_polar_night),
    each of the first two a (hours, date) tuple or None.
    """
    obs = ephem.Observer()
    obs.lat, obs.lon, obs.elevation = str(lat), str(lon), elev
    obs.horizon = str(horizon)
    obs.pressure = 0  # geometric horizon, consistent with RMS
    sun = ephem.Sun()

    latest_morning = None
    earliest_evening = None
    polar_day = 0
    polar_night = 0

    for day in dates:
        midnight = datetime.datetime(day.year, day.month, day.day, tzinfo=tz)
        noon_local = midnight + datetime.timedelta(hours=12)
        obs.date = noon_local.astimezone(UTC).replace(tzinfo=None)
        try:
            rise = obs.previous_rising(sun).datetime().replace(tzinfo=UTC)
            sett = obs.next_setting(sun).datetime().replace(tzinfo=UTC)
        except ephem.AlwaysUpError:
            polar_day += 1
            continue
        except ephem.NeverUpError:
            polar_night += 1
            continue

        rise_h = (rise.astimezone(tz) - midnight).total_seconds() / 3600.0
        set_h = (sett.astimezone(tz) - midnight).total_seconds() / 3600.0
        if latest_morning is None or rise_h > latest_morning[0]:
            latest_morning = (rise_h, day)
        if earliest_evening is None or set_h < earliest_evening[0]:
            earliest_evening = (set_h, day)

    return latest_morning, earliest_evening, polar_day, polar_night


def year_dates(year):
    out, day = [], datetime.date(year, 1, 1)
    while day.year == year:
        out.append(day)
        day += datetime.timedelta(days=1)
    return out


def horizon_dates(tz, n_days):
    today = datetime.datetime.now(tz).date()
    return [today + datetime.timedelta(days=i) for i in range(n_days + 1)]


def decide_hour(morning, evening, proc, buf, polar_day, polar_night):
    """
    Return (best_hour_or_None, floor, ceil, reason).
    best is the latest whole hour that fits [floor, ceil]; None means no safe hour.
    """
    if morning is None and evening is None:
        if polar_night and not polar_day:
            return None, None, None, "polar night (continuous capture, no gap)"
        if polar_day and not polar_night:
            return 12, None, None, "polar-day only (no capture; any time safe)"
        return None, None, None, "no sunrise/sunset in range"
    if morning is None or evening is None:
        return None, None, None, "incomplete day/night cycle in range"
    floor = morning[0] + proc
    ceil = evening[0] - buf
    best = int(math.floor(ceil))
    if polar_night:
        return None, floor, ceil, "polar night in range (continuous capture)"
    if floor <= best <= ceil and 0 <= best <= 23:
        return best, floor, ceil, "ok"
    return None, floor, ceil, "window too narrow for a whole hour"


def fmt_h(hours):
    """Format hours-since-midnight (may be >24) as HH:MM."""
    h = int(hours) % 24
    m = int(round((hours - int(hours)) * 60))
    if m == 60:
        h, m = (h + 1) % 24, 0
    return "%02d:%02d" % (h, m)


# --------------------------------------------------------------------------- #
# Crontab management
# --------------------------------------------------------------------------- #
def read_crontab():
    res = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""


def active_cron_line(hour, command):
    return "0 %d * * * %s" % (hour, command)


def disabled_cron_line(command):
    return ("%s no safe window (polar night / gap too small); auto-restores. "
            "Would run: %s" % (DISABLED_MARKER, command))


def tool_base_argv():
    """Shell tokens (already quoted) that re-invoke this tool from cron.

    Prefer `python -m rms_update_cron` when installed as a package (survives the
    checkout moving); fall back to the script path when run as a standalone file.
    """
    python = shlex.quote(sys.executable)
    if __package__:
        return [python, "-m", __package__]
    return [python, shlex.quote(os.path.abspath(__file__))]


def tick_cron_line(args, config_path):
    """The daily seasonal tick line that re-runs this tool in --retune mode."""
    parts = tool_base_argv() + ["--retune",
             "--config", shlex.quote(config_path),
             "--processing-hours", repr(args.processing_hours),
             "--presunset-buffer", repr(args.presunset_buffer),
             "--horizon", shlex.quote(args.horizon),
             "--retune-days", str(args.retune_days)]
    if args.command != DEFAULT_COMMAND:
        parts += ["--command", shlex.quote(args.command)]
    # Trailing shell-comment tag: ignored by /bin/sh, used only to identify our line.
    return "%s %s >> %s 2>&1 # %s" % (TICK_SCHEDULE, " ".join(parts),
                                      shlex.quote(RETUNE_LOG), TICK_TAG)


def rebuild_crontab(current_text, update_rep, tick_line):
    """
    Produce the desired crontab text.
      update_rep : the single active OR disabled-sentinel GRMSUpdater line to keep.
      tick_line  : the seasonal tick line to keep, or None to remove it.
    Our own managed lines (active update, disabled sentinel, tick) are replaced;
    everything else -- including the user's *commented* GRMSUpdater history -- is
    passed through untouched.
    """
    out, placed_update, placed_tick = [], False, False
    for line in (current_text.splitlines() if current_text else []):
        # Identify the tick line FIRST, so a custom --command that happens to
        # contain the update marker can never be mistaken for the update line.
        if TICK_TAG in line:
            if not placed_tick and tick_line is not None:
                out.append(tick_line)
                placed_tick = True
            continue  # drop if removing, or drop duplicate
        is_active_update = (CRON_MARKER in line) and not line.lstrip().startswith("#")
        is_disabled = DISABLED_MARKER in line
        if is_active_update or is_disabled:
            if not placed_update:
                out.append(update_rep)
                placed_update = True
            continue  # drop duplicates / old state
        out.append(line)
    if not placed_update:
        out.append(update_rep)
    if not placed_tick and tick_line is not None:
        out.append(tick_line)
    return "\n".join(out).rstrip("\n") + "\n"


def commit_crontab(desired_text):
    """Write only if the crontab actually changes. Returns True if written."""
    if read_crontab() == desired_text:
        return False
    res = subprocess.run(["crontab", "-"], input=desired_text, text=True)
    if res.returncode != 0:
        sys.exit("ERROR: failed to install crontab")
    return True


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def retune(args, config_path, lat, lon, elev, tz, tzname):
    """Seasonal tick: recompute the current hour over a short rolling horizon and
    update the crontab (disabling the line during polar night). Quiet & idempotent."""
    dates = horizon_dates(tz, args.retune_days)
    morning, evening, pday, pnight = scan_days(lat, lon, elev, args.horizon, dates, tz)
    best, _floor, _ceil, reason = decide_hour(
        morning, evening, args.processing_hours, args.presunset_buffer, pday, pnight)

    if best is not None:
        update_rep = active_cron_line(best, args.command)
        state = "active @ %02d:00 %s" % (best, tzname)
    else:
        update_rep = disabled_cron_line(args.command)
        state = "DISABLED (%s)" % reason

    tick_line = tick_cron_line(args, config_path)  # self-heal / keep the tick present
    changed = commit_crontab(rebuild_crontab(read_crontab(), update_rep, tick_line))

    stamp = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    print("[%s] retune horizon %s..%s (%dd): %s%s"
          % (stamp, dates[0], dates[-1], args.retune_days, state,
             "  [crontab updated]" if changed else "  [no change]"))


def install_static(args, best):
    """Write one year-round GRMSUpdater line; remove any leftover seasonal tick."""
    update_rep = active_cron_line(best, args.command)
    changed = commit_crontab(rebuild_crontab(read_crontab(), update_rep, tick_line=None))
    print("\nInstalled static year-round schedule%s:\n  %s"
          % ("" if changed else " (already current)", update_rep))


def install_seasonal(args, config_path, lat, lon, elev, tz, tzname, reason):
    """Install the daily seasonal tick and set the current hour immediately."""
    tick_line = tick_cron_line(args, config_path)
    dates = horizon_dates(tz, args.retune_days)
    morning, evening, pday, pnight = scan_days(lat, lon, elev, args.horizon, dates, tz)
    best, _f, _c, now_reason = decide_hour(
        morning, evening, args.processing_hours, args.presunset_buffer, pday, pnight)

    if best is not None:
        update_rep = active_cron_line(best, args.command)
        now_state = "currently active @ %02d:00 %s" % (best, tzname)
    else:
        update_rep = disabled_cron_line(args.command)
        now_state = "currently DISABLED (%s)" % now_reason

    changed = commit_crontab(rebuild_crontab(read_crontab(), update_rep, tick_line))
    print("\nNo single hour is valid year-round (%s)." % reason)
    print("Installed SEASONAL auto-updater%s:" % ("" if changed else " (already current)"))
    print("  tick   : %s" % tick_line)
    print("  update : %s" % update_rep)
    print("  state  : %s" % now_state)
    print("The tick runs daily, re-tunes the update hour as the season moves, and\n"
          "comments the update line out during polar night (auto-restores after).")
    print("Log: %s" % RETUNE_LOG)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None,
                    help="RMS .config path (default: first under ~/source/Stations/<id>/, "
                         "else ~/source/RMS/.config)")
    ap.add_argument("--command", default=DEFAULT_COMMAND,
                    help="command cron should run (default: the GRMSUpdater invocation)")
    ap.add_argument("--processing-hours", type=float, default=2.0,
                    help="max post-sunrise processing time to reserve (default: 2)")
    ap.add_argument("--presunset-buffer", type=float, default=1.0,
                    help="hours before capture start to finish updating (default: 1)")
    ap.add_argument("--horizon", default=DEFAULT_HORIZON,
                    help="Sun horizon deg:min for capture start/stop (default: %(default)s)")
    ap.add_argument("--year", type=int, default=2027,
                    help="reference (non-leap) year to scan for feasibility (default: %(default)s)")
    ap.add_argument("--retune-days", type=int, default=5,
                    help="rolling horizon (days) the seasonal tick tunes over (default: 5)")
    ap.add_argument("--install", action="store_true",
                    help="install/update cron: static line if a year-round hour exists, "
                         "else a seasonal daily tick")
    ap.add_argument("--retune", action="store_true",
                    help="(used by the seasonal tick) recompute over the rolling horizon "
                         "and update the crontab line now")
    args = ap.parse_args()

    config_path = args.config or discover_config()
    lat, lon, elev = parse_config(config_path)
    tz = local_tz()
    tzname = getattr(tz, "key", None) or datetime.datetime.now(tz).strftime("%Z") or str(tz)

    if args.retune:
        retune(args, config_path, lat, lon, elev, tz, tzname)
        return

    # Feasibility over the whole year.
    morning, evening, polar_day, polar_night = scan_days(
        lat, lon, elev, args.horizon, year_dates(args.year), tz)
    best, floor, ceil, reason = decide_hour(
        morning, evening, args.processing_hours, args.presunset_buffer,
        polar_day, polar_night)

    print("Config : %s" % config_path)
    print("Station: lat=%.5f lon=%.5f elev=%.0fm   horizon=%s   tz=%s"
          % (lat, lon, elev, args.horizon, tzname))
    note = "whole year has a night/day cycle"
    if polar_day or polar_night:
        note = "%d polar-day, %d polar-night day(s)" % (polar_day, polar_night)
    print("Scanning year %d: %s" % (args.year, note))
    if morning and evening:
        print("  latest  morning capture-end : %s %s (+%.1fh processing -> earliest %s)"
              % (fmt_h(morning[0]), morning[1], args.processing_hours, fmt_h(floor)))
        print("  earliest evening cap-start  : %s %s (-%.1fh buffer     -> latest   %s)"
              % (fmt_h(evening[0]), evening[1], args.presunset_buffer, fmt_h(ceil)))

    if best is not None:
        print("\nRecommended update hour: %02d:00 %s  (%.1fh after latest sunrise-end, "
              "%.1fh before earliest capture start)"
              % (best, tzname, best - morning[0], evening[0] - best))
        print("\nCron line:\n  %s" % active_cron_line(best, args.command))
        if args.install:
            install_static(args, best)
        else:
            print("\n(Dry run. Re-run with --install to write it to your crontab.)")
    else:
        print("\nNo single year-round hour: %s." % reason)
        if args.install:
            install_seasonal(args, config_path, lat, lon, elev, tz, tzname, reason)
        else:
            print("Re-run with --install to set up a seasonal auto-updater "
                  "(daily cron tick that tracks the season).")


if __name__ == "__main__":
    main()
