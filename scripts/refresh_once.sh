#!/usr/bin/env bash
# One full refresh: build all three leagues, commit the data, open alert issues. Used by both workflows.
set -u
python pipeline/build.py || echo "WARN 512 build failed"
DYN=ring python pipeline/build.py || echo "WARN Ring build failed"
python pipeline/build_redraft.py || echo "WARN redraft build failed"
git config user.name "dynasty-hq-bot"
git config user.email "actions@users.noreply.github.com"
git add docs/*.json docs/digest.md docs/redraft docs/ring
if ! git diff --cached --quiet; then
  git commit -q -m "Refresh data $(date -u +%Y-%m-%dT%H:%MZ)"
  for i in 1 2 3; do git pull -q --rebase --autostash origin main && git push -q && break; sleep 5; done
fi
if [ -s out/new_alerts.md ]; then
  gh issue create --title "Dynasty HQ alert · $(head -1 out/new_alerts.md | cut -c3-80)" --body-file out/new_alerts.md || true
  : > out/new_alerts.md
fi
if [ -s out/new_alerts_ring.md ]; then
  gh issue create --title "Ring League alert · $(head -1 out/new_alerts_ring.md | cut -c3-80)" --body-file out/new_alerts_ring.md || true
  : > out/new_alerts_ring.md
fi
