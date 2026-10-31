#!/usr/bin/env sh
# Builds the add-on image for every architecture, pushes it and bumps the add-on repository.
# Usage: scripts/release-addon.sh ["changelog line"]   (env: REGISTRY, ADDON_REPO, ARCHES)
set -eu
cd "$(dirname "$0")/.."
REGISTRY=${REGISTRY:-git.hauptstadt.monster/manzari}
ADDON_REPO=${ADDON_REPO:-../SMSMonster-addon}
ARCHES=${ARCHES:-$(sed -n 's/^  \([a-z0-9]*\): .*/\1/p' addon/build.yaml)}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
NOTES=${1:-"Update SMS Monster to $VERSION"}

platform() {
  case $1 in
    aarch64) echo linux/arm64 ;; armv7) echo linux/arm/v7 ;; armhf) echo linux/arm/v6 ;;
    amd64) echo linux/amd64 ;; i386) echo linux/386 ;;
    *) echo "unknown arch: $1" >&2; exit 1 ;;
  esac
}

[ -d "$ADDON_REPO/.git" ] || { echo "add-on repo not found at $ADDON_REPO (set ADDON_REPO)" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first" >&2; exit 1; }

for arch in $ARCHES; do
  base=$(sed -n "s/^  $arch: //p" addon/build.yaml)
  [ -n "$base" ] || { echo "no build_from entry for $arch in addon/build.yaml" >&2; exit 1; }
  echo "==> building $arch ($base)"
  docker buildx build --platform "$(platform "$arch")" -f addon/Dockerfile \
    --build-arg BUILD_FROM="$base" --build-arg BUILD_ARCH="$arch" --build-arg BUILD_VERSION="$VERSION" \
    -t "$REGISTRY/$arch-smsmonster:$VERSION" --push .
done

git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null || git tag "v$VERSION"
git push origin "v$VERSION"

cd "$ADDON_REPO"
git pull --ff-only
sed -i "s/^version: .*/version: \"$VERSION\"/" smsmonster/config.yaml
printf '## %s\n\n- %s\n\n%s' "$VERSION" "$NOTES" "$(cat smsmonster/CHANGELOG.md)" > smsmonster/CHANGELOG.md
git add -A
git commit -m "Update SMS Monster add-on to $VERSION"
git push
echo "released $VERSION"
