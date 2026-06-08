#!/bin/sh
# Cain installer/updater for Linux (user-local, no sudo).
#
#   curl -fsSL https://raw.githubusercontent.com/jackmusick/cain/main/install.sh | sh
#   # ...or from a checkout:
#   ./install.sh
#
# Creates a private venv under ~/.local/share/cain, installs `cain` (the desktop
# app) and `cain-cli` (the read/verify CLI) into ~/.local/bin, and a desktop
# launcher. Re-run to update.
set -eu

REPO_TARBALL="https://github.com/jackmusick/cain/archive/refs/heads/main.tar.gz"
RAW_BASE="https://raw.githubusercontent.com/jackmusick/cain/main"
HOME_DIR="${CAIN_HOME:-$HOME/.local/share/cain}"
BIN_DIR="${CAIN_BIN:-$HOME/.local/bin}"
VENV="$HOME_DIR/venv"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

say()  { printf '\033[1m[cain]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[cain]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required"

# Install from a local checkout when this script sits next to pyproject.toml,
# otherwise from the GitHub tarball (the curl | sh path).
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    SOURCE="$SCRIPT_DIR"
    say "installing from local checkout: $SCRIPT_DIR"
else
    SOURCE="cain @ $REPO_TARBALL"
    say "installing from $REPO_TARBALL"
fi

mkdir -p "$HOME_DIR" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
if [ ! -x "$VENV/bin/python" ]; then
    say "creating environment in $HOME_DIR"
    python3 -m venv "$VENV"
fi

say "installing Cain (pulls PySide6 on first run — can take a minute)"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
# Two passes: resolve deps, then force-reinstall the app itself so a same-version
# source always overwrites (main moves without version bumps).
"$VENV/bin/pip" install --quiet --upgrade "$SOURCE"
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "$SOURCE"

ln -sf "$VENV/bin/cain" "$BIN_DIR/cain"
ln -sf "$VENV/bin/cain-cli" "$BIN_DIR/cain-cli"

# Desktop launcher + icon — from the checkout, or fetched from the repo.
say "installing desktop launcher"
if [ -n "${SCRIPT_DIR:-}" ] && [ -f "$SCRIPT_DIR/packaging/linux/Cain.desktop.in" ]; then
    install -Dm644 "$SCRIPT_DIR/packaging/linux/cain.svg" "$ICON_DIR/cain.svg"
    sed "s|@EXEC@|$BIN_DIR/cain|g" \
        "$SCRIPT_DIR/packaging/linux/Cain.desktop.in" > "$APPS_DIR/cain.desktop"
elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$RAW_BASE/packaging/linux/cain.svg" -o "$ICON_DIR/cain.svg" || true
    curl -fsSL "$RAW_BASE/packaging/linux/Cain.desktop.in" \
        | sed "s|@EXEC@|$BIN_DIR/cain|g" > "$APPS_DIR/cain.desktop" || true
fi
chmod +x "$APPS_DIR/cain.desktop" 2>/dev/null || true

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
say "  update:   re-run this installer"
