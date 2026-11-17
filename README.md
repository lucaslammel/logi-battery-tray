# Logi Battery Tray

A tiny Windows tray app that shows the battery level of your **Logitech wireless
keyboard, mouse and headset**. No G HUB required.

- One tray icon per device (keyboard, mouse, headset)
- Reads batteries straight from the USB dongle using the HID++ 2.0 protocol
- Low-battery notification, charging indicator, follows the Windows light/dark theme
- English by default (or your Windows language), selectable from the tray menu
  (English, Português, Español, 日本語, Русский)
- Optional "Start with Windows", single instance (a second launch does nothing)

> Not affiliated with, endorsed by or sponsored by Logitech. Logitech and G HUB are
> trademarks of their owner.

## Tested with

| Device | Result |
| --- | --- |
| G715 Keyboard | works |
| PRO X Wireless (mouse) | works |
| PRO X Wireless Gaming Headset | works, but the percentage might vary slightly from what is shown in G Hub |

Other Logitech devices that expose a standard HID++ 2.0 battery feature (`0x1004`,
`0x1000`, `0x1001`) should work too. Reports are welcome!

## Run from source

Requires Windows and Python 3.9+.

```
pip install -r requirements.txt
python logi_battery_tray.py
```

Use `pythonw logi_battery_tray.py` to run without a console window.

## Build a standalone .exe

```
pip install pyinstaller
python -m PyInstaller --onefile --noconsole --name LogiBatteryTray logi_battery_tray.py
```

The result is `dist\LogiBatteryTray.exe`. It runs on other Windows 64-bit PCs without
Python. Right-click a tray icon and tick **Start with Windows** (copy the .exe to its
final folder first). Unsigned PyInstaller executables are sometimes flagged by
SmartScreen/antivirus: that is a known false positive.

## Troubleshooting and diagnostics

If a device does not show up, or its percentage looks wrong:

```
python logi_battery_tray.py --diagnose
```

This lists the Logitech HID interfaces and what each one answers (including the raw
battery bytes). Please attach that output when opening an issue, together with the
exact device model and the real battery percentage (from G HUB).

Notes:

- Mice go to sleep after a few seconds without movement and stop answering. The icon
  keeps its last value (tooltip says "no response") for 10 minutes. Keyboards and
  headsets are removed after 20 seconds without an answer. See `GRACE_SECONDS`.
- Headsets that report voltage instead of a percentage need a per-model curve. The
  PRO X Wireless Gaming Headset is calibrated with one data point
  (`HEADSET_ADC_OFFSET_MV`); more measurements would improve it.
- The reading interval is `POLL_SECONDS` (5 s by default).
- Settings (language) are stored in `%APPDATA%\LogiBatteryTray\config.json`.

## License

MIT, see [LICENSE](LICENSE).
