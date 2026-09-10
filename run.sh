#!/bin/sh
# run vimview with the project's venv
here="$(dirname "$0")"
exec "$here/.venv/bin/python" "$here/vimview.py" "$@"
