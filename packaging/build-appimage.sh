#!/bin/sh
# Build OBDruid-<version>-<arch>.AppImage from the PyInstaller folder in dist/OBDruid.
# Usage (repository root):  pyinstaller --noconfirm packaging/obdruid.spec && sh packaging/build-appimage.sh
# Needs: curl (to fetch appimagetool once), file. Output: dist/OBDruid-<version>-<arch>.AppImage
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCH="${ARCH:-$(uname -m)}"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/obdruid/__init__.py")"
APPDIR="$ROOT/build/OBDruid.AppDir"
TOOL="$ROOT/build/appimagetool-$ARCH.AppImage"
OUT="$ROOT/dist/OBDruid-$VERSION-$ARCH.AppImage"

[ -x "$ROOT/dist/OBDruid/obdruid" ] || { echo "dist/OBDruid/obdruid not found - run pyinstaller first" >&2; exit 1; }

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$ROOT/dist/OBDruid" "$APPDIR/usr/lib/obdruid"
install -m 755 "$ROOT/packaging/AppRun" "$APPDIR/AppRun"
cp "$ROOT/packaging/obdruid.desktop" "$APPDIR/obdruid.desktop"
cp "$ROOT/packaging/obdruid.desktop" "$APPDIR/usr/share/applications/obdruid.desktop"
cp "$ROOT/packaging/obdruid.png" "$APPDIR/obdruid.png"
cp "$ROOT/packaging/obdruid.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/obdruid.png"
cp "$ROOT/LICENSE" "$APPDIR/LICENSE"

if [ ! -x "$TOOL" ]; then
    curl -fsSL -o "$TOOL" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$TOOL"
fi
# --appimage-extract-and-run: works in containers and CI runners without FUSE
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT"
chmod +x "$OUT"
echo "Built: $OUT"
