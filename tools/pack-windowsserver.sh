#!/bin/bash
# Pack the Windrose dedicated-server bundle from a local Steam install into a
# tarball ready to upload to the self-hosted container.
#
# Intended to run on the operator's WSL or Linux workstation where Windrose is
# installed via Steam. Uses Steam's libraryfolders.vdf to find the install.

set -euo pipefail

OUTPUT="${1:-${HOME}/windrose-server.tgz}"

log() { printf '[pack] %s\n' "$*" >&2; }

candidate_libraryfolders=(
  "/mnt/c/Program Files (x86)/Steam/steamapps/libraryfolders.vdf"
  "/mnt/c/Program Files/Steam/steamapps/libraryfolders.vdf"
  "${HOME}/.steam/steam/steamapps/libraryfolders.vdf"
  "${HOME}/.local/share/Steam/steamapps/libraryfolders.vdf"
)

# Translate a Windows-style path under /mnt/<drive> where needed.
translate_wsl_path() {
  local p="$1"
  if [[ "$p" =~ ^([A-Za-z]):\\ ]] || [[ "$p" =~ ^([A-Za-z]):/ ]]; then
    local drive="${p:0:1}"
    drive="${drive,,}"
    local rest="${p:2}"
    rest="${rest//\\//}"
    printf '/mnt/%s%s\n' "$drive" "${rest}"
  else
    printf '%s\n' "$p"
  fi
}

find_library_paths() {
  local vdf
  for vdf in "${candidate_libraryfolders[@]}"; do
    [ -f "${vdf}" ] || continue
    # Extract all "path" values from the vdf.
    grep -oE '"path"[[:space:]]+"[^"]+"' "${vdf}" | sed -E 's/.*"([^"]+)"$/\1/' | while read -r raw; do
      raw="${raw//\\\\/\\}"
      translate_wsl_path "${raw}"
    done
  done
}

find_windrose_root() {
  local lib candidate
  while read -r lib; do
    [ -z "${lib}" ] && continue
    for candidate in \
        "${lib}/steamapps/common/Windrose" \
        "${lib}/SteamLibrary/steamapps/common/Windrose"; do
      if [ -d "${candidate}/R5/Builds/WindowsServer" ]; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done
  done < <(find_library_paths)
  return 1
}

WINDROSE_ROOT="${WINDROSE_ROOT:-$(find_windrose_root || true)}"

if [ -z "${WINDROSE_ROOT}" ] || [ ! -d "${WINDROSE_ROOT}/R5/Builds/WindowsServer" ]; then
  log "ERROR: could not locate Windrose game install with R5/Builds/WindowsServer/."
  log "Set WINDROSE_ROOT=<path> to override. Looked in Steam libraryfolders.vdf from:"
  for vdf in "${candidate_libraryfolders[@]}"; do log "  ${vdf}"; done
  exit 1
fi

SRC="${WINDROSE_ROOT}/R5/Builds/WindowsServer"
log "Windrose install: ${WINDROSE_ROOT}"
log "Source:          ${SRC}"
log "Output:          ${OUTPUT}"

if [ -f "${OUTPUT}" ]; then
  log "WARNING: ${OUTPUT} exists and will be overwritten"
fi

# Tar from the Builds parent so WindowsServer/ is the top-level dir in the tarball.
tar -czf "${OUTPUT}" -C "${WINDROSE_ROOT}/R5/Builds" WindowsServer

size_h="$(du -h "${OUTPUT}" | awk '{print $1}')"
log "Packed ${size_h} -> ${OUTPUT}"
log ""
log "Next steps (k8s):"
log "  kubectl -n games port-forward svc/windrose 28080:28080 &"
log "  curl -fsS -X POST http://127.0.0.1:28080/cgi-bin/upload.sh \\"
log "    -H 'X-Filename: $(basename "${OUTPUT}")' \\"
log "    -H 'Content-Type: application/gzip' \\"
log "    --data-binary @${OUTPUT}"
log ""
log "Or open http://windrose.local (or http://127.0.0.1:28080 via port-forward) and drag the file in."
