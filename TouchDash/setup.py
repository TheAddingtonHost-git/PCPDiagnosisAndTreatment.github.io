#!/usr/bin/env python3
from setuptools import setup

APP = ["touchdash.py"]
OPTIONS = {
    "argv_emulation": False,
    "packages": ["objc", "Quartz", "AppKit", "Foundation", "PyObjCTools"],
    "plist": {
        "CFBundleName": "TouchDash",
        "CFBundleDisplayName": "TouchDash",
        "CFBundleIdentifier": "local.touchdash",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "10.15",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    },
}

setup(
    app=APP,
    name="TouchDash",
    version="1.0.0",
    description="A system-wide macOS Touch Bar replacement and control surface.",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
