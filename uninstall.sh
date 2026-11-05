#!/usr/bin/env bash
set -euo pipefail
rm -f \
  "${HOME}/.local/bin/player" \
  "${HOME}/.local/bin/lee-player" \
  "${HOME}/.local/share/applications/lee-player.desktop"
rm -rf "${HOME}/.local/share/lee-player"
echo "Removed the program. Saved settings under ~/.config were left in place."
