#!/usr/bin/env sh
# Copies the application source into the Home Assistant add-on folder so the add-on builds self-contained.
set -eu
cd "$(dirname "$0")/.."
rm -rf smsmonster-addon/app
mkdir -p smsmonster-addon/app
cp -r src pyproject.toml README.md smsmonster-addon/app/
find smsmonster-addon/app -name __pycache__ -type d -exec rm -rf {} +
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
sed -i "s/^version: .*/version: \"$VERSION\"/" smsmonster-addon/config.yaml
echo "synced add-on app (version $VERSION)"
