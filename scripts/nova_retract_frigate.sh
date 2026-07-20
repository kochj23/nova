#!/bin/sh
cd "$HOME/nova-journal" || { echo "cd failed"; exit 1; }
SLUG="2026-06-30-frigate-nvr-the-camera-brain-you-already-need-but-haven-t-ad"
git rm -f "content/operations/$SLUG.md" 2>&1
git rm -f "static/images/operations/$SLUG.webp" 2>&1
git commit -m "Retract Frigate scout article — already in production (#635). Scout false positive; now guarded." 2>&1
git push 2>&1
echo "RETRACT DONE $(date)"
