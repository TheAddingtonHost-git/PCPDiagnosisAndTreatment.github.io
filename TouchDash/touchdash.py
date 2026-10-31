#!/usr/bin/env python3
"""
TouchDash - standalone macOS Touch Bar app (PyObjC), no BetterTouchTool needed.

Base: the original TouchDash module (Chrome/media page, system-info page, animated volume strip, worker thread).
Ported on top of it from the Linux touchbar daemon, using macOS equivalents:

  Linux daemon                          macOS equivalent used here
  ------------------------------------  ----------------------------------------------------------
  ESC button (esc_mode "T1")            CGEvent key 53                       (config: esc_mode)
  VOL / BRI buttons -> full-bar slider  animated expanding strips (CoreAudio + DisplayServices)
  FN button + F1-F12 row                CGEvent F-keys; button OFF by default (config: show_fn_button)
  EXPAND -> PREV / PLAY / NEXT          media-key events (NX_KEYTYPE_*)
  CAVA visualizer + colour picker       `cava` subprocess (raw ascii) drawn natively + popover picker
  EMOJI categories / grid               NSScrubber popover, typed with CGEventKeyboardSetUnicodeString
  COLORS tool (RGB, HEX/RGB/HSL/HSV)    popover with sliders, copy to NSPasteboard / type into the app
  CAPS lock indicator                   CGEventSourceFlagsState
  KWin + AT-SPI dialog buttons          Accessibility API (AXSheet / AXDialog buttons, AXPress)
  client widgets + shortcuts (ctrl+x)   per-app "contexts" in config.json with cmd-based shortcuts
  SLEEP                                 pmset sleepnow
  font.ttf                              SF Pro (the macOS system font) - configurable

Settings live in  ~/Library/Application Support/TouchDash/config.json  (created on first run; --config PATH overrides).
Uses PRIVATE Touch Bar APIs (system-modal bar + control-strip button), so it must run from an .app bundle
(see README.md / setup.py) and may break on future macOS versions.
"""
import colorsys
import ctypes
import json
import logging
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

LOG_PATH = os.path.expanduser("~/Library/Logs/TouchDash.log")
APP_DIR = os.path.expanduser("~/Library/Application Support/TouchDash")
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
except OSError:
    pass
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("touchdash")
if sys.stderr.isatty():  # running from Terminal: also print the log there
    log.addHandler(logging.StreamHandler(sys.stderr))
sys.excepthook = lambda *a: log.error("uncaught:\n%s", "".join(traceback.format_exception(*a)))


def die(msg):
    """log + show a dialog, so a failed start is never silent"""
    log.error(msg)
    print(msg, file=sys.stderr)
    try:
        subprocess.run(["osascript", "-e", f"display alert \"TouchDash can't start\" message {json.dumps(msg)}"],
                       timeout=60)
    except Exception:
        pass
    sys.exit(1)


def guard1(fn):
    """log exceptions from UI callbacks instead of letting PyObjC swallow them"""
    def w(self, a):
        try:
            return fn(self, a)
        except Exception:
            log.exception("error in %s", fn.__name__)
    w.__name__ = fn.__name__
    return w


def guard2(fn):
    def w(self, a, b):
        try:
            return fn(self, a, b)
        except Exception:
            log.exception("error in %s", fn.__name__)
    w.__name__ = fn.__name__
    return w


# ======================= config =======================
DEFAULT_CONFIG = {
    # SF Pro == the macOS system font. Any other installed font name works too ("Helvetica Neue", "SFProText-Regular"...).
    "font": {"family": "SF Pro", "size": 13, "now_playing_size": 14, "weight": "regular"},
    "colors": {"surface": [28, 28, 30], "surface_active": [60, 60, 65], "accent": [90, 150, 255],
               "track_bg": [18, 18, 20], "text": [242, 242, 247], "caps_on": [52, 199, 89], "danger": [255, 69, 58]},
    "esc_mode": "T1",                 # "T1" = show an esc button on the bar, anything else = none
    "show_fn_button": False,          # the fn button (and its F1-F12 row) is gone unless you set this to true
    "show_caps_indicator": True,
    "show_brightness": True,
    "click_sound": False,
    # left -> right order of the expanded page. Valid: COLORS EMOJI CAVA PREV PLAY NEXT SLEEP
    "expanded_items": ["COLORS", "EMOJI", "CAVA", "PREV", "PLAY", "NEXT"],
    "widths": {"esc": 64, "title": 190, "np": 210, "small": 44, "caps": 40, "vol": 76, "expand": 50},
    "dialogs": {"enabled": True},     # show the buttons of the frontmost app's sheet / dialog on the bar
    # cava needs `brew install cava`; to visualise system audio route it through a loopback device (e.g. BlackHole)
    "cava": {"enabled": True, "bars": 64, "input_method": "portaudio", "source": "auto", "width": 300},
    # per-app widgets. key = bundle id, "browser" (any supported browser) or "default".
    # {"builtin": tabs|np|newtab|closetab|reload}  or  {"text": ..., "shortcut": "cmd+shift+n", "width": 100}
    "contexts": {
        "browser": [{"builtin": "tabs"}, {"builtin": "np"}, {"builtin": "newtab"}, {"builtin": "closetab"},
                    {"builtin": "reload"}],
        "default": [{"builtin": "np"}],
        "com.apple.finder": [{"text": "New Folder", "shortcut": "cmd+shift+n", "width": 120},
                             {"text": "Copy", "shortcut": "cmd+c", "width": 80},
                             {"text": "Paste", "shortcut": "cmd+v", "width": 80},
                             {"text": "Trash", "shortcut": "cmd+backspace", "width": 80},
                             {"builtin": "np"}],
    },
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def config_path():
    if "--config" in sys.argv:
        try:
            return os.path.expanduser(sys.argv[sys.argv.index("--config") + 1])
        except IndexError:
            pass
    return os.environ.get("TOUCHDASH_CONFIG") or os.path.join(APP_DIR, "config.json")


def load_config():
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(APP_DIR, exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            log.info("wrote default config to %s", path)
    except Exception:
        log.exception("could not create config")
    cfg = DEFAULT_CONFIG
    try:
        with open(path) as f:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(f))
    except Exception:
        log.exception("bad config %s - using defaults", path)
    return cfg


CFG = load_config()

try:
    import objc
    import Quartz
    from AppKit import (
        NSApplication, NSBezierPath, NSButton, NSColor, NSCompositingOperationSourceOver, NSCustomTouchBarItem,
        NSEvent, NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSImage, NSImageLeft,
        NSLineBreakByTruncatingTail, NSMutableParagraphStyle, NSParagraphStyleAttributeName, NSPasteboard,
        NSPasteboardTypeString, NSPopoverTouchBarItem, NSRunningApplication, NSScreen, NSSegmentedControl,
        NSSlider, NSSliderTouchBarItem, NSSound, NSTextAlignmentCenter, NSTouchBar, NSTouchBarItem, NSView,
        NSWorkspace,
    )
    from Foundation import (
        NSAttributedString, NSData, NSMakeRect, NSNotificationCenter, NSObject, NSString, NSURL,
    )
    from PyObjCTools import AppHelper
except ImportError as e:
    die(f"A PyObjC module is missing ({e}).\nInstall with:  pip install pyobjc py2app")

try:  # emoji picker: without NSScrubber the emoji button is simply left out
    from AppKit import NSScrubber, NSScrubberFlowLayout, NSScrubberTextItemView
    HAS_SCRUBBER = True
except ImportError:
    HAS_SCRUBBER = False
    log.warning("NSScrubber unavailable: emoji picker disabled")

try:  # optional: dialog buttons via the Accessibility API
    from ApplicationServices import (
        AXIsProcessTrusted, AXUIElementCopyAttributeValue, AXUIElementCreateApplication, AXUIElementPerformAction,
        AXUIElementSetMessagingTimeout,
    )
except ImportError:
    AXIsProcessTrusted = AXUIElementCopyAttributeValue = AXUIElementCreateApplication = None
    AXUIElementPerformAction = AXUIElementSetMessagingTimeout = None

try:
    from Foundation import NSURLVolumeAvailableCapacityForImportantUsageKey as K_AVAIL
    from Foundation import NSURLVolumeTotalCapacityKey as K_TOTAL
except ImportError:
    K_AVAIL, K_TOTAL = "NSURLVolumeAvailableCapacityForImportantUsageKey", "NSURLVolumeTotalCapacityKey"

try:  # optional: without it Spotify / Music / browser tabs are skipped but the app still starts
    from ScriptingBridge import SBApplication
except ImportError:
    SBApplication = None
    log.warning("pyobjc-framework-ScriptingBridge missing: pip install pyobjc-framework-ScriptingBridge")

# ======================= settings (from config) =======================


def _rgb(name):
    return tuple(int(x) for x in CFG["colors"][name][:3])


BG, BG_ACTIVE, ACCENT, TRACK = _rgb("surface"), _rgb("surface_active"), _rgb("accent"), _rgb("track_bg")
FG, CAPS_ON, DANGER = _rgb("text"), _rgb("caps_on"), _rgb("danger")
FG_DIM = tuple(int(a * 0.55 + b * 0.45) for a, b in zip(FG, BG))
FONT_SIZE = CFG["font"]["size"]               # info buttons / title
NP_FONT_SIZE = CFG["font"]["now_playing_size"]  # now playing
ICON_SIZE = 30          # app icon / album art, px
W = CFG["widths"]
W_ESC, W_TITLE, W_NP, W_SMALL, W_CAPS, W_VOL, W_EXPAND = (W["esc"], W["title"], W["np"], W["small"], W["caps"],
                                                           W["vol"], W["expand"])
EXP_SLIDER_W, EXP_MUTE_W = 640, 64   # expanded strip: slider + mute button widths
AUTO_CLOSE = 4.0        # s of no interaction before the expanded strip closes itself
FLEX = "NSTouchBarItemIdentifierFlexibleSpace"
PAUSED_LABEL, NOT_PLAYING, NOT_BROWSER, EMPTY = "Paused", "Not playing", "Typing...", "Loading..."
TITLE_SUFFIXES = [" - Google Chrome", " - Brave", " - Microsoft Edge", " - Vivaldi", " - Chromium"]
MEDIA_SITES = ["music.youtube.com", "youtube.com", "soundcloud.com", "spotify.com", "bandcamp.com", "music.apple.com",
               "deezer.com", "tidal.com", "pandora.com", "twitch.tv", "vimeo.com", "mixcloud.com", "audiomack.com"]
CLICK_WINDOW = 10       # s: a click this close to browser audio stopping counts as "paused"
PAUSED_HOLD = 1800      # s: how long an assumed pause is shown before "Not playing"
TICK = 0.25             # worker loop interval (cheap: no process spawning in the hot path)
MAX_TABS = 9
# ========================================================

PLAYERS = {"Spotify": "com.spotify.client", "Music": "com.apple.Music"}
BROWSERS = {"Google Chrome": "com.google.Chrome", "Brave Browser": "com.brave.Browser",
            "Microsoft Edge": "com.microsoft.edgemac", "Vivaldi": "com.vivaldi.Vivaldi",
            "Chromium": "org.chromium.Chromium", "Arc": "company.thebrowser.Browser"}
BUNDLE_TO_BROWSER = {v: k for k, v in BROWSERS.items()}
ALL_BUNDLES = {**PLAYERS, **BROWSERS}

DEFAULT_EMOJI_DATA = {
    "smileys": list("\U0001F600\U0001F603\U0001F604\U0001F601\U0001F606\U0001F605\U0001F923\U0001F602\U0001F642\U0001F643\U0001F609\U0001F60A\U0001F607\U0001F970\U0001F60D\U0001F929\U0001F618\U0001F617\U0001F61A\U0001F619\U0001F60B\U0001F61B\U0001F61C\U0001F92A\U0001F61D\U0001F911\U0001F917\U0001F92D\U0001F92B\U0001F914\U0001F610\U0001F644\U0001F634\U0001F971\U0001F637\U0001F912\U0001F975\U0001F976\U0001F622\U0001F621\U0001F92C\U0001F631\U0001F973\U0001F60E\U0001F92F"),
    "animals": list("\U0001F436\U0001F431\U0001F42D\U0001F439\U0001F430\U0001F98A\U0001F43B\U0001F43C\U0001F428\U0001F42F\U0001F981\U0001F42E\U0001F437\U0001F438\U0001F435\U0001F648\U0001F649\U0001F64A\U0001F414\U0001F427\U0001F426\U0001F424\U0001F986\U0001F985\U0001F989\U0001F987\U0001F43A\U0001F417\U0001F434\U0001F984\U0001F41D\U0001F41B\U0001F98B\U0001F40C\U0001F41E\U0001F41C\U0001F422\U0001F40D\U0001F419\U0001F991\U0001F420\U0001F42C\U0001F433\U0001F418"),
    "food": list("\U0001F34F\U0001F34E\U0001F350\U0001F34A\U0001F34B\U0001F34C\U0001F349\U0001F347\U0001F353\U0001FAD0\U0001F352\U0001F351\U0001F96D\U0001F34D\U0001F965\U0001F95D\U0001F345\U0001F951\U0001F346\U0001F966\U0001F33D\U0001F955\U0001F9C0\U0001F355\U0001F354\U0001F35F\U0001F32D\U0001F32E\U0001F959\U0001F37F\U0001F369\U0001F36A\U0001F382\U0001F370\U0001F36B\U0001F366\U0001F375\U0001F37A\U0001F377\U0001F964"),
    "activities": list("\U000026BD\U0001F3C0\U0001F3C8\U000026BE\U0001F3BE\U0001F3D0\U0001F3C9\U0001F3B1\U0001F3D3\U0001F3F8\U0001F94A\U0001F94B\U000026F3\U0001F3A3\U0001F3BF\U0001F3C2\U0001F3CB\U0001F938\U0001F93A\U0001F3C7\U0001F3C4\U0001F6B4\U0001F3C6\U0001F947\U0001F3AE\U0001F3B2\U0001F3AF\U0001F3B3\U0001F3B8\U0001F3A8"),
    "travel": list("\U00002708\U0001F697\U0001F695\U0001F699\U0001F68C\U0001F693\U0001F691\U0001F692\U0001F6B2\U0001F6F5\U0001F680\U0001F6F3\U000026F5\U0001F682\U0001F686\U0001F5FD\U0001F5FC\U0001F3F0\U0001F3DD\U0001F3D4\U000026F0\U0001F30B\U0001F3D5\U0001F3D6\U0001F30D\U0001F30E\U0001F30F\U0001F9ED\U0001F5FA\U0001F6EB"),
    "objects": list("\U0001F4A1\U0001F526\U0001F56F\U0001F4F1\U0001F4BB\U00002328\U0001F5A5\U0001F5A8\U0001F4F7\U0001F3A5\U0001F4FA\U0001F4FB\U000023F0\U0000231A\U0001F50B\U0001F50C\U0001F9F0\U0001F527\U0001F528\U0001FA9B\U0001F529\U0001F9F2\U0001F512\U0001F511\U0001F4DA\U0001F4D6\U0000270F\U0001F58A\U0001F4CC\U0001F381"),
    "symbols": list("\U00002764\U0001F9E1\U0001F49B\U0001F49A\U0001F499\U0001F49C\U0001F5A4\U0001F90D\U0001F90E\U0001F494\U00002763\U0001F495\U0001F49E\U0001F493\U0001F497\U0001F496\U00002728\U00002B50\U0001F31F\U0001F4AB\U0001F525\U0001F4AF\U00002705\U0000274C\U00002753\U00002757\U000026A0\U0000267B\U0001F51E\U0001F197"),
    "flags": list("\U0001F3F3\U0001F3F4\U0001F3C1\U0001F6A9") + ["\U0001F3F3\uFE0F\u200D\U0001F308", "\U0001F1FA\U0001F1F8", "\U0001F1EC\U0001F1E7", "\U0001F1E8\U0001F1E6", "\U0001F1E6\U0001F1FA", "\U0001F1E9\U0001F1EA", "\U0001F1EB\U0001F1F7", "\U0001F1EF\U0001F1F5", "\U0001F1EE\U0001F1F3", "\U0001F1E7\U0001F1F7"],
}
EMOJI_CATEGORY_LABELS = [("smileys", "Smileys"), ("animals", "Animals"), ("food", "Food"), ("activities", "Activities"), ("travel", "Travel"), ("objects", "Objects"), ("symbols", "Symbols"), ("flags", "Flags")]

COLOR_FORMATS = ["HEX", "RGB", "HSL", "HSV"]
NX_PLAY, NX_NEXT, NX_PREV = 16, 17, 18


def fcc(s):
    return int.from_bytes(s.encode(), "big")


PS_PLAYING, PS_PAUSED = fcc("kPSP"), fcc("kPSp")


def color(rgb, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, a)


def run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def osa(script):
    """fire-and-forget AppleScript (used for rare actions: switch tab, new tab, reload)"""
    threading.Thread(target=lambda: run(["osascript", "-e", script], 8), daemon=True).start()


def find_exe(name):
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):  # an .app launched from Finder has a minimal PATH
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


