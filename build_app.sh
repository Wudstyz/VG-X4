#!/bin/bash
# Construit « Atelier Traceuse.app » : application autonome (Python embarqué, aucune installation requise).
# Python = « python-build-standalone » (astral-sh), téléchargé une fois et vérifié par SHA-256.
APPNAME="Atelier Traceuse"; EXE="AtelierTraceuse"
PBS_TAG="20261026"
PBS_FILE="cpython-3.14.10+${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_SHA="95eb8c87d16af37c54e9765e31d8c56c0d8ffa9968ea0f704f73a9b83f84e9f9"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-3.14.10%2B${PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
set -euo pipefail
cd "$(dirname "$0")"

# 1. Python autonome (en cache dans ~/Library/Caches/VG-X4-build)
CACHE="$HOME/Library/Caches/VG-X4-build"; mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PBS_FILE" ]; then
  echo "Téléchargement de Python autonome…"
  curl -fsSL -o "$CACHE/$PBS_FILE.part" "$PBS_URL"
  mv "$CACHE/$PBS_FILE.part" "$CACHE/$PBS_FILE"
fi
echo "$PBS_SHA  $CACHE/$PBS_FILE" | shasum -a 256 -c - >/dev/null || { echo "Somme de contrôle incorrecte pour $PBS_FILE"; rm -f "$CACHE/$PBS_FILE"; exit 1; }

# Construit hors du dossier iCloud/Bureau (les attributs de fichier empêcheraient la signature), puis copie.
FINAL="$PWD/$APPNAME.app"
T="$(mktemp -d)"
APP="$T/$APPNAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
tar xzf "$CACHE/$PBS_FILE" -C "$T"                 # → $T/python
mv "$T/python" "$APP/Contents/Resources/python"
PYH="$APP/Contents/Resources/python"
PY="$PYH/bin/python3.14"
SP="$PYH/lib/python3.14/site-packages"

# 2. Bibliothèques Python, installées dans le Python embarqué
"$PY" -m pip install --quiet --disable-pip-version-check --no-warn-script-location --only-binary=:all: -r requirements.txt
# 3. Allègement : tout ce dont l'app n'a pas besoin
rm -rf "$SP"/pip "$SP"/pip-* "$SP"/setuptools* "$SP"/_distutils_hack "$SP"/pkg_resources "$SP"/wheel* 2>/dev/null || true
rm -rf "$PYH"/share "$PYH"/lib/pkgconfig "$PYH"/lib/itcl* "$PYH"/lib/tcl* "$PYH"/lib/tk* "$PYH"/lib/thread* \
       "$PYH"/lib/libtcl* "$PYH"/lib/libtk* "$PYH"/lib/python3.14/tkinter "$PYH"/lib/python3.14/idlelib \
       "$PYH"/lib/python3.14/turtledemo "$PYH"/lib/python3.14/test "$PYH"/lib/python3.14/ensurepip \
       "$PYH"/lib/python3.14/lib-dynload/_tkinter* "$PYH"/lib/python3.14/config-*
find "$APP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SP" -type d \( -name tests -o -name test -o -name PyObjCTest \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$SP" -name 'RECORD' -delete 2>/dev/null || true

# 4. Lanceur (lié à la libpython embarquée) et ressources
if [ ! -f icon.icns ]; then PYTHONPATH="$SP" "$PY" make_icon.py; fi
clang launcher.c -o "$APP/Contents/MacOS/$EXE" -I"$PYH/include/python3.14" -L"$PYH/lib" -lpython3.14 \
  -Wl,-rpath,@executable_path/../Resources/python/lib -mmacosx-version-min=11.0 -Wno-deprecated-declarations
cp app.py grbl.py vec.py shapes.py docimport.py index.html "$APP/Contents/Resources/"
cp icon.icns LICENSE THIRD_PARTY_NOTICES.md "$APP/Contents/Resources/"

# 5. macOS minimal = le plus exigeant des binaires embarqués (calculé, pas deviné)
MINOS=$(find "$APP" -type f \( -name '*.so' -o -name '*.dylib' -o -path '*/MacOS/*' \) -print0 | xargs -0 -n1 sh -c \
  'otool -l "$0" 2>/dev/null | awk "/LC_BUILD_VERSION/{f=1} f&&/minos/{print \$2; f=0}"' | sort -t. -k1,1n -k2,2n | tail -1)
MINOS=${MINOS:-11.0}
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>$APPNAME</string>
<key>CFBundleDisplayName</key><string>$APPNAME</string>
<key>CFBundleExecutable</key><string>$EXE</string>
<key>CFBundleIdentifier</key><string>local.vgx4</string>
<key>CFBundleIconFile</key><string>icon</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSHighResolutionCapable</key><true/>
<key>LSMinimumSystemVersion</key><string>$MINOS</string>
</dict></plist>
EOF

# 6. Pré-compilation (le bundle signé ne doit plus être modifié), puis signature ad hoc de chaque binaire
"$PY" -m compileall -q "$APP/Contents/Resources" "$PYH/lib" >/dev/null 2>&1 || true
rm -rf "$PYH/bin" "$PYH/include"          # plus utiles : le lanceur est déjà compilé
xattr -cr "$APP"
find "$APP" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 | xargs -0 -n20 codesign --force -s - >/dev/null 2>&1 || true
codesign --force -s - "$APP" 2>&1 | tail -4
rm -rf "$FINAL" "$PWD/VG-X4.app"      # supprime aussi l'ancien nom
ditto --noextattr --noacl "$APP" "$FINAL"
rm -rf "$T"
echo "OK : $FINAL  (macOS $MINOS minimum, $(du -sh "$FINAL" | cut -f1))"
