#!/bin/bash
# Double-click this file to start SDTM Oversight.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Setting up (first run only)…"
  python3 -m venv .venv || { echo "Python 3.10+ is required."; read -r; exit 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[parquet]" || { echo "Install failed."; read -r; exit 1; }
fi
# Warn if this ends up back inside a cloud-synced folder — it makes the venv crawl and
# uploads built subject data.
case "$PWD" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|*/Dropbox/*|*/"Google Drive"/*)
    echo "WARNING: this folder is inside a cloud-synced location ($PWD)."
    echo "         Move it somewhere unsynced (e.g. ~/Developer) — built datasets"
    echo "         contain study data and would be uploaded automatically."
    echo ;;
esac
exec .venv/bin/python -m app.server "$@"