# ======================= fonts (SF Pro) =======================
WEIGHTS = {"ultralight": -0.8, "thin": -0.6, "light": -0.4, "regular": 0.0, "medium": 0.23, "semibold": 0.3,
           "bold": 0.4, "heavy": 0.56, "black": 0.62}
SYSTEM_FAMILIES = {"sfpro", "sfprotext", "sfprodisplay", "system", "sf", "sanfrancisco", ".sfnstext", ".applesystemuifont"}
_font_cache = {}


def make_font(size, weight=None):
    """"SF Pro" -> the system font, which IS SF Pro (Text below 20pt, Display above, chosen by macOS).
    Any other family name is looked up by name and falls back to the system font."""
    key = (size, weight)
    if key in _font_cache:
        return _font_cache[key]
    fam = str(CFG["font"].get("family", "SF Pro"))
    w = WEIGHTS.get(str(weight or CFG["font"].get("weight", "regular")).lower(), 0.0)
    f = None
    if fam.lower().replace(" ", "").replace("-", "") not in SYSTEM_FAMILIES:
        f = NSFont.fontWithName_size_(fam, size)
        if f is None:
            log.warning("font %r not found, using SF Pro (system font)", fam)
    if f is None:
        f = NSFont.systemFontOfSize_weight_(size, w)
    _font_cache[key] = f
    return f


# ======================= low-level helpers =======================
libc = ctypes.CDLL("/usr/lib/libc.dylib")


def sysctl_str(name):
    size = ctypes.c_size_t(0)
    if libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0) != 0:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0)
    return buf.value.decode(errors="ignore")


def sysctl_int(name):
    v, size = ctypes.c_int(0), ctypes.c_size_t(4)
    return v.value if libc.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(size), None, 0) == 0 else None


try:  # brightness (private DisplayServices, works on the built-in display)
    _ds = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
    _ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
    _ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
except Exception:
    _ds = None


def builtin_display():
    try:
        _, ids, _ = Quartz.CGGetOnlineDisplayList(16, None, None)
        for d in ids:
            if Quartz.CGDisplayIsBuiltin(d):
                return d
    except Exception:
        pass
    return Quartz.CGMainDisplayID()


def get_brightness():
    if not _ds:
        return None
    v = ctypes.c_float(0)
    return v.value if _ds.DisplayServicesGetBrightness(builtin_display(), ctypes.byref(v)) == 0 else None


def set_brightness(x):
    if _ds:
        _ds.DisplayServicesSetBrightness(builtin_display(), ctypes.c_float(max(0.0, min(1.0, x))))


# ---- system volume via CoreAudio (no process spawning, so dragging the slider is instant) ----
try:
    _ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

    class _Addr(ctypes.Structure):
        _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

    _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    _ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.c_void_p]
    _ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr)]
    _ca.AudioObjectHasProperty.restype = ctypes.c_ubyte
except Exception:
    _ca = None


def _ca_get(obj, sel, scope, ctype):
    addr = _Addr(fcc(sel), fcc(scope), 0)
    val, size = ctype(0), ctypes.c_uint32(ctypes.sizeof(ctype))
    ok = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val)) == 0
    return val.value if ok else None


def _ca_set(obj, sel, scope, ctype, value):
    addr = _Addr(fcc(sel), fcc(scope), 0)
    val = ctype(value)
    return _ca.AudioObjectSetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.sizeof(ctype), ctypes.byref(val)) == 0


def _out_device():
    return _ca_get(1, "dOut", "glob", ctypes.c_uint32) if _ca else None


def get_volume():
    """(volume 0..1, muted) of the default output device, or None"""
    dev = _out_device()
    if not dev:
        return None
    v = _ca_get(dev, "vmvc", "outp", ctypes.c_float)
    if v is None:
        return None
    m = _ca_get(dev, "mute", "outp", ctypes.c_uint32)
    return round(v, 3), bool(m)


def set_volume(x):
    dev = _out_device()
    if dev:
        _ca_set(dev, "vmvc", "outp", ctypes.c_float, max(0.0, min(1.0, x)))
        if x > 0 and _ca_get(dev, "mute", "outp", ctypes.c_uint32):
            _ca_set(dev, "mute", "outp", ctypes.c_uint32, 0)


def set_mute(flag):
    dev = _out_device()
    if dev:
        _ca_set(dev, "mute", "outp", ctypes.c_uint32, 1 if flag else 0)


def symbol(name):
    try:
        return NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    except Exception:
        return None


# ---- synthetic input (replaces the daemon's uinput / pyautogui / xdotool layer) ----
def post_media_key(key=NX_PLAY):
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (key << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, data1, -1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())


# ANSI virtual key codes
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
    "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51, "esc": 53, "escape": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
    "f11": 103, "f12": 111,
}
MODS = {"cmd": Quartz.kCGEventFlagMaskCommand, "command": Quartz.kCGEventFlagMaskCommand,
        "shift": Quartz.kCGEventFlagMaskShift, "alt": Quartz.kCGEventFlagMaskAlternate,
        "opt": Quartz.kCGEventFlagMaskAlternate, "option": Quartz.kCGEventFlagMaskAlternate,
        "ctrl": Quartz.kCGEventFlagMaskControl, "control": Quartz.kCGEventFlagMaskControl}


def press_key(code, flags=0):
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def press_shortcut(spec):
    """"cmd+shift+n" -> key event. (ctrl+... shortcuts from the Linux config need cmd+ here.)"""
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        return
    flags = 0
    for m in parts[:-1]:
        flags |= MODS.get(m, 0)
    code = KEYCODES.get(parts[-1])
    if code is None:
        log.warning("unknown key in shortcut %r", spec)
        return
    press_key(code, flags)


def copy_to_clipboard(text):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def type_text(text):
    """type text (emoji included) into the frontmost app; falls back to clipboard + cmd+v"""
    try:
        n = len(text.encode("utf-16-le")) // 2
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, n, text)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    except Exception:
        log.exception("unicode typing failed, pasting instead")
        copy_to_clipboard(text)
        press_shortcut("cmd+v")


def click_sound():
    if CFG.get("click_sound"):
        try:
            s = NSSound.soundNamed_("Tink")
            if s:
                s.stop()
                s.play()
        except Exception:
            pass


def caps_lock_on():
    try:
        return bool(Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateCombinedSessionState)
                    & Quartz.kCGEventFlagMaskAlphaShift)
    except Exception:
        return False


