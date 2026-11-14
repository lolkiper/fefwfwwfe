#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install -q -r requirements-build.txt
python3 -m PyInstaller --noconfirm twitch-drops-unlink.spec
ARCHIVE="release/TwitchDropsUnlink-linux-x86_64.tar.gz"
mkdir -p release
tar -czf "$ARCHIVE" -C dist TwitchDropsUnlink
echo "Готово: $ARCHIVE"
echo "Запуск: dist/TwitchDropsUnlink/TwitchDropsUnlink"