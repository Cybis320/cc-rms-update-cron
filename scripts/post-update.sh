#!/usr/bin/env bash
# Run by the cc-utils hourly updater after it pulls new code: as the user,
# unattended, possibly with no display. Refreshes what a pull alone does not.
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CC_TOOL=rms_update_cron
# shellcheck source=../cc-utils/lib.sh
. "$DEST/cc-utils/lib.sh"

# Package metadata and entry points (the code itself is an editable install).
PY="$(cc_python "$DEST")"
cc_pip_install "$PY" "$DEST" "ephem:ephem>=4"
