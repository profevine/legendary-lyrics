#!/usr/bin/env bash
# Launcher for a source checkout. Prefers the project's virtualenv when one
# exists, and otherwise falls back to the system interpreter — which is all
# that's needed when the dependencies come from distro packages.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -x "$DIR/.venv/bin/python3" ]]; then
    PYTHON="$DIR/.venv/bin/python3"
elif PYTHON="$(command -v python3)"; then
    :
else
    echo "run.sh: no python3 found on PATH" >&2
    exit 1
fi

# find_spec only looks the modules up, so this costs a few milliseconds
# rather than the ~300 ms an actual `import textual` would.
missing="$("$PYTHON" -c 'import importlib.util as u; print(" ".join(m for m in ("textual","httpx","PIL") if u.find_spec(m) is None))')"

if [[ -n "$missing" ]]; then
    cat >&2 <<MSG
run.sh: missing Python modules: $missing
Install them from your distro (Arch: python-textual python-httpx python-pillow),
or set up a virtualenv in the checkout:
  python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"
MSG
    exit 1
fi

exec "$PYTHON" "$DIR/legendary_lyrics.py" "$@"
