#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SHARE="${HOME}/.local/share/lee-player"
BIN="${HOME}/.local/bin"
mkdir -p "$SHARE" "$BIN"
install -m 0755 "$ROOT/lee_player_widget.py" "$SHARE/lee_player_widget.py"
install -m 0644 "$ROOT/desklet_geometry.py" "$SHARE/desklet_geometry.py"
ln -sfn "$SHARE/lee_player_widget.py" "$BIN/player"
ln -sfn "$SHARE/lee_player_widget.py" "$BIN/lee-player"
echo "Installed."
echo "  Run: player"
echo "  It does not start at login until you tick Open at login."
