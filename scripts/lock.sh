#!/bin/sh
# Regenerate requirements.txt and requirements-dev.txt from the .in files:
# every package (direct and indirect) pinned with SHA-256 hashes, and only
# versions uploaded to PyPI at least 14 days ago (most poisoned releases are
# caught and pulled within days). Needs pip-tools: pip install pip-tools
set -eu
cd "$(dirname "$0")/.."
CUTOFF=$(python -c "import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(days=14)).strftime('%Y-%m-%dT00:00:00Z'))")
for f in requirements requirements-dev; do
  CUSTOM_COMPILE_COMMAND="scripts/lock.sh" pip-compile --quiet --generate-hashes --strip-extras \
    --pip-args "--uploaded-prior-to $CUTOFF" "$f.in"
done
echo "locked with versions uploaded before $CUTOFF"
