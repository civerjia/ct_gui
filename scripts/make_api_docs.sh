#!/bin/sh
# Regenerate the API reference from the docstrings.
#
# The docstrings ARE the API documentation -- they carry the measured numbers,
# the failure modes and the "do not do this" notes, and they are what a reader
# sees in an editor. Duplicating them into the README meant two copies drifting
# apart, and the README growing past 2800 lines as a reference nobody could
# navigate. The README is a guide now; this is the reference.
#
# Output is NOT committed (see .gitignore): it is derived, it is large, and a
# regenerated copy churns the diff on every docstring edit. Run this instead.
set -e
cd "$(dirname "$0")/.."          # the repository root
python3 -m pdoc ct.client \
    --output-directory docs/api \
    --no-show-source \
    --no-search \
    --docformat markdown
echo "-> docs/api/ct/client.html"
