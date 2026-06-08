#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
APP_ID="save-editor"
EXEC_PATH="$ROOT/dist/SaveEditor"
ICON_SRC="$ROOT/packaging/linux/save-editor.svg"
DESKTOP_TEMPLATE="$ROOT/packaging/linux/SaveEditor.desktop.in"
ICON_DEST="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/$APP_ID.svg"
DESKTOP_DEST="${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP_ID.desktop"

if [ ! -x "$EXEC_PATH" ]; then
  echo "Missing executable: $EXEC_PATH" >&2
  echo "Run: python3 build.py" >&2
  exit 1
fi

install -Dm644 "$ICON_SRC" "$ICON_DEST"
install -Dm644 "$DESKTOP_TEMPLATE" "$DESKTOP_DEST"

python3 - "$DESKTOP_DEST" "$EXEC_PATH" <<'PY'
import pathlib
import sys

desktop = pathlib.Path(sys.argv[1])
exe = pathlib.Path(sys.argv[2])
text = desktop.read_text(encoding="utf-8")
exec_value = '"' + str(exe).replace('"', '\\"') + '"'
text = text.replace("@EXEC@", exec_value)
desktop.write_text(text, encoding="utf-8")
PY

chmod +x "$DESKTOP_DEST"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$(dirname "$DESKTOP_DEST")" >/dev/null 2>&1 || true
fi

if command -v kbuildsycoca6 >/dev/null 2>&1; then
  kbuildsycoca6 >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
  kbuildsycoca5 >/dev/null 2>&1 || true
fi

echo "Installed Save Editor launcher:"
echo "  $DESKTOP_DEST"
echo "Icon:"
echo "  $ICON_DEST"
