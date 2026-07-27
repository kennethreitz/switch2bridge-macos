#!/bin/bash
#
# Switch2 Bridge - Build DMG
# ==========================

set -e

APP_NAME="Switch2 Bridge"
DMG_NAME="Switch2Bridge-Installer"
VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)".*/\1/p' Switch2Bridge.py)

echo ""
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║          Switch2 Bridge - Build DMG                       ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────
# 1. Check Python
# ─────────────────────────────────────────────────────────────
echo "📋 Checking requirements..."

if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 required"
    exit 1
fi
echo "   ✓ Python 3"

# ─────────────────────────────────────────────────────────────
# 2. Setup venv
# ─────────────────────────────────────────────────────────────
echo ""
echo "📦 Setting up virtual environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "   ✓ Dependencies installed"

# ─────────────────────────────────────────────────────────────
# 3. Build .app
# ─────────────────────────────────────────────────────────────
echo ""
echo "🔨 Building application..."

rm -rf build dist

BUILD_LOG="build_py2app.log"
if ! python setup_app.py py2app > "$BUILD_LOG" 2>&1; then
    echo "❌ Build failed — last lines of ${BUILD_LOG}:"
    tail -n 25 "$BUILD_LOG"
    exit 1
fi

if [ ! -d "dist/${APP_NAME}.app" ]; then
    echo "❌ Build failed (no .app produced) — see ${BUILD_LOG}"
    exit 1
fi

echo "   ✓ ${APP_NAME}.app"

# ─────────────────────────────────────────────────────────────
# 4. Create DMG
# ─────────────────────────────────────────────────────────────
echo ""
echo "📀 Creating DMG..."

DMG_TMP="dist/dmg_tmp"
rm -rf "$DMG_TMP"
mkdir -p "$DMG_TMP"

# Copy app
cp -R "dist/${APP_NAME}.app" "$DMG_TMP/"

# Symlink to Applications
ln -s /Applications "$DMG_TMP/Applications"

# Create README
cat > "$DMG_TMP/README.txt" << 'README'
Switch2 Bridge - Installation
=============================

1. Drag "Switch2 Bridge" to "Applications"

2. Launch from Applications
   (Right-click → Open the first time)

3. Grant Bluetooth when macOS prompts you.
   That is the only permission needed.

4. Click the controller icon in the menu bar,
   then "Connect Controller".


The controller is exposed to emulators as a real
analog gamepad over DSU (cemuhook) on
127.0.0.1:26760 — no driver, no Accessibility.

  Dolphin  Options > Controller Settings >
           Alternate Input Sources > DSU Client
           Add 127.0.0.1:26760

  Cemu     Input settings > add a DSUController
           at the same address

  Ryujinx  Settings > Input > add controller.
           Motion needs "Use CemuHook compatible
           motion" (see note below).

The menu bar shows how many DSU clients are
attached, so you can tell whether the emulator
actually connected.

Motion/gyro is not available yet: the controller
does not stream IMU data in its BLE reports.

A legacy keyboard bridge is still included for
emulators that only read the keyboard. It is OFF
by default and needs Accessibility; enable it from
the menu bar ("Keyboard bridge").
README

# Build DMG
rm -f "dist/${DMG_NAME}.dmg"

hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "$DMG_TMP" \
    -ov \
    -format UDZO \
    "dist/${DMG_NAME}.dmg" \
    -quiet

rm -rf "$DMG_TMP"

echo "   ✓ ${DMG_NAME}.dmg"

# ─────────────────────────────────────────────────────────────
# Done!
# ─────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✅ Build complete!"
echo ""
echo "   📀 dist/${DMG_NAME}.dmg"
echo ""
echo "   To test: open dist/${DMG_NAME}.dmg"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Open in Finder (skip on CI / headless)
if [ -z "${CI:-}" ]; then
    open dist
fi
