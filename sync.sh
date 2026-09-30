#!/bin/bash
set -e
cd "$(dirname "$0")"

git fetch origin

FORCE=false
[ "$1" = "--force" ] && FORCE=true

UNPUSHED="$(git log origin/main..HEAD --oneline)"
DIRTY="$(git status --porcelain)"

if [ "$FORCE" = false ] && { [ -n "$UNPUSHED" ] || [ -n "$DIRTY" ]; }; then
  echo "Refusing to sync: this would destroy local work that isn't on GitHub."
  if [ -n "$UNPUSHED" ]; then
    echo
    echo "Unpushed commits (would be discarded):"
    echo "$UNPUSHED" | sed 's/^/  /'
  fi
  if [ -n "$DIRTY" ]; then
    echo
    echo "Uncommitted/untracked changes (would be discarded):"
    echo "$DIRTY" | sed 's/^/  /'
  fi
  echo
  echo "Push or commit what you want to keep first (e.g. ./deploy.sh), then re-run."
  echo "Or run './sync.sh --force' to discard all of the above and match GitHub exactly."
  exit 1
fi

echo "Overwriting local files with what's on GitHub..."
git reset --hard origin/main
git clean -fd
echo "Done! Local files now match GitHub."
