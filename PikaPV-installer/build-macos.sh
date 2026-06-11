#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-$(tr -d '[:space:]' < "$ROOT/VERSION")}"
VENV="$ROOT/.build-venv-macos"
PYTHON="$VENV/bin/python"
DIST="$ROOT/dist/macos"
WORK="$ROOT/build/macos"
OUTPUT="$ROOT/installer-output"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "VERSION must look like 1.2.3. Current value: $VERSION" >&2
  exit 1
fi

rm -rf "$DIST" "$WORK"
mkdir -p "$OUTPUT"

python3 -m venv "$VENV"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$ROOT/src/requirements.txt"
"$PYTHON" -m pip install -r "$ROOT/requirements-build.txt"

cd "$ROOT"
"$PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST" \
  --workpath "$WORK" \
  "packaging/PikaPV-macos.spec"

APP="$DIST/PikaPV.app"
if [[ -n "${PIKAPV_CODESIGN_IDENTITY:-}" ]]; then
  codesign --deep --force --options runtime --sign "$PIKAPV_CODESIGN_IDENTITY" "$APP"
fi

DMG="$OUTPUT/PikaPV-macOS-$VERSION.dmg"
ZIP="$OUTPUT/PikaPV-macOS-$VERSION.zip"
rm -f "$DMG" "$ZIP"
hdiutil create -volname "PikaPV" -srcfolder "$APP" -ov -format UDZO "$DMG"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo "macOS app: $APP"
echo "macOS installer image: $DMG"
echo "macOS portable archive: $ZIP"

