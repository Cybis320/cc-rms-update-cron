#!/usr/bin/env bash
#
# One-command installer for rms_update_cron.
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-update-cron/master/install.sh | bash
#       -- or, from a clone --
#   ./install.sh
#
# This is the setup step install.sh runs; the old one-liner that curls this file
# directly still works.
# Idempotent: clones or updates the repo, installs the package into the RMS
# virtualenv (or a local .venv), runs `rms-update-cron --install` to set (or
# update) the cron job, and installs the shared hourly updater. Re-run it any
# time.
#
# Set CC_NO_INSTALL=1 to install the package only and NOT touch the crontab.
#
set -euo pipefail

# --- Settings (override via environment) ------------------------------------
REPO_URL="${CC_REPO_URL:-https://github.com/Cybis320/cc-rms-update-cron.git}"
DEST="${CC_DEST:-$HOME/source/CC_Utils/rms_update_cron}"
VENV="${CC_VENV:-$HOME/vRMS}"

info() { printf '\033[32m[deploy]\033[0m %s\n' "$1"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$1"; }

# --- 1. Get the code --------------------------------------------------------
# If we're already running from inside a clone, use it; otherwise clone/update.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../pyproject.toml" ] \
        && grep -q '^name = "rms_update_cron"' "$SCRIPT_DIR/../pyproject.toml"; then
    DEST="$(cd "$SCRIPT_DIR/.." && pwd)"
    info "Using existing checkout at $DEST"
elif [ -d "$DEST/.git" ]; then
    info "Updating existing checkout at $DEST"
    git -C "$DEST" pull --ff-only \
        || warn "Could not fast-forward $DEST (offline, or local changes); using it as is."
else
    info "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --depth 1 "$REPO_URL" "$DEST"
fi

CC_TOOL=rms_update_cron
if [ ! -f "$DEST/cc-utils/lib.sh" ]; then
    warn "$DEST predates this installer and could not be updated."
    warn "See: git -C $DEST status   (then re-run this command)"
    exit 1
fi
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

# --- 2. Python environment --------------------------------------------------
# Prefer the RMS virtualenv (it already has ephem); otherwise make a local one.
# ephem is only pip-installed when no copy imports.
CC_VENV="$VENV"
PY="$(cc_python "$DEST")"
info "Installing package into $PY"
cc_pip_install "$PY" "$DEST" "ephem:ephem>=4"

# --- 3. Set up the cron job -------------------------------------------------
if [ "${CC_NO_INSTALL:-0}" = "1" ]; then
    warn "CC_NO_INSTALL=1 set -- package installed, crontab left untouched."
    info "Run it yourself when ready:"
    info "    $PY -m rms_update_cron            # dry-run preview"
    info "    $PY -m rms_update_cron --install  # write the cron job"
else
    info "Computing and installing the cron schedule..."
    "$PY" -m rms_update_cron --install
fi

# --- 4. Auto-update ----------------------------------------------------------
# The updater is a crontab line too, so CC_NO_INSTALL=1 skips it as well.
if [ "${CC_NO_INSTALL:-0}" = "1" ]; then
    warn "CC_NO_INSTALL=1 set -- auto-update not scheduled either."
else
    cc_install_updater "$DEST/cc-utils"
fi
cc_mark_applied "$DEST"

echo
info "Done. To preview the schedule any time:"
info "    $PY -m rms_update_cron"
[ "${CC_NO_INSTALL:-0}" = "1" ] || info "Updates arrive hourly (cc-utils updater)."
