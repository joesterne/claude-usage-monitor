#!/bin/bash
# Builds "Claude Usage Monitor.app" into ./dist. Run on a Mac:  ./build_mac.sh
# Needs python3 (from python.org, Homebrew, or Xcode Command Line Tools) only
# for the build; the finished app bundles its own Python.
set -euo pipefail
cd "$(dirname "$0")"
APP="Claude Usage Monitor"

python3 -m venv .build-venv
source .build-venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller pywebview

pyinstaller --noconfirm --clean --windowed \
  --name "$APP" \
  --osx-bundle-identifier com.local.claude-usage-monitor \
  --hidden-import webview.platforms.cocoa \
  app.py

echo
echo "Built: $(pwd)/dist/$APP.app"
echo "Install it with:  cp -R \"dist/$APP.app\" /Applications/"
