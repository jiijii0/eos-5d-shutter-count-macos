#!/bin/sh
# Builds dist/EOS5D_ShutterCount.app and dist/EOS5D_ShutterCount-macos-<arch>.zip.
# The app's architecture is that of the Python used. Use a uv-managed or python.org
# Python, not Homebrew's (whose Tcl/Tk only runs on the macOS it was built for):
#   uv venv --managed-python --python 3.12 .venv && uv pip install -r requirements.txt
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="${PYTHON:-.venv/bin/python}"
ARCH="$("$PY" -c 'import platform; print(platform.machine())')"
APP=dist/EOS5D_ShutterCount.app

"$PY" -m PyInstaller --noconfirm --clean --windowed \
  --name EOS5D_ShutterCount \
  --osx-bundle-identifier io.github.eos5d-shuttercount \
  --specpath "build/$ARCH" --workpath "build/$ARCH" --distpath dist \
  --collect-binaries libusb_package \
  --hidden-import usb.backend.libusb1 \
  --add-data "$ROOT/fonts:fonts" \
  "$ROOT/shutter_count_app.py"

# PyInstaller's pyusb hook also copies whatever libusb the build machine has (e.g.
# Homebrew's). The app uses the one from libusb-package, so drop the extra copy.
rm -f "$APP/Contents/Frameworks/libusb-1.0.dylib" "$APP/Contents/Resources/libusb-1.0.dylib"

codesign --force --deep --sign - "$APP"   # ad-hoc; Apple Silicon needs a signature
ditto -c -k --keepParent "$APP" "dist/EOS5D_ShutterCount-macos-$ARCH.zip"
echo "Built: $ROOT/$APP ($ARCH) and dist/EOS5D_ShutterCount-macos-$ARCH.zip"