# private Touch Bar bits
try:
    _dfr = ctypes.CDLL("/System/Library/PrivateFrameworks/DFRFoundation.framework/DFRFoundation")
    _dfr.DFRElementSetControlStripPresenceForIdentifier.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    _dfr.DFRSystemModalShowsCloseBoxWhenFrontMost.argtypes = [ctypes.c_bool]
except Exception:
    _dfr = None


def present_modal(bar, tray_id):
    for sel in ("presentSystemModalTouchBar_placement_systemTrayItemIdentifier_",
                "presentSystemModalFunctionBar_placement_systemTrayItemIdentifier_"):
        if not hasattr(NSTouchBar, sel):
            log.warning("selector missing: %s", sel)
            continue
        for ident in (tray_id, None):
            try:
                getattr(NSTouchBar, sel)(bar, 1, ident)
                log.info("presented via %s (tray id: %s)", sel, ident)
                return True
            except Exception:
                log.exception("%s failed (tray id: %s)", sel, ident)
    return False


def dismiss_modal(bar):
    for sel in ("dismissSystemModalTouchBar_", "dismissSystemModalFunctionBar_"):
        if hasattr(NSTouchBar, sel):
            try:
                getattr(NSTouchBar, sel)(bar)
            except Exception:
                log.exception("%s failed", sel)
            return


# ======================= colour tool + cava (ported from the daemon) =======================
def format_color(rgb, fmt):
    r, g, b = [max(0, min(255, int(c))) for c in rgb[:3]]
    if fmt == "HEX":
        return "#%02X%02X%02X" % (r, g, b)
    if fmt == "RGB":
        return f"rgb({r}, {g}, {b})"
    if fmt == "HSL":
        h, l, sat = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        return f"hsl({int(round(h * 360)) % 360}, {int(round(sat * 100))}%, {int(round(l * 100))}%)"
    h, sat, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return f"hsv({int(round(h * 360)) % 360}, {int(round(sat * 100))}%, {int(round(v * 100))}%)"


