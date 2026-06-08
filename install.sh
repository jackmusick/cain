#!/bin/sh
# Cain installer/updater for Linux (from-source, user-local — no sudo).
#
#   cd ~/GitHub/Cain && ./install.sh
#
# Everything is user-local: a private venv under ~/.local/share/cain, a `cain`
# command (the desktop app) and `cain-cli` (the read/verify CLI) in
# ~/.local/bin, and a desktop launcher. Re-run to update after pulling changes.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
HOME_DIR="${CAIN_HOME:-$HOME/.local/share/cain}"
BIN_DIR="${CAIN_BIN:-$HOME/.local/bin}"
VENV="$HOME_DIR/venv"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

say()  { printf '\033[1m[cain]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[cain]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required"

# -- venv + install -----------------------------------------------------------
mkdir -p "$HOME_DIR" "$BIN_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    say "creating environment in $HOME_DIR"
    python3 -m venv "$VENV"
fi

say "installing Cain (this pulls PySide6 on first run — can take a minute)"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
# Two passes: resolve deps, then force-reinstall the app itself so a same-version
# source tree always overwrites (versions don't bump between local edits).
"$VENV/bin/pip" install --quiet --upgrade "$SCRIPT_DIR"
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "$SCRIPT_DIR"

ln -sf "$VENV/bin/cain" "$BIN_DIR/cain"
ln -sf "$VENV/bin/cain-cli" "$BIN_DIR/cain-cli"

# -- desktop launcher + icon --------------------------------------------------
say "installing desktop launcher"
mkdir -p "$APPS_DIR" "$ICON_DIR"
install -Dm644 "$SCRIPT_DIR/packaging/linux/cain.svg" "$ICON_DIR/cain.svg"
sed "s|@EXEC@|$BIN_DIR/cain|g" \
    "$SCRIPT_DIR/packaging/linux/Cain.desktop.in" > "$APPS_DIR/cain.desktop"
chmod +x "$APPS_DIR/cain.desktop"

command -v update-desktop-database >/dev/null 2>&1 \
    && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "NOTE: $BIN_DIR is not on your PATH — add it to run 'cain' from a terminal" ;;
esac

say "done."
say "  launch:   cain   (or find 'Cain' in your app menu)"
say "  read CLI: cain-cli --mpq <pd2data.mpq> character <save.d2s>"
say "  update:   re-run ./install.sh"
