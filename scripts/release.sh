#!/usr/bin/env bash
# Release helper: bumps the pinned image tag everywhere it appears,
# commits the bump on main, creates an annotated git tag, and prints
# the single push command that fires CI.
#
# Usage:
#   scripts/release.sh 0.1.2
#   scripts/release.sh 0.1.2 "one-line release summary for the tag message"
#
# What it touches:
#   - docker-compose.yaml         (3x image:  ${WINDROSE_IMAGE:-...:VERSION})
#   - helm/windrose/values.yaml   (tag: "VERSION")
#   - helm/windrose/Chart.yaml    (version: VERSION, appVersion: "VERSION")
#
# Safety rails:
#   - Refuses if not on `main`
#   - Refuses if the working tree has uncommitted changes (other than
#     what this script is about to write)
#   - Refuses if the tag already exists locally or on origin
#   - Refuses if the version arg isn't strict semver (X.Y.Z)
#   - Validates with `docker compose config` + `helm lint` before
#     committing — catches typos in the sed before they hit main
#
# Does NOT push for you. After the script returns clean, you run:
#   git push --follow-tags origin main
# That single push lands the bump + the tag together; the tag push
# fires the publish-images.yml + publish-chart.yml workflows, which
# build + tag :VERSION on GHCR and publish the chart.
set -euo pipefail

NEW="${1:-}"
TAG_MSG="${2:-Release v${NEW}}"

if ! [[ "${NEW}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: $0 <X.Y.Z> [\"tag message\"]" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Refuse outside of main — release commits go on main, not feature branches.
branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "${branch}" != "main" ]; then
  echo "error: must be on main to cut a release (currently on '${branch}')" >&2
  exit 1
fi

# Refuse on a dirty tree. We're about to make commits; conflating those
# with the operator's WIP is an easy way to ship the wrong thing.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: working tree has uncommitted changes; commit or stash first" >&2
  git status --short >&2
  exit 1
fi

# Refuse if the tag already exists either locally or on origin. Re-using
# a release version is a recipe for confusion (which image does :0.1.2
# resolve to? the old one or the new one?).
if git rev-parse "v${NEW}" >/dev/null 2>&1; then
  echo "error: tag v${NEW} already exists locally" >&2
  exit 1
fi
if git ls-remote --exit-code --tags origin "v${NEW}" >/dev/null 2>&1; then
  echo "error: tag v${NEW} already exists on origin" >&2
  exit 1
fi

echo "[release] bumping pinned image tag to ${NEW}"

# docker-compose.yaml: three image: lines, all the same tag.
sed -i -E "s|(windrose-server:)[0-9]+\.[0-9]+\.[0-9]+|\1${NEW}|g" docker-compose.yaml

# helm/windrose/values.yaml: tag: "X.Y.Z"
sed -i -E "s|^(  tag: \")[0-9]+\.[0-9]+\.[0-9]+(\")|\1${NEW}\2|" helm/windrose/values.yaml

# helm/windrose/Chart.yaml: version (chart) + appVersion (app)
sed -i -E "s|^(version: )[0-9]+\.[0-9]+\.[0-9]+|\1${NEW}|" helm/windrose/Chart.yaml
sed -i -E "s|^(appVersion: \")[0-9]+\.[0-9]+\.[0-9]+(\")|\1${NEW}\2|" helm/windrose/Chart.yaml

# Sanity: every reference now matches.
echo "[release] new pins:"
grep -nE "windrose-server:[0-9]+\.[0-9]+\.[0-9]+" docker-compose.yaml
grep -nE "^  tag:" helm/windrose/values.yaml
grep -nE "^(version|appVersion):" helm/windrose/Chart.yaml

# Lint before committing — typos in the sed shouldn't make it to main.
echo "[release] validating compose + helm"
docker compose config --quiet
helm lint ./helm/windrose >/dev/null

# Commit + annotated tag. Pushing is the operator's job.
git add docker-compose.yaml helm/windrose/values.yaml helm/windrose/Chart.yaml
git commit -m "release: pin to v${NEW}"
git tag -a "v${NEW}" -m "${TAG_MSG}"

echo
echo "[release] ready to ship v${NEW}. To publish:"
echo "  git push --follow-tags origin main"
echo
echo "  → publish-images.yml will tag ghcr.io/shipstuff/windrose-server:${NEW}"
echo "  → publish-chart.yml will publish oci://ghcr.io/shipstuff/charts/windrose:${NEW}"
echo
echo "  Reverting (if you change your mind before pushing):"
echo "    git tag -d v${NEW} && git reset --hard HEAD~1"
