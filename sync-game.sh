#!/usr/bin/env bash
# Single source of truth for the game is the standalone index.html. This copies it to the two
# served/deploy locations so they can't drift. Run after editing the game, then commit.
#   ./sync-game.sh && bench --site hikmat.local execute hikmat.setup_data.export_offline_curriculum
set -euo pipefail
SRC="${HIKMAT_GAME_SRC:-/Users/fossdot/code/Hikmat Games/index.html}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cp "$SRC" "$ROOT/index.html"
cp "$SRC" "$ROOT/hikmat/public/game.html"
echo "synced $SRC → index.html + hikmat/public/game.html"
