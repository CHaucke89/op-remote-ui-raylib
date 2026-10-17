#!/usr/bin/env bash
# Install op-remote-ui into an openpilot checkout (stock or fork). Idempotent.
#
#   ./install.sh [OPENPILOT_DIR]
#   curl -sL https://raw.githubusercontent.com/CHaucke89/op-remote-ui/main/install.sh | bash
#
# Env: INSTALL_DIR (default /data/op-remote-ui), OPENPILOT_DIR (default /data/openpilot),
#      REPO_TARBALL (override download URL when not run from a checkout).
set -euo pipefail

OP_DIR="${1:-${OPENPILOT_DIR:-/data/openpilot}}"
INSTALL_DIR="${INSTALL_DIR:-/data/op-remote-ui}"
REPO_TARBALL="${REPO_TARBALL:-https://github.com/CHaucke89/op-remote-ui/archive/refs/heads/main.tar.gz}"
FILES=(remote_ui.py remote_ui_auto.py touch_injector.py frame_streamer.py stream_server.py requirements.txt)
BEGIN="# >>> op-remote-ui >>>"
END="# <<< op-remote-ui <<<"

die() { echo "error: $*" >&2; exit 1; }

UI_PY="$OP_DIR/selfdrive/ui/ui.py"
[ -f "$UI_PY" ] || die "$UI_PY not found (pass your openpilot directory as the first argument)"

# Python: openpilot's venv if present, else system python3. It runs both the UI
# and the server, so compiled deps (Pillow) match the interpreter that uses them.
if [ -x "$OP_DIR/.venv/bin/python" ]; then PY="$OP_DIR/.venv/bin/python"
else PY="$(command -v python3)" || die "python3 not found"; fi
echo "Using Python: $PY"

# Locate sources: next to this script, or download a tarball (curl | bash).
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
TMP=""
if [ -z "$SRC" ] || [ ! -f "$SRC/remote_ui_auto.py" ]; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "Downloading $REPO_TARBALL"
  curl -fsSL "$REPO_TARBALL" | tar -xz -C "$TMP" --strip-components=1
  SRC="$TMP"
fi

mkdir -p "$INSTALL_DIR"
for f in "${FILES[@]}"; do
  [ "$SRC/$f" -ef "$INSTALL_DIR/$f" ] || cp "$SRC/$f" "$INSTALL_DIR/$f"
done

# Dependencies go into $INSTALL_DIR/vendor (nothing touches openpilot's venv).
echo "Installing dependencies into $INSTALL_DIR/vendor"
if command -v uv >/dev/null 2>&1; then
  uv pip install --quiet --python "$PY" --target "$INSTALL_DIR/vendor" -r "$INSTALL_DIR/requirements.txt"
elif "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip install --quiet --target "$INSTALL_DIR/vendor" -r "$INSTALL_DIR/requirements.txt"
else
  die "neither uv nor pip is available for $PY"
fi

# Patch ui.py with a single marked block (skipped if already present).
if grep -qF "$BEGIN" "$UI_PY"; then
  echo "ui.py already patched"
else
  BLOCK="$BEGIN
import sys; sys.path.insert(0, \"$INSTALL_DIR\"); import remote_ui_auto  # noqa: E402,F401
$END"
  OUT="$(mktemp)"
  # Keep a shebang on line 1 if there is one.
  awk -v block="$BLOCK" 'NR==1 && /^#!/ {print; print block; next} NR==1 {print block} {print}' "$UI_PY" > "$OUT"
  cat "$OUT" > "$UI_PY"; rm -f "$OUT"
  echo "Patched $UI_PY"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Restart the openpilot UI (or reboot), then open http://${IP:-DEVICE_IP}:8081"
echo "Note: openpilot updates may reset ui.py; re-run this script afterwards."
