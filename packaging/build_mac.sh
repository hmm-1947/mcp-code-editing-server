#!/usr/bin/env bash
# Builds the macOS app + DMG. Run ON A MAC (PyInstaller cannot cross-compile):
#   bash packaging/build_mac.sh [version]
# Needs: python3 with Tk (python.org installer or `brew install python-tk`), ngrok and rg on PATH
#   brew install ripgrep && brew install ngrok
# Output: packaging/installer_output/CodyMCPServer-<version>-<arch>.dmg
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export APP_VERSION="$VERSION"
ARCH="$(uname -m)"

if [ ! -d venv ]; then
  python3 -m venv venv
fi
PY="$ROOT/venv/bin/python"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt pyinstaller pillow requests

echo "==> Staging bundled tools"
mkdir -p packaging/bin
for tool in ngrok rg; do
  src="$(command -v "$tool" || true)"
  [ -n "$src" ] || { echo "$tool not found on PATH"; exit 1; }
  cp -f "$src" "packaging/bin/$tool"
  chmod +x "packaging/bin/$tool"
done

echo "==> Building icon"
ICONSET="$ROOT/packaging/build/icon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
"$PY" - <<'PYEOF'
from PIL import Image
im = Image.open("assets/icon.ico")
im = im.convert("RGBA").resize((1024, 1024), Image.LANCZOS)
for size in (16, 32, 128, 256, 512):
    im.resize((size, size), Image.LANCZOS).save(f"packaging/build/icon.iconset/icon_{size}x{size}.png")
    im.resize((size * 2, size * 2), Image.LANCZOS).save(f"packaging/build/icon.iconset/icon_{size}x{size}@2x.png")
PYEOF
iconutil -c icns "$ICONSET" -o assets/icon.icns

echo "==> PyInstaller"
"$PY" -m PyInstaller packaging/CodyMCPServer.spec --noconfirm --clean \
  --distpath packaging/dist_app --workpath packaging/build

APP="packaging/dist_app/CodyMCPServer.app"
echo "==> Ad-hoc signing"
codesign --force --deep --sign - "$APP"

echo "==> DMG"
STAGE="$ROOT/packaging/build/dmg"
rm -rf "$STAGE" && mkdir -p "$STAGE" packaging/installer_output
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="packaging/installer_output/CodyMCPServer-$VERSION-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname "Cody MCP Server" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

echo "Done: $DMG"