def load_json(name, default):
    try:
        with open(os.path.join(APP_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, data):
    try:
        with open(os.path.join(APP_DIR, name), "w") as f:
            json.dump(data, f)
    except Exception:
        log.exception("saving %s failed", name)


def cava_preset(idx):
    if idx == 0:
        return {"type": "solid", "val": list(ACCENT)}
    if idx == 1:
        return {"type": "solid", "val": [255, 60, 60]}
    if idx == 2:
        return {"type": "solid", "val": [60, 255, 120]}
    if idx == 3:
        return {"type": "gradient", "val": [[255, 180, 0], [255, 40, 40]]}
    return {"type": "gradient", "val": [[40, 255, 200], [40, 80, 255]]}


def sanitize_cava_cfg(cfg):
    def rgb(c):
        v = [max(0, min(255, int(x))) for x in list(c)[:3]]
        if len(v) < 3:
            raise ValueError("need 3 channels")
        return v
    try:
        if cfg.get("type") == "gradient":
            return {"type": "gradient", "val": [rgb(cfg["val"][0]), rgb(cfg["val"][1])]}
        return {"type": "solid", "val": rgb(cfg["val"])}
    except Exception:
        return None


class CavaEngine:
    """runs `cava` with raw ascii output (exactly like the daemon) and keeps the latest bars"""

    def __init__(self, on_frame):
        c = CFG["cava"]
        self.enabled, self.n, self.on_frame = bool(c.get("enabled", True)), int(c.get("bars", 64)), on_frame
        self.lock = threading.Lock()
        self.bars = [0] * self.n
        self.proc, self.gen = None, 0
        self.exe = find_exe("cava") if self.enabled else None
        if self.enabled and not self.exe:
            log.warning("cava not found (brew install cava): visualizer stays flat")
        self.cfg = sanitize_cava_cfg(load_json("cava_config.json", {})) or cava_preset(0)

    def save(self):
        save_json("cava_config.json", self.cfg)

    def colors(self, n):
        cfg = self.cfg
        if cfg["type"] != "gradient":
            return [tuple(cfg["val"])] * n
        c1, c2 = cfg["val"]
        out = []
        for i in range(n):
            p = i / max(1, n - 1)
            out.append(tuple(int(c1[k] + (c2[k] - c1[k]) * p) for k in range(3)))
        return out

    def snapshot(self):
        with self.lock:
            return list(self.bars)

    def start(self):
        if not self.exe or self.proc is not None:
            return
        self.gen += 1
        threading.Thread(target=self._loop, args=(self.gen,), daemon=True).start()

    def stop(self):
        self.gen += 1
        p, self.proc = self.proc, None
        if p:
            try:
                p.terminate()
            except Exception:
                pass
        with self.lock:
            self.bars = [0] * self.n

    def _loop(self, gen):
        c = CFG["cava"]
        conf = (f"[general]\nbars = {self.n}\nframerate = 50\n"
                f"[input]\nmethod = {c.get('input_method', 'portaudio')}\nsource = {c.get('source', 'auto')}\n"
                "[output]\nmethod = raw\nraw_target = /dev/stdout\ndata_format = ascii\nascii_max_range = 100\n")
        fd, path = tempfile.mkstemp(suffix=".conf")
        proc = None
        try:
            with os.fdopen(fd, "w") as f:
                f.write(conf)
            proc = subprocess.Popen([self.exe, "-p", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1)
            self.proc = proc
            last = 0.0
            for line in proc.stdout:
                if gen != self.gen:
                    break
                vals = line.strip().split(";")[:-1]
                if len(vals) == self.n:
                    try:
                        bars = [int(v) for v in vals]
                    except ValueError:
                        continue
                    with self.lock:
                        self.bars = bars
                    now = time.time()
                    if now - last > 0.033:
                        last = now
                        self.on_frame()
        except Exception:
            log.exception("cava failed")
        finally:
            if proc:
                if self.proc is proc:
                    self.proc = None
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                os.remove(path)
            except Exception:
                pass


CAVA = None  # set by Dash


# ======================= dialog buttons (Accessibility API; replaces KWin + AT-SPI) =======================
def clean_button_label(label):
    label = (label or "").strip()
    label = re.sub(r"\s*\(&?[A-Za-z]\)\s*$", "", label)
    return label.replace("&", "").replace("\u2026", "").strip()


def classify_button(label):
    low = label.lower().replace("\u2019", "'")
    words = set(re.findall(r"[a-z']+", low))
    if ("don't save" in low or "do not save" in low or "discard" in low or
            words & {"delete", "remove", "deny", "erase", "overwrite", "trash"}):
        return "destructive"
    if words & {"cancel", "close", "reject", "no", "dismiss", "later", "abort", "never", "skip"} or "not now" in low:
        return "reject"
    if words & {"authenticate", "ok", "yes", "save", "allow", "continue", "accept", "unlock", "grant", "confirm",
                "apply", "open", "install", "update", "restart", "shutdown", "logout", "log", "retry", "replace"}:
        return "accept"
    return "action"


STD_WORDS = frozenset({
    "save", "cancel", "ok", "open", "yes", "no", "close", "apply", "discard", "accept", "reject", "choose", "select",
    "print", "authenticate", "continue", "retry", "ignore", "replace", "overwrite", "delete", "remove", "quit",
    "logout", "shutdown", "restart", "install", "update", "allow", "deny", "grant", "confirm", "done", "finish",
    "create", "export", "import", "upload", "send", "stop", "abort", "reset", "revert", "keep", "leave", "stay",
    "unlock", "dismiss", "later", "skip", "rename", "never", "log", "don't", "dont", "trash", "move"})
NAV_WORDS = frozenset({"folder", "folders", "hidden", "bookmark", "bookmarks", "view", "sidebar", "filter", "preview",
                       "places", "recent", "all", "new", "up", "forward", "back", "home", "reload", "refresh", "sort"})


def is_standard_label(label):
    low = label.lower().replace("\u2019", "'")
    words = re.findall(r"[a-z']+", low)
    if not words or (set(words) & NAV_WORDS):
        return False
    if len(words) == 1:
        return words[0] in STD_WORDS
    return words[0] in STD_WORDS or " ".join(words[:2]) in ("not now", "do not")


def ax_get(el, name):
    try:
        err, v = AXUIElementCopyAttributeValue(el, name, None)
    except Exception:
        return None
    return v if err == 0 else None


def ax_dialog_buttons(pid):
    """buttons of the focused window's sheet / dialog, or [] when there is none"""
    if AXUIElementCreateApplication is None:
        return []
    try:
        app = AXUIElementCreateApplication(pid)
        if AXUIElementSetMessagingTimeout:
            AXUIElementSetMessagingTimeout(app, 0.25)
        win = ax_get(app, "AXFocusedWindow")
        if win is None:
            return []
        root = win if ax_get(win, "AXSubrole") in ("AXDialog", "AXSystemDialog") else None
        if root is None:
            for c in ax_get(win, "AXChildren") or []:
                if ax_get(c, "AXRole") == "AXSheet":
                    root = c
                    break
        if root is None:
            return []
        default_btn, cancel_btn = ax_get(root, "AXDefaultButton"), ax_get(root, "AXCancelButton")
        out = []

        def walk(el, depth):
            if depth > 7 or len(out) > 24:
                return
            for c in ax_get(el, "AXChildren") or []:
                role = ax_get(c, "AXRole")
                if role == "AXButton":
                    label = clean_button_label(str(ax_get(c, "AXTitle") or ax_get(c, "AXDescription") or ""))
                    en = ax_get(c, "AXEnabled")
                    if label and (en is None or bool(en)):
                        out.append({"label": label, "el": c})
                elif role in ("AXGroup", "AXSplitGroup", "AXSheet", "AXUnknown"):
                    walk(c, depth + 1)

        walk(root, 0)
        std = [b for b in out if is_standard_label(b["label"])]
        out = std or out
        for b in out:
            role = classify_button(b["label"])
            try:
                if default_btn is not None and b["el"] == default_btn:
                    role = "accept"
                elif cancel_btn is not None and b["el"] == cancel_btn:
                    role = "reject"
            except Exception:
                pass
            b["role"] = role
        return out
    except Exception:
        log.exception("dialog scan failed")
        return []


# ======================= system info =======================
def battery_text():
    try:
        out = subprocess.run(["ioreg", "-arn", "AppleSmartBattery"], capture_output=True, timeout=5).stdout
        if not out.strip():
            return "No battery"
        d = plistlib.loads(out)[0]
        design = d.get("DesignCapacity") or 0
        cap = d.get("NominalChargeCapacity") or d.get("AppleRawMaxCapacity") or d.get("MaxCapacity") or 0
        health = min(100, round(cap * 100 / design))
        return f"Batt {health}% | {d.get('CycleCount', 0)}c"
    except Exception:
        return "Batt N/A"


def ssd_text():
    try:
        vals, _ = NSURL.fileURLWithPath_("/").resourceValuesForKeys_error_(
            [K_AVAIL, K_TOTAL], None)
        free = vals[K_AVAIL]
        return f"SSD {free / 1e9:.0f}G free"
    except Exception:
        return f"SSD {shutil.disk_usage('/').free / 1e9:.0f}G free"


def ram_static():
    try:
        data = json.loads(run(["system_profiler", "SPMemoryDataType", "-json"], 30))["SPMemoryDataType"]
        sizes, total, unified = [], 0, ""
        for blk in data:
            if blk.get("_items"):
                for it in blk["_items"]:
                    total += 1
                    sz = (it.get("dimm_size") or "").strip()
                    if sz and "empty" not in sz.lower():
                        sizes.append(sz.replace(" GB", "").replace(" ", ""))
            elif isinstance(blk.get("SPMemoryDataType"), str):
                unified = blk["SPMemoryDataType"]
        if total:
            return f"{len(sizes)}/{total}: " + "+".join(sizes) + " GB"
        if unified:
            return unified + " unified"
    except Exception:
        pass
    try:
        v = ctypes.c_uint64(0)
        size = ctypes.c_size_t(8)
        libc.sysctlbyname(b"hw.memsize", ctypes.byref(v), ctypes.byref(size), None, 0)
        return f"{v.value // 2**30} GB"
    except Exception:
        return "N/A"


def tb_chip():
    try:
        data = json.loads(run(["system_profiler", "SPiBridgeDataType", "-json"], 30))["SPiBridgeDataType"]
        name = data[0].get("ibridge_model_name", "")
        return name.replace("Apple ", "").replace(" Security Chip", "").replace(" Chip", "") or "built-in"
    except Exception:
        return "built-in"


# ======================= worker (all slow work happens here, never on the UI thread) =======================
def running_bundles():
    return {b for b in ALL_BUNDLES.values() if NSRunningApplication.runningApplicationsWithBundleIdentifier_(b)}


_sb = {}


def sb(bundle):
    if SBApplication is None:
        raise RuntimeError("ScriptingBridge not installed")
    if bundle not in _sb:
        _sb[bundle] = SBApplication.applicationWithBundleIdentifier_(bundle)
    return _sb[bundle]


def query_player(name):
    try:
        app = sb(PLAYERS[name])
        st = app.playerState()
        state = "playing" if st == PS_PLAYING else "paused" if st == PS_PAUSED else ""
        if not state:
            return None
        t = app.currentTrack()
        title, artist = str(t.name() or ""), str(t.artist() or "")
        art = "music"
        if name == "Spotify":
            u = t.artworkUrl()
            art = f"url:{u}" if u else ""
        return dict(app=name, state=state, label=title + (f" - {artist}" if artist else ""), art=art)
    except Exception:
        return None


def clean_title(t):
    t = re.sub(r"^\(\d+\)\s*", "", t)
    for s in (" - YouTube Music", " - YouTube") + tuple(TITLE_SUFFIXES):
        if t.endswith(s) and len(t) > len(s):
            t = t[: -len(s)]
    return t.strip()


def browser_audio():
    out = run(["pmset", "-g", "assertions"], 5)
    m = re.search(r"pid \d+\((Google Chrome|Brave Browser|Microsoft Edge|Vivaldi|Chromium|Arc)[^)]*\).*audio", out,
                  re.I)
    return m.group(1) if m else ""


def read_tabs(name):
    """(label, art) of the tab that is most likely the audio source"""
    best, best_url, best_score = "", "", 0
    try:
        for w in sb(BROWSERS[name]).windows():
            tabs = w.tabs()
            titles = list(tabs.arrayByApplyingSelector_("title"))
            urls = list(tabs.arrayByApplyingSelector_("URL"))
            act = w.activeTabIndex()
            for i, (t, u) in enumerate(zip(titles, urls)):
                t, u = str(t or ""), str(u or "")
                score = (2 if any(d in u for d in MEDIA_SITES) else 0) + (1 if i + 1 == act else 0)
                if t and score > best_score:
                    best, best_url, best_score = t, u, score
    except Exception:
        pass
    m = re.search(r"[?&]v=([A-Za-z0-9_-]+)", best_url) if "youtube.com/" in best_url else None
    return clean_title(best or "Playing audio"), (f"yt:{m.group(1)}" if m else "")


def ax_front_title(pid):
    try:
        err, win = AXUIElementCopyAttributeValue(AXUIElementCreateApplication(pid), "AXFocusedWindow", None)
        if err or win is None:
            return ""
        err, title = AXUIElementCopyAttributeValue(win, "AXTitle", None)
        return str(title) if not err and title else ""
    except Exception:
        return ""


class Worker(threading.Thread):
    def __init__(self, dash):
        super().__init__(daemon=True)
        self.dash = dash
        self.click_time = 0.0
        self.last_browser = None      # (time, dict) last browser track seen playing
        self.paused_rec = None        # (since, dict)
        self.browser_cache = (None, 0, "", "")
        self.audio_cache = (0, "")
        self.last_good_title = ""
        self.snap = None
        self.sys = {}
        self.bright = None
        self.vol_last = None
        self.dlg_btns, self.dlg_sig = [], ()
        self.ax_ok = bool(AXIsProcessTrusted and AXIsProcessTrusted())
        if CFG["dialogs"]["enabled"] and not self.ax_ok:
            log.warning("Accessibility permission missing: dialog buttons / window titles need it "
                        "(System Settings > Privacy & Security > Accessibility)")

    # --- Now Playing ---
    def now_playing(self, now, running):
        cands = []
        for name, bid in PLAYERS.items():
            if bid in running:
                r = query_player(name)
                if r:
                    cands.append(r)
        proc = ""
        if any(b in running for b in BROWSERS.values()):
            if now - self.audio_cache[0] >= 1:
                self.audio_cache = (now, browser_audio())
            proc = self.audio_cache[1]
        if proc:
            bc = self.browser_cache
            if bc[0] != proc or now - bc[1] >= 3:
                label, art = read_tabs(proc)
                self.browser_cache = (proc, now, label, art)
            _, _, label, art = self.browser_cache
            cands.append(dict(app=proc, state="playing", label=label, art=art))
        chosen = next((c for c in cands if c["state"] == "playing"), cands[0] if cands else None)
        if chosen and chosen["state"] == "playing":
            self.paused_rec = None
            if chosen["app"] in BROWSERS:
                self.last_browser = (now, chosen)
        if not chosen:
            chosen = self.assumed_paused(now)
        return chosen or dict(app="", state="none", label=NOT_PLAYING, art="")

    def assumed_paused(self, now):
        """browsers have no pause signal: audio stopping right after a click on Now Playing = paused"""
        if self.paused_rec:
            if now - self.paused_rec[0] < PAUSED_HOLD:
                return self.paused_rec[1]
            self.paused_rec = None
            return None
        if not self.last_browser or now - self.last_browser[0] > PAUSED_HOLD or not self.click_time:
            return None
        if abs(self.last_browser[0] - self.click_time) > CLICK_WINDOW:
            return None
        rec = dict(self.last_browser[1], state="paused")
        self.paused_rec = (now, rec)
        return rec

    # --- frontmost app: title (browsers), bundle id, pid ---
    def front_info(self):
        try:
            fa = NSWorkspace.sharedWorkspace().frontmostApplication()
            bid, pid = str(fa.bundleIdentifier()), int(fa.processIdentifier())
        except Exception:
            return dict(title=NOT_BROWSER, fb=None, bundle="", pid=0)
        name = BUNDLE_TO_BROWSER.get(bid)
        if not name:
            return dict(title=NOT_BROWSER, fb=None, bundle=bid, pid=pid)
        t = ""
        try:
            t = str(sb(bid).windows()[0].activeTab().title() or "")
        except Exception:
            pass
        if not t:
            t = ax_front_title(pid)
        for s in TITLE_SUFFIXES:
            if s in t:
                t = t[: t.index(s)]
                break
        if t:
            self.last_good_title = t
        return dict(title=t or self.last_good_title or EMPTY, fb=name, bundle=bid, pid=pid)

    # --- sheet / dialog buttons of the frontmost app ---
    def poll_dialog(self, tick, info):
        if not CFG["dialogs"]["enabled"] or not AXIsProcessTrusted:
            return ()
        if tick % 40 == 0:
            self.ax_ok = bool(AXIsProcessTrusted())
        if not self.ax_ok:
            return ()
        if tick % 2:
            return self.dlg_sig
        pid = info["pid"]
        self.dlg_btns = [] if (not pid or pid == os.getpid()) else ax_dialog_buttons(pid)
        self.dlg_sig = tuple((b["label"], b["role"]) for b in self.dlg_btns)
        return self.dlg_sig

    def press_dialog(self, idx):
        try:
            el = self.dlg_btns[idx]["el"]
        except IndexError:
            return
        threading.Thread(target=lambda: AXUIElementPerformAction(el, "AXPress"), daemon=True).start()

    # --- system info (slow-changing, refreshed on its own schedule) ---
    def refresh_sys(self, tick):
        s = self.sys
        if tick == 0:
            s["ram_static"], s["tb"], s["id"] = ram_static(), tb_chip(), sysctl_str("hw.model")
        if tick % 40 == 0:      # ~10 s
            s["batt"], s["ssd"] = battery_text(), ssd_text()
        if tick % 8 == 0:       # ~2 s
            lvl = sysctl_int("kern.memorystatus_level")
            s["ram"] = f"RAM {s.get('ram_static', '')}" + (f" | {lvl}% free" if lvl is not None else "")

    def step(self, tick):
        now = time.time()
        running = running_bundles()
        np = self.now_playing(now, running)
        info = self.front_info()
        snap = dict(np=np, title=info["title"], fb=info["fb"], bundle=info["bundle"], caps=caps_lock_on(),
                    dialog=self.poll_dialog(tick, info))
        if snap != self.snap:
            self.snap = snap
            AppHelper.callAfter(self.dash.apply_snapshot, snap)
        before = dict(self.sys)
        self.refresh_sys(tick)
        if self.sys != before:
            AppHelper.callAfter(self.dash.apply_sys, dict(self.sys))
        vol = get_volume()
        if vol is not None and vol != self.vol_last:
            self.vol_last = vol
            AppHelper.callAfter(self.dash.apply_volume, *vol)
        b = get_brightness()
        if b is not None and (self.bright is None or abs(b - self.bright) > 0.004):
            self.bright = b
            AppHelper.callAfter(self.dash.apply_brightness, b)

    def run(self):
        tick = 0
        while True:
            try:
                with objc.autorelease_pool():
                    self.step(tick)
            except Exception:
                log.exception("worker tick failed")
            tick += 1
            time.sleep(TICK)


# ======================= UI =======================
def para():
    ps = NSMutableParagraphStyle.alloc().init()
    ps.setAlignment_(NSTextAlignmentCenter)
    ps.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return ps


def set_title(btn, text, size=None, rgb=None):
    attrs = {NSFontAttributeName: make_font(size or FONT_SIZE), NSForegroundColorAttributeName: color(rgb or FG),
             NSParagraphStyleAttributeName: para()}
    btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))


def make_button(text, width, target=None, action=None, size=None, rgb=None, bezel=None, image=None):
    b = NSButton.buttonWithTitle_target_action_(text, target, action)
    b.setBezelColor_(color(bezel or BG))
    b.setImagePosition_(NSImageLeft)
    b.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    b.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    set_title(b, text, size, rgb)
    if image is not None:
        b.setImage_(symbol(image))
        if not text:
            b.setImagePosition_(1)  # NSImageOnly
    return b


def square_icon(img, size=ICON_SIZE):
    s = img.size()
    m = min(s.width, s.height)
    src = NSMakeRect((s.width - m) / 2, (s.height - m) / 2, m, m)
    out = NSImage.alloc().initWithSize_((size, size))
    out.lockFocus()
    img.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, size, size), src, NSCompositingOperationSourceOver, 1.0)
    out.unlockFocus()
    return out


class CavaButton(NSButton):
    """a button that paints the cava bars (drawing comes from the shared CavaEngine)"""

    def drawRect_(self, rect):
        try:
            b = self.bounds()
            w, h = b.size.width, b.size.height
            bars = CAVA.snapshot() if CAVA else []
            n = len(bars)
            if not n:
                return
            cols = CAVA.colors(n)
            gap = 1.5
            bw = max(1.0, (w - gap * (n - 1)) / n)
            for i, v in enumerate(bars):
                bh = max(2.0, h * min(100, v) / 100.0)
                color(cols[i]).set()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(i * (bw + gap), 0, bw, bh), 1.0, 1.0).fill()
        except Exception:
            log.exception("cava draw failed")


