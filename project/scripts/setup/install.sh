#!/usr/bin/env bash
# Install the `rtos` CLI from this checkout for the current user.
#
# Resolves the repository from this script's own location (so it can be run from
# anywhere, including paths with spaces), builds the CLI from the committed
# lockfile, installs the executable into a user-owned prefix, and configures a
# PATH snippet for the selected shell(s). No sudo is required for the default
# prefix. Fish is the primary acceptance shell; bash/zsh are supported and
# tested but secondary.
#
# Usage: scripts/setup/install.sh [--prefix DIR] [--shell fish|bash|zsh|all] [--force] [--debug]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PREFIX=""
FORCE=0
PROFILE="release"
SHELLS="fish"

RC_BEGIN="# >>> rtos installer >>>"
RC_END="# <<< rtos installer <<<"

usage() {
    cat >&2 <<'EOF'
Usage: install.sh [--prefix DIR] [--shell fish|bash|zsh|all] [--force] [--debug]
  --prefix DIR   Install under DIR (default: $HOME/.local)
  --shell SHELL  Configure PATH for fish (default), bash, zsh, or all
  --force        Replace an existing rtos executable not owned by this installer
  --debug        Install the debug build instead of the release build
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"; shift ;;
        --shell)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            SHELLS="$2"; shift 2 ;;
        --shell=*)
            SHELLS="${1#--shell=}"; shift ;;
        --force) FORCE=1; shift ;;
        --debug) PROFILE="debug"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'install: unknown argument: %s\n' "$1" >&2; usage; exit 2 ;;
    esac
done

case "${SHELLS}" in
    fish|bash|zsh|all) ;;
    *) printf 'install: --shell must be fish, bash, zsh, or all\n' >&2; exit 2 ;;
esac

: "${HOME:?install: HOME is not set}"
[ -n "${PREFIX}" ] || PREFIX="${HOME}/.local"

command -v cargo >/dev/null 2>&1 || {
    printf 'install: cargo is required (install the Rust toolchain first)\n' >&2
    exit 1
}

BIN_DIR="${PREFIX}/bin"
STATE_DIR="${PREFIX}/share/rtos"
MANIFEST="${STATE_DIR}/install.manifest"
TARGET="${BIN_DIR}/rtos"

want_shell() {
    [ "${SHELLS}" = "all" ] || [ "${SHELLS}" = "$1" ]
}

# Refuse to overwrite an executable that this installer does not own.
if [ -e "${TARGET}" ] && [ "${FORCE}" -ne 1 ]; then
    if ! { [ -f "${MANIFEST}" ] && grep -qxF "${TARGET}" "${MANIFEST}"; }; then
        printf 'install: %s exists and was not installed by this script; pass --force to replace\n' "${TARGET}" >&2
        exit 1
    fi
fi

printf 'install: building rtos (%s) from %s\n' "${PROFILE}" "${REPO_ROOT}" >&2
if [ "${PROFILE}" = "release" ]; then
    ( cd "${REPO_ROOT}" && cargo build --locked -p rtos-cli --release )
else
    ( cd "${REPO_ROOT}" && cargo build --locked -p rtos-cli )
fi

BUILT="${REPO_ROOT}/target/${PROFILE}/rtos"
[ -x "${BUILT}" ] || { printf 'install: build did not produce %s\n' "${BUILT}" >&2; exit 1; }

install -d "${BIN_DIR}" "${STATE_DIR}"

# Atomic replace: stage next to the target, then rename; nothing is renamed
# until the copy is done, so a failed install leaves the previous binary intact.
STAGE="${BIN_DIR}/.rtos.install.$$"
cp "${BUILT}" "${STAGE}"
chmod 0755 "${STAGE}"
mv -f "${STAGE}" "${TARGET}"

OWNED=("${TARGET}")

# Fish: an installation-owned conf.d snippet, sourced per interactive session.
if want_shell fish; then
    SNIPPET_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/fish/conf.d"
    SNIPPET="${SNIPPET_DIR}/rtos.fish"
    install -d "${SNIPPET_DIR}"
    cat > "${SNIPPET}" <<EOF
# Managed by scripts/setup/install.sh; removed by uninstall.sh.
set -l rtos_bin "${BIN_DIR}"
if test -d \$rtos_bin; and not contains -- \$rtos_bin \$PATH
    set -gx PATH \$rtos_bin \$PATH
end
EOF
    OWNED+=("${SNIPPET}")
fi

# Bash/Zsh: a marker-delimited block in the user's rc file; re-running replaces
# only the managed block, preserving the rest of the file.
ensure_rc_block() {
    local rc="$1"
    install -d "$(dirname "${rc}")"
    touch "${rc}"
    local tmp="${rc}.rtos.$$"
    awk -v b="${RC_BEGIN}" -v e="${RC_END}" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        skip!=1 {print}
    ' "${rc}" > "${tmp}"
    {
        printf '%s\n' "${RC_BEGIN}"
        printf 'export PATH="%s:$PATH"\n' "${BIN_DIR}"
        printf '%s\n' "${RC_END}"
    } >> "${tmp}"
    mv -f "${tmp}" "${rc}"
    printf 'install: configured %s\n' "${rc}" >&2
}
if want_shell bash; then
    ensure_rc_block "${HOME}/.bashrc"
    OWNED+=("${HOME}/.bashrc")
fi
if want_shell zsh; then
    ensure_rc_block "${HOME}/.zshrc"
    OWNED+=("${HOME}/.zshrc")
fi

printf '%s\n' "${OWNED[@]}" | awk '!seen[$0]++' > "${MANIFEST}"

printf 'install: installed %s\n' "${TARGET}" >&2

# Warn if a different rtos already resolves on PATH; it may shadow this install.
FOUND="$(command -v rtos || true)"
if [ -n "${FOUND}" ] && [ "${FOUND}" != "${TARGET}" ]; then
    printf 'install: warning: another rtos resolves on PATH: %s (may shadow %s)\n' \
        "${FOUND}" "${TARGET}" >&2
fi

case "${SHELLS}" in
    fish|all) printf 'Activate in this fish session:\n  fish_add_path "%s"\n' "${BIN_DIR}" ;;
esac
case "${SHELLS}" in
    bash|all) printf 'Activate in this bash session:\n  export PATH="%s:$PATH"\n' "${BIN_DIR}" ;;
    zsh|all)  printf 'Activate in this zsh session:\n  export PATH="%s:$PATH"\n' "${BIN_DIR}" ;;
esac
