#!/bin/bash
# Builds "Claude Usage Monitor.app" into ./dist. Run on a Mac:
#   ./build_mac.sh            build into ./dist
#   ./build_mac.sh --install  build and install into /Applications
# Needs python3 (from python.org, Homebrew, or Xcode Command Line Tools) only
# for the build; the finished app bundles its own Python.
#
# The build runs in a temporary folder, not next to this script. If this folder
# is synced by iCloud (Documents, Desktop), iCloud adds Finder metadata to the
# app while it is being built and codesign refuses to sign it ("resource fork,
# Finder information, or similar detritus not allowed").
set -euo pipefail
cd "$(dirname "$0")"
APP="Claude Usage Monitor"
INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

python3 -m venv .build-venv
source .build-venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller pywebview

WORK="$(mktemp -d "${TMPDIR:-/tmp}/claude-usage-monitor-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

pyinstaller --noconfirm --clean --windowed \
  --name "$APP" \
  --osx-bundle-identifier com.local.claude-usage-monitor \
  --hidden-import webview.platforms.cocoa --hidden-import limits \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" \
  app.py

# Sign (ad hoc) outside any synced folder, then check the signature.
BUILT="$WORK/dist/$APP.app"
xattr -cr "$BUILT"
codesign -s - --force --deep "$BUILT"
codesign --verify --deep --strict "$BUILT"

# Copy out without extended attributes, so the signature stays valid.
rm -rf "dist/$APP.app"
mkdir -p dist
ditto --noextattr --norsrc "$BUILT" "dist/$APP.app"
echo
echo "Built: $(pwd)/dist/$APP.app"

if [ "$INSTALL" = 1 ]; then
  if pgrep -f "/Applications/$APP.app/" >/dev/null; then
    echo "Quitting the running app..."
    osascript -e "quit app \"$APP\"" >/dev/null 2>&1 || true
    sleep 2
  fi
  rm -rf "/Applications/$APP.app"
  ditto --noextattr --norsrc "$BUILT" "/Applications/$APP.app"
  codesign --verify --deep --strict "/Applications/$APP.app"
  echo "Installed: /Applications/$APP.app"
else
  echo "Install it with:  ./build_mac.sh --install"
  echo "            or:  ditto --noextattr \"dist/$APP.app\" \"/Applications/$APP.app\""
fi
