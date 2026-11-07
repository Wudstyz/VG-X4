#!/bin/bash
# Fabrique dist/Atelier-Traceuse-1.0-macOS-arm64.dmg (l'app + un raccourci vers Applications : glisser-déposer).
set -euo pipefail
cd "$(dirname "$0")"
APPNAME="Atelier Traceuse"; OUT="dist/Atelier-Traceuse-1.0-macOS-arm64.dmg"
[ -d "$APPNAME.app" ] || ./build_app.sh
mkdir -p dist; rm -f "$OUT"
T="$(mktemp -d)"
ditto --noextattr --noacl "$APPNAME.app" "$T/$APPNAME.app"
ln -s /Applications "$T/Applications"
hdiutil create -quiet -volname "$APPNAME" -srcfolder "$T" -ov -format UDZO "$OUT"
rm -rf "$T"
echo "OK : $OUT ($(du -sh "$OUT" | cut -f1))"
