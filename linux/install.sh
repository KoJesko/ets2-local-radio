#!/usr/bin/env bash
# ETS2/ATS Local Radio for Linux - installer (per user, no root needed)
#   ./linux/install.sh              install launcher, desktop entry, user service, game plugin
#   ./linux/install.sh --autostart  same, and start the server at login (systemd --user)
#   ./linux/install.sh --uninstall  remove all of the above (settings/favourites are kept)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$HERE/server/ets2_local_radio.py"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
NAME=ets2-local-radio

autostart=0
uninstall=0
for arg in "$@"; do
  case "$arg" in
    --autostart) autostart=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

# Make app menus / launcher search pick up the entry now instead of after the next login.
refresh_menus() {
  have update-desktop-database && update-desktop-database -q "$APP_DIR" 2>/dev/null || true
  for k in kbuildsycoca6 kbuildsycoca5; do
    if have "$k"; then "$k" >/dev/null 2>&1 || true; break; fi
  done
}

if [[ $uninstall == 1 ]]; then
  if have systemctl; then systemctl --user disable --now "$NAME.service" 2>/dev/null || true; fi
  python3 "$SERVER" install-plugin --uninstall || true
  rm -f "$UNIT_DIR/$NAME.service" "$APP_DIR/$NAME.desktop"
  [[ -L "$BIN_DIR/$NAME" ]] && rm -f "$BIN_DIR/$NAME"
  have systemctl && systemctl --user daemon-reload || true
  refresh_menus
  echo "Uninstalled. Settings and favourites are still in ~/.config/$NAME/"
  exit 0
fi

have python3 || { echo "python3 is required" >&2; exit 1; }

# 1. Plugin: build from source when the toolchain is there, otherwise use the prebuilt binaries.
if have g++ && have x86_64-w64-mingw32-g++ && have make; then
  echo "==> building telemetry plugin"
  make -s -C "$HERE/plugin"
else
  echo "==> using prebuilt telemetry plugin (install g++ and mingw-w64-gcc to build it yourself)"
fi

# 2. Launcher on PATH.
mkdir -p "$BIN_DIR"
chmod +x "$SERVER"
ln -sfn "$SERVER" "$BIN_DIR/$NAME"
echo "==> launcher: $BIN_DIR/$NAME"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "    note: $BIN_DIR is not on your PATH" ;; esac

# 3. systemd user service (optional autostart).
if have systemctl; then
  mkdir -p "$UNIT_DIR"
  sed "s|@SERVER@|$SERVER|" "$HERE/packaging/$NAME.service" > "$UNIT_DIR/$NAME.service"
  systemctl --user daemon-reload
  if [[ $autostart == 1 ]]; then
    systemctl --user enable --now "$NAME.service"
    echo "==> user service enabled and started"
  else
    echo "==> user service installed (enable at login: systemctl --user enable --now $NAME)"
  fi
fi

# 4. Desktop entry.
mkdir -p "$APP_DIR"
sed "s|@SERVER@|$SERVER|" "$HERE/packaging/$NAME.desktop" > "$APP_DIR/$NAME.desktop"
refresh_menus
echo "==> app menu entry: \"ETS2 Local Radio\" ($APP_DIR/$NAME.desktop)"
if ! python3 -c "import PySide6" 2>/dev/null && ! python3 -c "import PyQt6" 2>/dev/null; then
  echo "    note: install PySide6 for the server window (Arch: pyside6, Debian/Ubuntu: python3-pyside6.qtwidgets,"
  echo "          Fedora: python3-pyside6). Without it the menu entry opens the website instead."
fi

# 5. Game plugin.
echo "==> installing the plugin into ETS2/ATS"
python3 "$SERVER" install-plugin || echo "    (re-run later with: $NAME install-plugin --path <game folder>)"

cat <<EOF

Done. Start it from your app menu ("ETS2 Local Radio") or run:  $NAME gui
Settings: ~/.config/$NAME/settings.json  (restart the server after editing)
EOF
