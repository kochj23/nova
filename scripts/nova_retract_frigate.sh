#!/bin/sh
cd "$HOME/nova-journal" || { echo "cd failed"; exit 1; }
SLUG="2026-06-30-frigate-nvr-the-camera-brain-you-already-need-but-haven-t-ad"
git rm -f "content/operations/$SLUG.md" 2>&1
git rm -f "static/images/operations/$SLUG.webp" 2>&1
git commit -m "Retract Frigate scout article — already in production (#635). Scout false positive; now guarded." 2>&1
# Rebase onto origin BEFORE pushing so a diverged clone can't silently strand commits
# (the failure mode that let host .6 drift 82 ahead / 25 behind unnoticed).
if git pull --rebase --autostash origin main 2>&1; then
    if git push 2>&1; then
        echo "pushed OK"
    else
        echo "PUSH FAILED — commit is NOT on origin" >&2
    fi
else
    git rebase --abort 2>/dev/null
    echo "PUSH ABORTED — pull --rebase failed (repo diverged/conflict); needs manual repair" >&2
fi
echo "RETRACT DONE $(date)"
