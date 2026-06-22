#!/usr/bin/env bash
# Cpt — commit, push, bump tag, push tag → triggers the GHCR image build.
# Usage:  ./deploy.sh "commit message"        (message optional)
# Then on EDJ Labs: Update the stack once the GitHub Actions workflow is green.
set -euo pipefail
cd "$(dirname "$0")"

# Identity stays anonymous — never a real name, never a Claude co-author.
git config user.name  "PhytoPlancton"
git config user.email "PhytoPlancton@users.noreply.github.com"

# Safety: a secret file must never be committed.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "✋ .env is TRACKED — aborting. Secrets must never be committed."; exit 1
fi

MSG="${1:-update}"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "$MSG"
fi
git push origin main

# Next patch tag from the latest vX.Y.Z (default v0.1.0).
LAST="$(git tag --list 'v*' --sort=-v:refname | head -1)"
if [ -z "$LAST" ]; then
  NEXT="v0.1.0"
else
  ver="${LAST#v}"; MA="${ver%%.*}"; rest="${ver#*.}"; MI="${rest%%.*}"; PA="${rest#*.}"
  NEXT="v${MA}.${MI}.$((PA + 1))"
fi
git tag "$NEXT"
git push origin "$NEXT"

echo "✅ $NEXT pushed → GitHub Actions builds ghcr.io/phytoplancton/ripgpt:$NEXT (+ :latest)"
echo "   Watch: https://github.com/PhytoPlancton/ripgpt/actions"
echo "   When green → EDJ Labs → Update the ripgpt stack."
