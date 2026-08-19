#!/usr/bin/env bash
# Single source of truth for the game is the standalone index.html. This copies it to the
# served/deploy locations so they can't drift. Run after editing the game, then commit.
#   ./sync-game.sh && bench --site hikmat.local execute hikmat.setup_data.export_offline_curriculum
set -euo pipefail
SRC="${HIKMAT_GAME_SRC:-/Users/fossdot/code/Hikmat Games/index.html}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
cp "$SRC" "$ROOT/index.html"
cp "$SRC" "$ROOT/hikmat/public/game.html"
echo "synced $SRC → index.html + hikmat/public/game.html"

# THIRD copy: the Capacitor bundle inside the Android app. It is a separate checkout outside
# this repo, so it used to be invisible here — and it silently fell 3 weeks / 1008 lines behind,
# which would have shipped a Play Store build missing a data-loss fix and an XSS fix, plus a
# curriculum stuck at 89 of 283 lessons. Re-bundle it whenever the game changes.
# Skipped without complaint when the Android checkout isn't present (CI, another machine).
ANDROID_APP="${HIKMAT_ANDROID_APP:-$(cd "$(dirname "$SRC")" && pwd)/android-app}"
if [ -f "$ANDROID_APP/bundle-game.js" ]; then
  if command -v node >/dev/null 2>&1; then
    ( cd "$ANDROID_APP" && node bundle-game.js ) \
      && echo "synced → android-app/www/ (run android-app/build.sh to produce a new .aab)" \
      || echo "WARNING: android bundle FAILED — the Play Store build would ship a stale game"
  else
    echo "WARNING: node not found — android-app/www/ NOT refreshed (it will ship stale)"
  fi
else
  echo "note: no android-app/bundle-game.js here — skipping the Capacitor bundle"
fi
