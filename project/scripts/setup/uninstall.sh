#!/usr/bin/env bash
# Remove files installed by install.sh, preserving projects and user data.
#
# Owned paths are read from the install manifest. Files that carry the rtos
# installer marker block (bash/zsh rc files) have only that block removed; the
# rest of the file and the user's tree are left intact.
#
# Usage: scripts/setup/uninstall.sh [--prefix DIR]
set -euo pipefail

RC_BEGIN="# >>> rtos installer >>>"
RC_END="# <<< rtos installer <<<"

PREFIX=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || { printf 'uninstall: --prefix needs a value\n' >&2; exit 2; }
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"; shift ;;
        -h|--help)
            printf 'Usage: uninstall.sh [--prefix DIR]\n' >&2; exit 0 ;;
        *)
            printf 'uninstall: unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

: "${HOME:?uninstall: HOME is not set}"
[ -n "${PREFIX}" ] || PREFIX="${HOME}/.local"

STATE_DIR="${PREFIX}/share/rtos"
MANIFEST="${STATE_DIR}/install.manifest"

if [ ! -f "${MANIFEST}" ]; then
    printf 'uninstall: no installation recorded at %s; nothing to do\n' "${PREFIX}" >&2
    exit 0
fi

while IFS= read -r path; do
    [ -n "${path}" ] || continue
    [ -f "${path}" ] || continue
    if grep -qxF "${RC_BEGIN}" "${path}"; then
        tmp="${path}.rtos.$$"
        awk -v b="${RC_BEGIN}" -v e="${RC_END}" '
            $0==b {skip=1; next}
            $0==e {skip=0; next}
            skip!=1 {print}
        ' "${path}" > "${tmp}"
        mv -f "${tmp}" "${path}"
        printf 'uninstall: cleaned rtos block from %s\n' "${path}" >&2
    else
        rm -f -- "${path}"
        printf 'uninstall: removed %s\n' "${path}" >&2
    fi
done < "${MANIFEST}"

rm -f -- "${MANIFEST}"
rmdir "${STATE_DIR}" 2>/dev/null || true

printf 'uninstall: done (projects and user data preserved)\n' >&2
