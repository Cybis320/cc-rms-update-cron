#!/usr/bin/env bash
#
# One-command installer for rms_update_cron.
#
#   curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-update-cron/master/scripts/deploy.sh | bash
#       -- or, from a clone --
#   ./scripts/deploy.sh
#
# Idempotent: clones or updates the repo, installs the package into the RMS
# virtualenv (or a local .venv), then runs `rms-update-cron --install` to set
# (or update) the cron job. Re-run it any time to update.
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
    git -C "$DEST" pull --ff-only
else
    info "Cloning $REPO_URL -> $DEST"
    mkdir -p "$(dirname "$DEST")"
    git clone --depth 1 "$REPO_URL" "$DEST"
fi

# --- 2. Python environment --------------------------------------------------
# Prefer the RMS virtualenv (it already has ephem); otherwise make a local one.
if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
    info "Using virtualenv $VENV"
else
    warn "No virtualenv at $VENV; creating one at $DEST/.venv"
    if ! python3 -m venv "$DEST/.venv"; then
        rm -rf "$DEST/.venv"
        warn "python3 -m venv failed (python3-venv not installed?). Either:"
        warn "    sudo apt install python3-venv     # then re-run this installer"
        warn "or point CC_VENV at an existing virtualenv and re-run."
        exit 1
    fi
    PY="$DEST/.venv/bin/python"
fi

info "Installing package (+ ephem)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e "$DEST"

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

echo
info "Done. To preview the schedule any time:"
info "    $PY -m rms_update_cron"
info "To update later, just re-run this installer (it git-pulls)."
