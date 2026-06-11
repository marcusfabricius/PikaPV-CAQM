#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${1:-$(tr -d '[:space:]' < "$ROOT/VERSION")}"
VENV="$ROOT/.build-venv-linux"
PYTHON="$VENV/bin/python"
DIST="$ROOT/dist/linux"
WORK="$ROOT/build/linux"
OUTPUT="$ROOT/installer-output"
STAGE="$ROOT/build/linux-deb"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "VERSION must look like 1.2.3. Current value: $VERSION" >&2
  exit 1
fi

rm -rf "$DIST" "$WORK" "$STAGE"
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
  "packaging/PikaPV.spec"

PORTABLE="$OUTPUT/PikaPV-Linux-$VERSION-$ARCH.tar.gz"
rm -f "$PORTABLE"
tar -C "$DIST" -czf "$PORTABLE" PikaPV

mkdir -p \
  "$STAGE/DEBIAN" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/lib/pikapv" \
  "$STAGE/usr/share/applications"
cp -a "$DIST/PikaPV/." "$STAGE/usr/lib/pikapv/"

cat > "$STAGE/usr/bin/pikapv" <<'EOF'
#!/bin/sh
exec /usr/lib/pikapv/PikaPV "$@"
EOF
chmod 0755 "$STAGE/usr/bin/pikapv"

cat > "$STAGE/usr/share/applications/pikapv.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=PikaPV
Comment=Solar-cell measurement interface
Exec=/usr/bin/pikapv
Terminal=true
Categories=Science;Engineering;
EOF

cat > "$STAGE/DEBIAN/control" <<EOF
Package: pikapv
Version: $VERSION
Section: science
Priority: optional
Architecture: $ARCH
Maintainer: CAQM Group
Depends: libc6
Description: PikaPV solar-cell measurement interface
 Local browser-based interface for DC, impedance, C-V, and live lock-in measurements.
EOF

DEB="$OUTPUT/PikaPV_${VERSION}_${ARCH}.deb"
rm -f "$DEB"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"

echo "Linux package: $DEB"
echo "Linux portable archive: $PORTABLE"

