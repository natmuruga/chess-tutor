#!/bin/bash
# usage: ./apply.sh ~/Downloads/0002-xyz.patch v0.1.2
set -e
cd "$(dirname "$0")"
git add -A && git commit -m "Local edits before $2" || true
git am "$1"
git tag "$2"
git push origin main --tags
docker compose up --build -d
echo "Released $2"