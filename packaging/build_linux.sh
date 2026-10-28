#!/usr/bin/env bash
# Builds the headless Linux server bundle. Run ON Linux (PyInstaller cannot cross-compile):
#   bash packaging/build_linux.sh [version]
# Needs: python3 with venv, binutils, ripgrep (apt install python3-venv binutils ripgrep)
# Output: packaging/dist_app/CodyMCPServer/cody-server and packaging/installer_output/CodyMCPServer-<version>-linux-<arch>.tar.gz
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ARCH="$(uname -m)"

[ -d venv ] || python3 -m venv venv
PY="$ROOT/venv/bin/python"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt pyinstaller

mkdir -p packaging/bin
RG="$(command -v rg || true)"
[ -n "$RG" ] || { echo "rg not found on PATH (apt install ripgrep)"; exit 1; }
cp -f "$RG" packaging/bin/rg
chmod +x packaging/bin/rg
NG="$(command -v ngrok || true)"
[ -z "$NG" ] || { cp -f "$NG" packaging/bin/ngrok; chmod +x packaging/bin/ngrok; }

"$PY" -m PyInstaller packaging/CodyMCPServer.spec --noconfirm --clean \
  --distpath packaging/dist_app --workpath packaging/build

mkdir -p packaging/installer_output
OUT="packaging/installer_output/CodyMCPServer-$VERSION-linux-$ARCH.tar.gz"
rm -f "$OUT"
tar -C packaging/dist_app -czf "$OUT" CodyMCPServer
echo "Done: $OUT"