def make_cava_button(width, target=None, action=None):
    b = CavaButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, 30))
    b.setTitle_("")
    b.setBordered_(False)
    b.setWantsLayer_(True)
    b.layer().setBackgroundColor_(color(BG).CGColor())
    b.layer().setCornerRadius_(8.0)
    b.setTranslatesAutoresizingMaskIntoConstraints_(False)
    b.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    b.heightAnchor().constraintEqualToConstant_(30.0).setActive_(True)
    if target is not None:
        b.setTarget_(target)
        b.setAction_(action)
    return b


TRAY_ID = "local.touchdash.tray"
STRIP_IDENT = {"vol": "dash.volume", "bri": "dash.bright"}
SYS_WIDTHS = {"dash.batt": 96, "dash.ssd": 92, "dash.ram": 160, "dash.tb": 90, "dash.disp": 140, "dash.id": 110}
EXPANDED_IDENT = {"PREV": "dash.prev", "PLAY": "dash.play", "NEXT": "dash.next", "CAVA": "dash.cava",
                  "EMOJI": "dash.emoji", "COLORS": "dash.colors", "SLEEP": "dash.sleep"}
BUILTIN_WIDGETS = {"tabs", "np", "newtab", "closetab", "reload"}
FN_CODES = {n: KEYCODES[f"f{n}"] for n in range(1, 13)}


