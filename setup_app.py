"""
py2app setup for Switch2 Bridge

Usage:
    python setup_app.py py2app
"""

import os
import re

from setuptools import setup

APP = ['Switch2Bridge.py']
ICON = 'AppIcon.icns'

# Single source of truth for the version (avoids importing the app module,
# which has import-time side effects)
with open('Switch2Bridge.py') as f:
    VERSION = re.search(r'^APP_VERSION = "(.+)"', f.read(), re.M).group(1)

LIBUSB = '/opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib'

OPTIONS = {
    'argv_emulation': False,
    'iconfile': ICON,
    'plist': {
        'CFBundleName': 'Switch2 Bridge',
        'CFBundleDisplayName': 'Switch2 Bridge',
        'CFBundleIdentifier': 'com.aureliendesert.switch2bridge',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundleIconFile': 'AppIcon',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,  # Menubar only, no dock icon
        'NSBluetoothAlwaysUsageDescription': 
            'Switch2 Bridge needs Bluetooth to connect to your controller.',
        'NSBluetoothPeripheralUsageDescription': 
            'Switch2 Bridge needs Bluetooth to connect to your controller.',
        'NSAccessibilityUsageDescription':
            'Only the optional legacy keyboard bridge needs accessibility access. '
            'The default DSU gamepad output does not.',
    },
    'packages': ['bleak', 'pynput', 'rumps', 'objc'],
    # Ship libusb so wired mode works without Homebrew. pyusb otherwise
    # resolves it through a hardcoded /opt/homebrew path.
    'frameworks': [LIBUSB] if os.path.exists(LIBUSB) else [],
    'includes': ['Foundation', 'AppKit', 'CoreBluetooth', 'ApplicationServices',
                 'ServiceManagement', 'dsu_server', 'controller_state', 'controller_commands', 'outputs', 'usb_transport'],
}

setup(
    app=APP,
    name='Switch2 Bridge',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
