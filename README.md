# EOS 5D Shutter Count for macOS

Reads the shutter count of the original **Canon EOS 5D** (2005) over USB. No drivers, no admin rights.

## Use

1. Download the zip for your Mac from [Releases](../../releases): `arm64` for Apple Silicon, `x86_64` for Intel. It needs macOS 11 or later (10.15 on Intel).
2. Unzip it, then right-click the app → **Open** (it isn't notarised).
3. Connect the camera and switch it on. Quit Photos and Image Capture.
4. Click **CHECK**.

## Build

```sh
uv venv --managed-python --python 3.12 .venv && uv pip install -r requirements.txt
./build_app.sh                       # or run directly: .venv/bin/python shutter_count_app.py
```

## Credits

macOS port of [Blianahui/EOS_5D_Shutter_Count](https://github.com/Blianahui/EOS_5D_Shutter_Count) (Windows).
GPL-3.0. Fonts: Press Start 2P and VT323 (OFL). Not affiliated with Canon. Use at your own risk.