class Dash(NSObject):
    def init(self):
        global CAVA
        self = objc.super(Dash, self).init()
        if self is None:
            return None
        self.items, self.buttons = {}, {}
        self.page, self.visible, self.ctx = 1, False, "default"
        self.snap, self.icon_cache, self.art_cache, self.art_tried = None, {}, {}, set()
        self.tab_list = []
        self.target_browser = "Google Chrome"
        self.strips, self.strip_state, self.open_key, self.vol_touched, self.bri_touched = {}, "collapsed", None, 0.0, 0.0
        self.vol_idle, self.anim_token = 0, 0
        self.sys_last, self.bright_last, self.vol_last_ui = {}, None, None
        self.worker = None
        self.window, self.status, self.status_lines = None, None, []
        self.mode = "window" if "--window" in sys.argv else "system"
        self.caps_on = False
        self.dialog_ids, self.dlg_buttons, self.dlg_gen, self.dlg_sig = [], [], 0, ()
        self.widget_defs, self.widget_tags, self.ctx_ids = {}, {}, {}
        self.cava_views, self.cava_hsv, self.cava_edit_idx = [], [0.0, 1.0, 1.0], 0
        self.cv = {}
        self.emoji_items, self.emoji_scrubber = [], None
        self.emoji_data = {k: list(v) for k, v in DEFAULT_EMOJI_DATA.items()}
        for k, v in (load_json("emoji_data.json", {}) or {}).items():
            if isinstance(v, list):
                self.emoji_data[k] = v
        ct = load_json("color_tool.json", {})
        self.tool_rgb = [max(0, min(255, int(c))) for c in (ct.get("rgb") or list(ACCENT))[:3]]
        self.tool_fmt = COLOR_FORMATS.index(ct["fmt"]) if ct.get("fmt") in COLOR_FORMATS else 0
        self.ct = {}
        self.save_tokens = {}
        self.build_context_tables()
        CAVA = self.cava = CavaEngine(lambda: AppHelper.callAfter(self.cava_redraw))
        self.cava_sync_hsv()
        return self

    @objc.python_method
    def build_context_tables(self):
        for key, widgets in CFG["contexts"].items():
            ids = []
            for i, w in enumerate(widgets):
                if "builtin" in w:
                    if w["builtin"] in BUILTIN_WIDGETS:
                        ids.append(f"dash.{w['builtin']}")
                else:
                    ident = f"dash.w.{key}.{i}"
                    self.widget_defs[ident] = w
                    ids.append(ident)
            self.ctx_ids[key] = ids
        self.ctx_ids.setdefault("default", ["dash.np"])

    # ---------- app lifecycle ----------
    @guard1
    def applicationDidFinishLaunching_(self, note):
        log.info("launched; presentSystemModal api: %s, addSystemTrayItem: %s, DFRFoundation: %s, DisplayServices: %s, SB: %s",
                 hasattr(NSTouchBar, "presentSystemModalTouchBar_placement_systemTrayItemIdentifier_"),
                 hasattr(NSTouchBarItem, "addSystemTrayItem_"), _dfr is not None, _ds is not None,
                 SBApplication is not None)
        self.build_status_window()
        self.say("TouchDash is running. Log: ~/Library/Logs/TouchDash.log  Config: " + config_path())
        self.bar = NSTouchBar.alloc().init()
        self.bar.setDelegate_(self)
        self.bar.setDefaultItemIdentifiers_(self.page_ids())
        if _dfr:
            _dfr.DFRSystemModalShowsCloseBoxWhenFrontMost(True)
        tray = NSCustomTouchBarItem.alloc().initWithIdentifier_(TRAY_ID)
        tray.setView_(NSButton.buttonWithTitle_target_action_("\u25a4", self, "togglePanel:"))
        if hasattr(NSTouchBarItem, "addSystemTrayItem_"):
            NSTouchBarItem.addSystemTrayItem_(tray)
            log.info("control strip item added")
        else:
            log.error("NSTouchBarItem.addSystemTrayItem_ missing")
        if _dfr:
            _dfr.DFRElementSetControlStripPresenceForIdentifier(objc.pyobjc_id(NSString.stringWithString_(TRAY_ID)), True)
        self.tray = tray
        self.update_display()
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
        self.worker = Worker(self)
        self.worker.start()
        # the control strip needs a moment to register our item before the bar can be shown
        self.performSelector_withObject_afterDelay_("startShowing:", None, 1.0)

    @guard1
    def startShowing_(self, sender):
        self.activate_mode()
        AppHelper.callLater(0.3, self.reapply)

    @guard1
    def screensChanged_(self, note):
        self.update_display()

    @objc.python_method
    def build_status_window(self):
        from AppKit import NSTextField, NSWindow
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 440, 190), 7, 2, False)
        win.setTitle_("TouchDash")
        win.setReleasedWhenClosed_(False)
        win.center()
        self.status = NSTextField.wrappingLabelWithString_("starting...")
        self.status.setFrame_(NSMakeRect(16, 64, 408, 108))
        btn = NSButton.buttonWithTitle_target_action_("Switch Touch Bar mode (system-wide / in-app)", self, "toggleMode:")
        btn.setFrame_(NSMakeRect(16, 16, 330, 32))
        win.contentView().addSubview_(self.status)
        win.contentView().addSubview_(btn)
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window = win

    @objc.python_method
    def say(self, line):
        log.info("status: %s", line)
        self.status_lines = (self.status_lines + [line])[-6:]
        if self.status is not None:
            self.status.setStringValue_("\n".join(self.status_lines))

    @objc.python_method
    def activate_mode(self):
        if self.mode == "system":
            ok = present_modal(self.bar, TRAY_ID)
            self.visible = ok
            if ok:
                self.say("System-wide Touch Bar presented. If you see nothing, press the mode button below.")
                return
            self.say("System-wide mode FAILED (private API unavailable). Falling back to in-app mode.")
            self.mode = "window"
        self.window.setTouchBar_(self.bar)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.say("In-app mode: the Touch Bar shows while TouchDash is the frontmost app (click this window).")

    @guard1
    def toggleMode_(self, sender):
        if self.mode == "system":
            dismiss_modal(self.bar)
            self.visible = False
            self.mode = "window"
        else:
            self.window.setTouchBar_(None)
            self.mode = "system"
        self.activate_mode()

    @guard1
    def togglePanel_(self, sender):
        if self.visible:
            dismiss_modal(self.bar)
            self.visible = False
        else:
            self.visible = present_modal(self.bar, TRAY_ID)

    # ---------- pages ----------
    @objc.python_method
    def strip_idents(self):
        return ["dash.volume"] + (["dash.bright"] if CFG["show_brightness"] else [])

    @objc.python_method
    def base_ids(self):
        esc = ["dash.esc"] if CFG["esc_mode"] == "T1" else []
        if self.page == 4:
            return esc + ["dash.back"] + [f"dash.f.{n}" for n in range(1, 13)] + ["dash.del"]
        if self.page == 3:
            items = [EXPANDED_IDENT[x] for x in CFG["expanded_items"] if x in EXPANDED_IDENT
                     and not (x == "EMOJI" and not HAS_SCRUBBER) and not (x == "CAVA" and not CFG["cava"]["enabled"])]
            return esc + [FLEX] + items + ["dash.back"]
        if self.page == 2:
            return esc + ["dash.page", "dash.batt", "dash.ssd", "dash.ram", "dash.tb", "dash.disp", "dash.id",
                          FLEX] + self.strip_idents()
        tail = (["dash.caps"] if CFG["show_caps_indicator"] else []) + self.strip_idents() \
            + (["dash.fn"] if CFG["show_fn_button"] else []) + ["dash.expand"]
        return esc + ["dash.page"] + self.ctx_ids.get(self.ctx, self.ctx_ids["default"]) + [FLEX] + tail

    @objc.python_method
    def page_ids(self):
        return self.dialog_ids or self.base_ids()

    @objc.python_method
    def refresh_bar(self):
        if self.strip_state == "collapsed" and hasattr(self, "bar"):
            self.bar.setDefaultItemIdentifiers_(self.page_ids())

    @objc.python_method
    def go_page(self, n):
        if self.strip_state != "collapsed":
            return
        self.page = n
        if n == 3:
            self.cava.start()
        else:
            self.cava.stop()
        self.refresh_bar()
        AppHelper.callLater(0.15, self.reapply)

    @guard1
    def togglePage_(self, sender):
        click_sound()
        self.go_page(2 if self.page == 1 else 1)

    @guard1
    def expandTapped_(self, sender):
        click_sound()
        self.go_page(3)

    @guard1
    def backTapped_(self, sender):
        click_sound()
        self.go_page(1)

    @guard1
    def fnOpen_(self, sender):
        click_sound()
        self.go_page(4)

    # ---------- items ----------
    @objc.typedSelector(b"@@:@@")
    @guard2
    def touchBar_makeItemForIdentifier_(self, touchBar, identifier):
        identifier = str(identifier)
        if identifier.startswith("dash.tab."):  # tab switcher entries (rebuilt every time the popover opens)
            return self.make_tab_item(identifier)
        if identifier.startswith("dash.dlg."):
            return self.make_dialog_item(identifier)
        if identifier not in self.items:
            self.items[identifier] = self.build_item(identifier)
        return self.items[identifier]

    @objc.python_method
    def custom(self, identifier, view):
        it = NSCustomTouchBarItem.alloc().initWithIdentifier_(identifier)
        it.setView_(view)
        self.buttons[identifier] = view
        return it

    @objc.python_method
    def popover(self, ident, collapsed, bar):
        it = NSPopoverTouchBarItem.alloc().initWithIdentifier_(ident)
        it.setCollapsedRepresentation_(collapsed)
        it.setShowsCloseButton_(True)
        it.setPopoverTouchBar_(bar)
        self.buttons[ident] = collapsed
        return it

    @objc.python_method
    def new_bar(self, ids):
        bar = NSTouchBar.alloc().init()
        bar.setDelegate_(self)
        bar.setDefaultItemIdentifiers_(ids)
        return bar

    @objc.python_method
    def build_item(self, ident):
        if ident == "dash.esc":
            return self.custom(ident, make_button("esc", W_ESC, self, "escTapped:"))
        if ident == "dash.page":
            return self.custom(ident, make_button("\u21c4", W_SMALL, self, "togglePage:"))
        if ident == "dash.tabs":
            btn = make_button(EMPTY, W_TITLE, self, "openTabs:")
            self.buttons[ident] = btn
            self.tabs_item = NSPopoverTouchBarItem.alloc().initWithIdentifier_(ident)
            self.tabs_item.setCollapsedRepresentation_(btn)
            self.tabs_item.setShowsCloseButton_(True)
            return self.tabs_item
        if ident == "dash.np":
            return self.custom(ident, make_button(NOT_PLAYING, W_NP, self, "npClicked:", NP_FONT_SIZE, FG_DIM))
        if ident == "dash.newtab":
            return self.custom(ident, make_button("+", W_SMALL, self, "newTab:", 18))
        if ident == "dash.closetab":
            return self.custom(ident, make_button("\u00d7", W_SMALL, self, "closeTab:", 18))
        if ident == "dash.reload":
            return self.custom(ident, make_button("\u21bb", W_SMALL, self, "reloadTab:", 18))
        if ident == "dash.caps":
            return self.custom(ident, make_button("\u21ea", W_CAPS, None, None, 16, FG_DIM))
        if ident in ("dash.volume", "dash.bright"):
            return self.build_strip("vol" if ident == "dash.volume" else "bri")
        if ident == "dash.fn":
            return self.custom(ident, make_button("fn", W_SMALL + 10, self, "fnOpen:"))
        if ident == "dash.expand":
            return self.custom(ident, make_button("\u2039", W_EXPAND, self, "expandTapped:", 22))
        if ident == "dash.back":
            return self.custom(ident, make_button("\u203a", W_EXPAND, self, "backTapped:", 22))
        if ident == "dash.prev":
            return self.custom(ident, make_button("", 90, self, "prevTapped:", image="backward.fill"))
        if ident == "dash.play":
            return self.custom(ident, make_button("", 115, self, "playTapped:", image="playpause.fill"))
        if ident == "dash.next":
            return self.custom(ident, make_button("", 90, self, "nextTapped:", image="forward.fill"))
        if ident == "dash.sleep":
            return self.custom(ident, make_button("Sleep", 90, self, "sleepTapped:"))
        if ident == "dash.cava":
            return self.build_cava_item(ident)
        if ident == "dash.emoji":
            return self.popover(ident, make_button("\U0001F642", 90, self, "openEmoji:", 18), self.build_emoji_bar())
        if ident == "dash.colors":
            btn = make_button("RGB", 90, self, "openColors:", 14)
            self.colors_item = self.popover(ident, btn, self.new_bar(self.ct_ids()))
            return self.colors_item
        if ident.startswith("dash.f."):
            n = int(ident.rsplit(".", 1)[1])
            b = make_button(f"F{n}", 70, self, "fnTapped:", 13)
            b.setTag_(n)
            return self.custom(ident, b)
        if ident == "dash.del":
            return self.custom(ident, make_button("del", 90, self, "delTapped:"))
        if ident in self.widget_defs:
            w = self.widget_defs[ident]
            b = make_button(w.get("text", ""), int(w.get("width", 90)), self, "widgetTapped:", image=w.get("icon"))
            tag = len(self.widget_tags) + 1
            self.widget_tags[tag] = w
            b.setTag_(tag)
            return self.custom(ident, b)
        if ident in SYS_WIDTHS:
            return self.custom(ident, make_button("...", SYS_WIDTHS[ident]))
        if ident.startswith("cv."):
            return self.build_cava_picker_item(ident)
        if ident.startswith("ct."):
            return self.build_color_item(ident)
        if ident.startswith("em."):
            return self.build_emoji_item(ident)
        return None

    @objc.python_method
    def make_tab_item(self, ident):
        i = int(ident.rsplit(".", 1)[1])
        title, active = self.tab_list[i]
        b = make_button(title[:20], 150, self, "switchTab:", 12, FG if active else FG_DIM)
        b.setTag_(i)
        it = NSCustomTouchBarItem.alloc().initWithIdentifier_(ident)
        it.setView_(b)
        return it

    # ---------- basic key actions ----------
    @guard1
    def escTapped_(self, sender):
        click_sound()
        press_key(KEYCODES["esc"])

    @guard1
    def fnTapped_(self, sender):
        click_sound()
        press_key(FN_CODES[sender.tag()])

    @guard1
    def delTapped_(self, sender):
        click_sound()
        press_key(KEYCODES["backspace"])

    @guard1
    def prevTapped_(self, sender):
        click_sound()
        post_media_key(NX_PREV)

    @guard1
    def nextTapped_(self, sender):
        click_sound()
        post_media_key(NX_NEXT)

    @guard1
    def playTapped_(self, sender):
        click_sound()
        if self.worker:
            self.worker.click_time = time.time()
        post_media_key(NX_PLAY)

    @guard1
    def sleepTapped_(self, sender):
        threading.Thread(target=lambda: run(["pmset", "sleepnow"], 5), daemon=True).start()

    @guard1
    def widgetTapped_(self, sender):
        click_sound()
        w = self.widget_tags.get(sender.tag(), {})
        if w.get("shortcut"):
            press_shortcut(w["shortcut"])
        elif w.get("applescript"):
            osa(w["applescript"])

    # ---------- expanding strips (volume / brightness): collapsed button <-> animated full-bar slider ----------
    @objc.python_method
    def build_strip(self, key):
        ident = STRIP_IDENT[key]
        icon = make_button("--", W_VOL, self, key + "Tapped:", 12)
        icon.setImage_(symbol("speaker.wave.2.fill" if key == "vol" else "sun.max.fill"))
        if hasattr(icon, "setContentTintColor_"):
            icon.setContentTintColor_(color(FG))
        slider = NSSlider.sliderWithTarget_action_(self, key + "Changed:")
        slider.setMinValue_(0.0)
        slider.setMaxValue_(1.0)
        slider.setContinuous_(True)
        slider.setAlphaValue_(0.0)
        if hasattr(slider, "setTrackFillColor_"):
            slider.setTrackFillColor_(color(ACCENT))
        mute = make_button("Mute", EXP_MUTE_W, self, "muteTapped:", 12) if key == "vol" else None
        if mute is not None:
            mute.setAlphaValue_(0.0)
        cont = NSView.alloc().init()
        cont.setTranslatesAutoresizingMaskIntoConstraints_(False)
        for v in (icon, slider, mute):
            if v is not None:
                v.setTranslatesAutoresizingMaskIntoConstraints_(False)
                cont.addSubview_(v)

        def on(c):
            c.setActive_(True)
            return c

        on(icon.leadingAnchor().constraintEqualToAnchor_(cont.leadingAnchor()))
        on(icon.centerYAnchor().constraintEqualToAnchor_(cont.centerYAnchor()))
        on(slider.leadingAnchor().constraintEqualToAnchor_constant_(icon.trailingAnchor(), 6))
        on(slider.centerYAnchor().constraintEqualToAnchor_(cont.centerYAnchor()))
        sw = on(slider.widthAnchor().constraintEqualToConstant_(0.0))
        mw = None
        if mute is not None:
            on(mute.leadingAnchor().constraintEqualToAnchor_constant_(slider.trailingAnchor(), 6))
            on(mute.centerYAnchor().constraintEqualToAnchor_(cont.centerYAnchor()))
            mw = on(mute.widthAnchor().constraintEqualToConstant_(0.0))
            on(cont.trailingAnchor().constraintEqualToAnchor_(mute.trailingAnchor()))
        else:
            on(cont.trailingAnchor().constraintEqualToAnchor_(slider.trailingAnchor()))
        on(cont.heightAnchor().constraintEqualToConstant_(30.0))
        self.strips[key] = dict(icon=icon, slider=slider, mute=mute, sw=sw, mw=mw)
        return self.custom(ident, cont)

    @objc.python_method
    def animate(self, duration, apply, done=None):
        """timer-driven animation (ease-out); a newer animation cancels an older one"""
        self.anim_token += 1
        token, t0 = self.anim_token, time.time()

        def step():
            if token != self.anim_token:
                return
            p = min(1.0, (time.time() - t0) / duration)
            try:
                apply(1 - (1 - p) ** 3)
            except Exception:
                log.exception("animation step failed")
            if p < 1.0:
                AppHelper.callLater(1 / 60.0, step)
            elif done:
                done()
        step()

    @objc.python_method
    def other_views(self, skip_ident):
        out = []
        for ident in self.base_ids():
            if ident in (FLEX, skip_ident):
                continue
            v = self.buttons.get(ident)
            if v is not None:
                out.append(v)
        return out

    @objc.python_method
    def set_open(self, key, e):  # e: 0 = collapsed, 1 = fully expanded
        v = self.strips[key]
        v["sw"].setConstant_(EXP_SLIDER_W * e)
        v["slider"].setAlphaValue_(e)
        if v["mute"] is not None:
            v["mw"].setConstant_(EXP_MUTE_W * e)
            v["mute"].setAlphaValue_(e)

    @objc.python_method
    def expand_strip(self, key):
        if self.strip_state != "collapsed" or key not in self.strips:
            return
        self.strip_state, self.open_key = "expanding", key
        ident = STRIP_IDENT[key]
        others = self.other_views(ident)

        def fade_out(e):
            for o in others:
                o.setAlphaValue_(1 - e)

        def grow():
            self.bar.setDefaultItemIdentifiers_([FLEX, ident])  # the strip now has the bar to itself
            self.animate(0.28, lambda e: self.set_open(key, e), expanded)

        def expanded():
            self.strip_state = "expanded"
            self.bump_idle()

        self.animate(0.12, fade_out, grow)

    @objc.python_method
    def collapse_strip(self):
        if self.strip_state != "expanded" or not self.open_key:
            return
        key = self.open_key
        self.strip_state = "collapsing"
        self.vol_idle += 1  # cancels the auto-close timer
        others = self.other_views(STRIP_IDENT[key])

        def shrink(e):
            self.set_open(key, 1 - e)

        def restore():
            self.bar.setDefaultItemIdentifiers_(self.page_ids())
            for o in others:
                o.setAlphaValue_(0.0)
            self.animate(0.16, lambda e: [o.setAlphaValue_(e) for o in others], collapsed)

        def collapsed():
            self.strip_state, self.open_key = "collapsed", None

        self.animate(0.24, shrink, restore)

    @objc.python_method
    def bump_idle(self):
        """(re)start the auto-close countdown"""
        self.vol_idle += 1
        token = self.vol_idle

        def check():
            if token == self.vol_idle and self.strip_state == "expanded":
                self.collapse_strip()
        AppHelper.callLater(AUTO_CLOSE, check)

    @objc.python_method
    def strip_tap(self, key):
        click_sound()
        if self.strip_state == "collapsed":
            self.expand_strip(key)
        elif self.strip_state == "expanded":
            self.collapse_strip()

    @guard1
    def volTapped_(self, sender):
        self.strip_tap("vol")

    @guard1
    def briTapped_(self, sender):
        self.strip_tap("bri")

    @guard1
    def volChanged_(self, sender):
        self.vol_touched = time.time()
        set_volume(sender.doubleValue())
        self.bump_idle()

    @guard1
    def briChanged_(self, sender):
        self.bri_touched = time.time()
        set_brightness(sender.doubleValue())
        self.bump_idle()

    @guard1
    def muteTapped_(self, sender):
        cur = get_volume()
        set_mute(not (cur and cur[1]))
        self.bump_idle()

    @objc.python_method
    def apply_volume(self, v, muted):
        self.vol_last_ui = (v, muted)
        s = self.strips.get("vol")
        if not s:
            return
        name = ("speaker.slash.fill" if muted or v <= 0.001 else "speaker.wave.1.fill" if v < 0.34
                else "speaker.wave.2.fill" if v < 0.67 else "speaker.wave.3.fill")
        s["icon"].setImage_(symbol(name))
        set_title(s["icon"], "Muted" if muted else f"{round(v * 100)}%", 12, FG_DIM if muted else FG)
        set_title(s["mute"], "Unmute" if muted else "Mute", 12)
        if time.time() - self.vol_touched > 1.0:  # don't fight the finger
            s["slider"].setDoubleValue_(v)

    @objc.python_method
    def apply_brightness(self, v):
        self.bright_last = v
        s = self.strips.get("bri")
        if not s:
            return
        set_title(s["icon"], f"{round(v * 100)}%", 12)
        if time.time() - self.bri_touched > 1.5:  # don't fight the finger
            s["slider"].setDoubleValue_(v)

    # ---------- state -> UI (main thread only; called only when something changed) ----------
    @objc.python_method
    def context_key(self, snap):
        if snap["fb"] and "browser" in self.ctx_ids:
            return "browser"
        return snap["bundle"] if snap["bundle"] in self.ctx_ids else "default"

    @objc.python_method
    def apply_snapshot(self, snap):
        self.snap = snap
        if snap["fb"]:
            self.target_browser = snap["fb"]
        tb = self.buttons.get("dash.tabs")
        if tb:
            set_title(tb, snap["title"])
        self.render_np()
        self.apply_caps(snap["caps"])
        ctx, dlg = self.context_key(snap), snap["dialog"]
        changed = ctx != self.ctx or dlg != self.dlg_sig
        self.ctx, self.dlg_sig = ctx, dlg
        if changed:
            self.set_dialog(dlg)
            self.refresh_bar()
            AppHelper.callLater(0.15, self.reapply)

    @objc.python_method
    def apply_caps(self, on):
        self.caps_on = on
        b = self.buttons.get("dash.caps")
        if b:
            set_title(b, "\u21ea", 16, CAPS_ON if on else FG_DIM)

    @objc.python_method
    def render_np(self):
        btn = self.buttons.get("dash.np")
        if not btn or not self.snap:
            return
        np = self.snap["np"]
        playing = np["state"] == "playing"
        text = np["label"] if playing else (PAUSED_LABEL if np["state"] == "paused" else NOT_PLAYING)
        set_title(btn, text, NP_FONT_SIZE, FG if playing else FG_DIM)
        img = None
        if np["state"] != "none":
            key = (np["art"], np["label"]) if np["art"] else None
            img = self.art_cache.get(key) if key else None
            if img is None:
                img = self.app_icon(np["app"])
                if key and key not in self.art_tried:
                    self.art_tried.add(key)
                    self.fetch_art(np["art"], key)
        btn.setImage_(img)

    @objc.python_method
    def app_icon(self, name):
        bid = ALL_BUNDLES.get(name)
        if not bid:
            return None
        if bid not in self.icon_cache:
            try:
                url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
                self.icon_cache[bid] = square_icon(NSWorkspace.sharedWorkspace().iconForFile_(url.path()))
            except Exception:
                self.icon_cache[bid] = None
        return self.icon_cache[bid]

    @objc.python_method
    def fetch_art(self, spec, key):
        def job():
            data = None
            try:
                if spec.startswith("url:"):
                    data = urllib.request.urlopen(spec[4:], timeout=4).read()
                elif spec.startswith("yt:"):
                    data = urllib.request.urlopen(f"https://i.ytimg.com/vi/{spec[3:]}/mqdefault.jpg", timeout=4).read()
                elif spec == "music":
                    img = sb(PLAYERS["Music"]).currentTrack().artworks()[0].data()
                    data = bytes(img.TIFFRepresentation())
            except Exception:
                data = None
            if data:
                AppHelper.callAfter(self.art_ready, key, data)
        threading.Thread(target=job, daemon=True).start()

    @objc.python_method
    def art_ready(self, key, data):
        img = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
        if img is not None:
            self.art_cache[key] = square_icon(img)
            self.render_np()

    @objc.python_method
    def apply_sys(self, s):
        self.sys_last = s
        for ident, k in (("dash.batt", "batt"), ("dash.ssd", "ssd"), ("dash.ram", "ram"), ("dash.tb", "tb"),
                         ("dash.id", "id")):
            b = self.buttons.get(ident)
            if b and k in s:
                set_title(b, f"Touch Bar: {s[k]}" if k == "tb" else f"ID: {s[k]}" if k == "id" else s[k])

    @objc.python_method
    def reapply(self):
        """push the latest known values into items that were created after the data arrived"""
        if self.snap:
            self.apply_snapshot(self.snap)
        if self.sys_last:
            self.apply_sys(self.sys_last)
        if self.bright_last is not None:
            self.apply_brightness(self.bright_last)
        if self.vol_last_ui:
            self.apply_volume(*self.vol_last_ui)
        self.update_display()

    @objc.python_method
    def update_display(self):
        try:
            sc = NSScreen.screens()[0]
            f, k = sc.frame(), sc.backingScaleFactor()
            try:
                hz = sc.maximumFramesPerSecond()
            except Exception:
                hz = 0
            text = f"{int(f.size.width * k)}x{int(f.size.height * k)}" + (f" @ {hz}Hz" if hz else "")
        except Exception:
            text = "Display N/A"
        self.display_text = text
        b = self.buttons.get("dash.disp")
        if b:
            set_title(b, text)

    # ---------- dialog buttons ----------
    @objc.python_method
    def set_dialog(self, sig):
        if sig:
            self.dlg_gen += 1  # fresh identifiers every time: the bar caches items by identifier
            self.dlg_buttons = list(sig)
            self.dialog_ids = [FLEX] + [f"dash.dlg.{self.dlg_gen}.{i}" for i in range(len(sig))] + [FLEX]
        else:
            self.dlg_buttons, self.dialog_ids = [], []

    @objc.python_method
    def make_dialog_item(self, ident):
        i = int(ident.rsplit(".", 1)[1])
        label, role = self.dlg_buttons[i]
        bezel = ACCENT if role == "accept" else DANGER if role == "destructive" else BG_ACTIVE
        b = make_button(label[:22], max(90, 14 * len(label[:22]) // 2 + 40), self, "dlgTapped:", 14, FG, bezel)
        b.setTag_(i)
        it = NSCustomTouchBarItem.alloc().initWithIdentifier_(ident)
        it.setView_(b)
        return it

    @guard1
    def dlgTapped_(self, sender):
        click_sound()
        if self.worker:
            self.worker.press_dialog(sender.tag())

    # ---------- cava ----------
    @objc.python_method
    def build_cava_item(self, ident):
        btn = make_cava_button(CFG["cava"].get("width", 300), self, "openCava:")
        self.cava_views.append(btn)
        self.cava_item = self.popover(ident, btn, self.new_bar(self.cv_ids()))
        return self.cava_item

    @objc.python_method
    def cava_redraw(self):
        for v in self.cava_views:
            v.setNeedsDisplay_(True)

    @guard1
    def openCava_(self, sender):
        self.cava_item.showPopover_(None)

    @objc.python_method
    def cv_ids(self):
        ids = [f"cv.p.{i}" for i in range(5)] + ["cv.mode"]
        if self.cava.cfg["type"] == "gradient":
            ids += ["cv.ea", "cv.eb"]
        return ids + ["cv.H", "cv.S", "cv.V", "cv.preview"]

    @objc.python_method
    def cava_edit_rgb(self):
        cfg = self.cava.cfg
        return cfg["val"][self.cava_edit_idx] if cfg["type"] == "gradient" else cfg["val"]

    @objc.python_method
    def cava_sync_hsv(self):
        r, g, b = [c / 255.0 for c in self.cava_edit_rgb()]
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        self.cava_hsv = [h * 360.0, s, v]

    @objc.python_method
    def cava_apply_hsv(self):
        h, s, v = self.cava_hsv
        r, g, b = colorsys.hsv_to_rgb((h % 360.0) / 360.0, s, v)
        rgb = [int(round(r * 255)), int(round(g * 255)), int(round(b * 255))]
        if self.cava.cfg["type"] == "gradient":
            self.cava.cfg["val"][self.cava_edit_idx] = rgb
        else:
            self.cava.cfg["val"] = rgb

    @objc.python_method
    def build_cava_picker_item(self, ident):
        it = None
        if ident.startswith("cv.p."):
            i = int(ident.rsplit(".", 1)[1])
            pc = cava_preset(i)
            first = pc["val"] if pc["type"] == "solid" else pc["val"][0]
            b = make_button("\u2197" if pc["type"] == "gradient" else "", 44, self, "cvPreset:", 14, FG, tuple(first))
            b.setTag_(i)
            it = self.custom(ident, b)
        elif ident == "cv.mode":
            b = make_button("Solid", 90, self, "cvMode:", 13)
            self.cv["mode"] = b
            it = self.custom(ident, b)
            self.cv_refresh_labels()
        elif ident in ("cv.ea", "cv.eb"):
            idx = 0 if ident == "cv.ea" else 1
            b = make_button("", 44, self, "cvEnd:", 13)
            b.setTag_(idx)
            self.cv[ident] = b
            it = self.custom(ident, b)
            self.cv_refresh_labels()
        elif ident in ("cv.H", "cv.S", "cv.V"):
            it = self.hsv_slider(ident)
        elif ident == "cv.preview":
            pv = make_cava_button(120)
            self.cava_views.append(pv)
            it = self.custom(ident, pv)
        return it

    @objc.python_method
    def hsv_slider(self, ident):
        it = NSSliderTouchBarItem.alloc().initWithIdentifier_(ident)
        sl = it.slider()
        sl.setMinValue_(0.0)
        sl.setMaxValue_(360.0 if ident == "cv.H" else 1.0)
        it.setMinimumSliderWidth_(90)
        it.setMaximumSliderWidth_(120)
        it.setTarget_(self)
        it.setAction_("cvSlider:")
        self.cv[ident] = it
        self.cv_refresh_labels()
        return it

    @objc.python_method
    def cv_refresh_labels(self):
        h, s, v = self.cava_hsv
        for ident, val, txt in (("cv.H", h, f"H {int(round(h))}"), ("cv.S", s, f"S {int(round(s * 100))}%"),
                                ("cv.V", v, f"V {int(round(v * 100))}%")):
            it = self.cv.get(ident)
            if it is not None:
                it.setLabel_(txt)
                it.slider().setDoubleValue_(val)
        mode = self.cv.get("mode")
        if mode is not None:
            set_title(mode, "Gradient" if self.cava.cfg["type"] == "gradient" else "Solid", 13)
        if self.cava.cfg["type"] == "gradient":
            for idx, ident in enumerate(("cv.ea", "cv.eb")):
                b = self.cv.get(ident)
                if b is not None:
                    b.setBezelColor_(color(tuple(self.cava.cfg["val"][idx])))
                    set_title(b, "\u25cf" if self.cava_edit_idx == idx else "", 13)

    @objc.python_method
    def cv_changed(self, relayout=False):
        self.cava_redraw()
        self.cv_refresh_labels()
        if relayout and hasattr(self, "cava_item"):
            self.cava_item.popoverTouchBar().setDefaultItemIdentifiers_(self.cv_ids())
        self.debounced_save("cava", self.cava.save)

    @objc.python_method
    def debounced_save(self, name, fn):
        self.save_tokens[name] = tok = self.save_tokens.get(name, 0) + 1
        AppHelper.callLater(0.6, lambda: fn() if self.save_tokens.get(name) == tok else None)

    @guard1
    def cvPreset_(self, sender):
        click_sound()
        self.cava.cfg = cava_preset(sender.tag())
        self.cava_edit_idx = 0
        self.cava_sync_hsv()
        self.cv_changed(relayout=True)

    @guard1
    def cvMode_(self, sender):
        click_sound()
        cfg = self.cava.cfg
        if cfg["type"] == "gradient":
            self.cava.cfg = {"type": "solid", "val": list(cfg["val"][0])}
        else:
            a = list(cfg["val"])
            h, s, v = colorsys.rgb_to_hsv(a[0] / 255.0, a[1] / 255.0, a[2] / 255.0)
            r, g, b = colorsys.hsv_to_rgb((h + 0.33) % 1.0, max(s, 0.6), max(v, 0.8))
            self.cava.cfg = {"type": "gradient", "val": [a, [int(r * 255), int(g * 255), int(b * 255)]]}
        self.cava_edit_idx = 0
        self.cava_sync_hsv()
        self.cv_changed(relayout=True)

    @guard1
    def cvEnd_(self, sender):
        self.cava_edit_idx = sender.tag()
        self.cava_sync_hsv()
        self.cv_changed()

    @guard1
    def cvSlider_(self, sender):
        ident, val = str(sender.identifier()), sender.slider().doubleValue()
        self.cava_hsv[{"cv.H": 0, "cv.S": 1, "cv.V": 2}[ident]] = val
        self.cava_apply_hsv()
        self.cava_redraw()
        self.cv_refresh_labels_text_only(ident, val)
        self.debounced_save("cava", self.cava.save)
        if self.cava.cfg["type"] == "gradient":
            self.cv_refresh_labels_chip_only()

    @objc.python_method
    def cv_refresh_labels_text_only(self, ident, val):  # don't reset the slider the finger is on
        it = self.cv.get(ident)
        if it is not None:
            it.setLabel_(f"H {int(round(val))}" if ident == "cv.H" else f"{ident[-1]} {int(round(val * 100))}%")

    @objc.python_method
    def cv_refresh_labels_chip_only(self):
        for idx, ident in enumerate(("cv.ea", "cv.eb")):
            b = self.cv.get(ident)
            if b is not None:
                b.setBezelColor_(color(tuple(self.cava.cfg["val"][idx])))

    # ---------- colour tool ----------
    @objc.python_method
    def ct_ids(self):
        return ["ct.R", "ct.G", "ct.B", "ct.swatch", "ct.fmt", "ct.value", "ct.copy", "ct.type"]

    @objc.python_method
    def build_color_item(self, ident):
        if ident in ("ct.R", "ct.G", "ct.B"):
            it = NSSliderTouchBarItem.alloc().initWithIdentifier_(ident)
            sl = it.slider()
            sl.setMinValue_(0.0)
            sl.setMaxValue_(255.0)
            it.setMinimumSliderWidth_(80)
            it.setMaximumSliderWidth_(110)
            it.setTarget_(self)
            it.setAction_("ctSlider:")
            self.ct[ident] = it
            self.ct_refresh()
            return it
        if ident == "ct.swatch":
            b = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 48, 30))
            b.setTitle_("")
            b.setBordered_(False)
            b.setWantsLayer_(True)
            b.layer().setCornerRadius_(8.0)
            b.setTranslatesAutoresizingMaskIntoConstraints_(False)
            b.widthAnchor().constraintEqualToConstant_(48.0).setActive_(True)
            b.heightAnchor().constraintEqualToConstant_(30.0).setActive_(True)
            self.ct[ident] = b
            self.ct_refresh()
            return self.custom(ident, b)
        if ident == "ct.fmt":
            seg = NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
                COLOR_FORMATS, 0, self, "ctFmt:")
            seg.setSelectedSegment_(self.tool_fmt)
            seg.widthAnchor().constraintEqualToConstant_(200.0).setActive_(True)
            self.ct[ident] = seg
            return self.custom(ident, seg)
        if ident == "ct.value":
            b = make_button("", 150, self, "ctCopy:", 13, FG, TRACK)
            self.ct[ident] = b
            self.ct_refresh()
            return self.custom(ident, b)
        if ident == "ct.copy":
            b = make_button("Copy", 64, self, "ctCopy:", 13)
            self.ct[ident] = b
            return self.custom(ident, b)
        if ident == "ct.type":
            b = make_button("Type", 64, self, "ctType:", 13)
            self.ct[ident] = b
            return self.custom(ident, b)
        return None

    @objc.python_method
    def ct_text(self):
        return format_color(self.tool_rgb, COLOR_FORMATS[self.tool_fmt])

    @objc.python_method
    def ct_refresh(self, skip_sliders=False):
        r, g, b = self.tool_rgb
        if not skip_sliders:
            for ident, val, name in (("ct.R", r, "R"), ("ct.G", g, "G"), ("ct.B", b, "B")):
                it = self.ct.get(ident)
                if it is not None:
                    it.slider().setDoubleValue_(val)
        for ident, val, name in (("ct.R", r, "R"), ("ct.G", g, "G"), ("ct.B", b, "B")):
            it = self.ct.get(ident)
            if it is not None:
                it.setLabel_(f"{name} {val}")
        sw = self.ct.get("ct.swatch")
        if sw is not None:
            sw.layer().setBackgroundColor_(color(self.tool_rgb).CGColor())
        v = self.ct.get("ct.value")
        if v is not None:
            set_title(v, self.ct_text(), 13)

    @guard1
    def openColors_(self, sender):
        self.colors_item.showPopover_(None)

    @guard1
    def ctSlider_(self, sender):
        idx = {"ct.R": 0, "ct.G": 1, "ct.B": 2}[str(sender.identifier())]
        self.tool_rgb[idx] = int(round(sender.slider().doubleValue()))
        self.ct_refresh(skip_sliders=True)
        self.debounced_save("color", lambda: save_json("color_tool.json", {"rgb": self.tool_rgb,
                                                                           "fmt": COLOR_FORMATS[self.tool_fmt]}))

    @guard1
    def ctFmt_(self, sender):
        self.tool_fmt = int(sender.selectedSegment())
        self.ct_refresh(skip_sliders=True)
        save_json("color_tool.json", {"rgb": self.tool_rgb, "fmt": COLOR_FORMATS[self.tool_fmt]})

    @objc.python_method
    def flash(self, ident, text, default):
        b = self.ct.get(ident)
        if b is None:
            return
        set_title(b, text, 13, CAPS_ON)
        AppHelper.callLater(1.0, lambda: set_title(b, default, 13))

    @guard1
    def ctCopy_(self, sender):
        click_sound()
        copy_to_clipboard(self.ct_text())
        self.flash("ct.copy", "Copied", "Copy")

    @guard1
    def ctType_(self, sender):
        click_sound()
        type_text(self.ct_text())
        self.flash("ct.type", "Typed", "Type")

    # ---------- emoji ----------
    @objc.python_method
    def build_emoji_bar(self):
        self.emoji_bar = self.new_bar([f"em.cat.{k}" for k, _ in EMOJI_CATEGORY_LABELS])
        return self.emoji_bar

    @guard1
    def openEmoji_(self, sender):
        self.emoji_bar.setDefaultItemIdentifiers_([f"em.cat.{k}" for k, _ in EMOJI_CATEGORY_LABELS])
        self.items["dash.emoji"].showPopover_(None)

    @objc.python_method
    def build_emoji_item(self, ident):
        if ident.startswith("em.cat."):
            key = ident[len("em.cat."):]
            label = dict(EMOJI_CATEGORY_LABELS).get(key, key)
            b = make_button(label, 104, self, "emojiCat:", 13)
            b.setTag_([k for k, _ in EMOJI_CATEGORY_LABELS].index(key))
            return self.custom(ident, b)
        if ident == "em.back":
            return self.custom(ident, make_button("\u203a", 70, self, "emojiBack:", 20))
        if ident == "em.grid":
            sc = NSScrubber.alloc().initWithFrame_(NSMakeRect(0, 0, 720, 30))
            lay = NSScrubberFlowLayout.alloc().init()
            lay.setItemSize_((46, 30))
            lay.setItemSpacing_(2)
            sc.setScrubberLayout_(lay)
            sc.setDataSource_(self)
            sc.setDelegate_(self)
            sc.setMode_(1)  # NSScrubberModeFree
            sc.setShowsArrowButtons_(False)
            sc.registerClass_forItemIdentifier_(NSScrubberTextItemView, "emoji")
            sc.setTranslatesAutoresizingMaskIntoConstraints_(False)
            sc.widthAnchor().constraintEqualToConstant_(720.0).setActive_(True)
            self.emoji_scrubber = sc
            return self.custom(ident, sc)
        return None

    @guard1
    def emojiCat_(self, sender):
        key = EMOJI_CATEGORY_LABELS[sender.tag()][0]
        self.emoji_items = list(self.emoji_data.get(key, []))
        self.emoji_bar.setDefaultItemIdentifiers_(["em.back", "em.grid"])
        if self.emoji_scrubber is not None:
            self.emoji_scrubber.reloadData()

    @guard1
    def emojiBack_(self, sender):
        self.emoji_bar.setDefaultItemIdentifiers_([f"em.cat.{k}" for k, _ in EMOJI_CATEGORY_LABELS])

    @objc.typedSelector(b"q@:@")
    @guard1
    def numberOfItemsForScrubber_(self, scrubber):
        return len(self.emoji_items)

    @objc.typedSelector(b"@@:@q")
    @guard2
    def scrubber_viewForItemAtIndex_(self, scrubber, idx):
        v = scrubber.makeItemWithIdentifier_owner_("emoji", None)
        tf = v.textField()
        tf.setStringValue_(self.emoji_items[idx])
        tf.setFont_(NSFont.systemFontOfSize_(20))
        return v

    @objc.typedSelector(b"v@:@q")
    @guard2
    def scrubber_didSelectItemAtIndex_(self, scrubber, idx):
        if 0 <= idx < len(self.emoji_items):
            click_sound()
            type_text(self.emoji_items[idx])
        scrubber.setSelectedIndex_(-1)

    # ---------- browser / media actions ----------
    @guard1
    def npClicked_(self, sender):
        click_sound()
        self.worker.click_time = time.time()
        np = (self.snap or {}).get("np", {})
        app = np.get("app", "")
        if app in PLAYERS:
            threading.Thread(target=lambda: sb(PLAYERS[app]).playpause(), daemon=True).start()
        else:
            post_media_key(NX_PLAY)

    @guard1
    def newTab_(self, sender):
        click_sound()
        osa(f'tell application "{self.target_browser}" to tell front window to make new tab')

    @guard1
    def closeTab_(self, sender):
        click_sound()
        press_shortcut("cmd+w")

    @guard1
    def reloadTab_(self, sender):
        click_sound()
        osa(f'tell application "{self.target_browser}" to reload active tab of front window')

    @guard1
    def openTabs_(self, sender):
        name = self.target_browser
        tabs = []
        try:
            w = sb(BROWSERS[name]).windows()[0]
            ts = w.tabs()
            titles = list(ts.arrayByApplyingSelector_("title"))
            act = w.activeTabIndex()
            tabs = [(str(t or "(untitled)"), i + 1 == act) for i, t in enumerate(titles[:MAX_TABS])]
        except Exception:
            log.exception("reading tabs failed")
        if not tabs:
            return
        self.tab_list = tabs
        bar = NSTouchBar.alloc().init()
        bar.setDelegate_(self)
        bar.setDefaultItemIdentifiers_([f"dash.tab.{i}" for i in range(len(tabs))])
        self.tabs_item.setPopoverTouchBar_(bar)
        self.tabs_item.showPopover_(None)

    @guard1
    def switchTab_(self, sender):
        i = sender.tag() + 1
        osa(f'tell application "{self.target_browser}"\nset active tab index of front window to {i}\nactivate\nend tell')
        self.tabs_item.dismissPopover_(None)


def main():
    log.info("starting (python %s)", sys.version.split()[0])
    try:
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(0)  # regular app (Dock icon + status window); use NSApplicationActivationPolicyAccessory to hide
        delegate = Dash.alloc().init()
        app.setDelegate_(delegate)
        AppHelper.runEventLoop()
    except Exception:
        log.exception("fatal")
        die("TouchDash crashed on start. Details are in ~/Library/Logs/TouchDash.log")


if __name__ == "__main__":
    main()
