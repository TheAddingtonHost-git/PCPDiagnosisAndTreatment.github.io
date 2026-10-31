# TouchDash

TouchDash is a standalone PyObjC Touch Bar control surface for Touch Bar MacBooks. It combines the supplied TouchDash base with macOS-native equivalents for system controls, media, browser context actions, function keys, dialog actions, emoji, colors, audio visualization, and system information.

## What it provides

- System-wide private Touch Bar presentation, with an in-app `--window` fallback.
- Volume and display-brightness controls, including animated full-width sliders and mute.
- Escape, Caps Lock state, Delete, F1-F12, media previous/play/next, sleep, and browser actions.
- Now Playing integration for Music, Spotify, and supported browsers.
- Browser tab picker, per-application configurable widgets, and Accessibility dialog buttons.
- Emoji picker, RGB/HEX/HSL/HSV color tool, clipboard/type actions, and optional Cava visualizer.
- Battery, SSD, RAM, Touch Bar/security-chip, display, and model information.

## Requirements

This app must be built and run on macOS with a Touch Bar. Install Python dependencies with:

```sh
python3 -m pip install -r requirements.txt
```

Optional visualizer:

```sh
brew install cava
```

## Run and build

Run directly:

```sh
python3 touchdash.py
```

If private system-modal APIs are unavailable, test the Touch Bar in the app window:

```sh
python3 touchdash.py --window
```

Build an application bundle:

```sh
python3 setup.py py2app
open dist/TouchDash.app
```

The app creates its configuration at `~/Library/Application Support/TouchDash/config.json` and logs to `~/Library/Logs/TouchDash.log`. Set `TOUCHDASH_CONFIG` or pass `--config PATH` to use another config.

## Permissions

For synthetic keyboard/media events, enable the app under **System Settings → Privacy & Security → Accessibility**. This permission is also needed for frontmost-window titles and dialog buttons. Screen recording may be required by macOS for some window metadata. The private Touch Bar APIs are unsupported by Apple and may change between macOS releases.
