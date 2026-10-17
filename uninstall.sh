#!/usr/bin/env bash
# Remove op-remote-ui: unpatch ui.py and delete the install directory.
#   ./uninstall.sh [OPENPILOT_DIR]
set -euo pipefail

OP_DIR="${1:-${OPENPILOT_DIR:-/data/openpilot}}"
INSTALL_DIR="${INSTALL_DIR:-/data/op-remote-ui}"
UI_PY="$OP_DIR/selfdrive/ui/ui.py"

if [ -f "$UI_PY" ] && grep -qF "# >>> op-remote-ui >>>" "$UI_PY"; then
  sed -i '/# >>> op-remote-ui >>>/,/# <<< op-remote-ui <<</d' "$UI_PY"
  echo "Unpatched $UI_PY"
fi
rm -rf "$INSTALL_DIR"
echo "Removed $INSTALL_DIR. Restart the openpilot UI to finish."
