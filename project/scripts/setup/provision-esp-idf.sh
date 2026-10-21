#!/usr/bin/env bash
# Explicit, project-local SDK setup. Invoke with bash from fish; never source into fish.
set -euo pipefail
rtos_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
rtos_idf_dir="$rtos_project_dir/.local/sdk/esp-idf"
export IDF_TOOLS_PATH="$rtos_project_dir/.local/toolchains/esp-idf"
read -r rtos_idf_revision rtos_idf_tag < <(python3 -c 'import sys,tomllib; p=tomllib.load(open(sys.argv[1],"rb"))["sources"]["esp-idf"]; print(p["revision"],p["tag"])' "$rtos_project_dir/config/sources.toml")
[[ "$rtos_idf_revision" =~ ^[0-9a-f]{40}$ ]] || { printf '%s\n' 'A full ESP-IDF commit pin is required.' >&2; exit 2; }
mkdir -p "$rtos_project_dir/.local/sdk"
exec 9>"$rtos_project_dir/.local/sdk/esp-idf-setup.lock"
flock -n 9 || { printf '%s\n' 'ESP-IDF setup is already running.' >&2; exit 2; }
if [[ ! -d "$rtos_idf_dir/.git" ]]; then
    [[ ! -e "$rtos_idf_dir" ]] || { printf '%s\n' 'Existing SDK directory is not a Git checkout.' >&2; exit 2; }
    git clone --depth 1 --branch "$rtos_idf_tag" https://github.com/espressif/esp-idf.git "$rtos_idf_dir"
fi
[[ "$(git -C "$rtos_idf_dir" rev-parse HEAD)" == "$rtos_idf_revision" ]] || { printf '%s\n' 'SDK checkout does not match the configured commit; no files changed.' >&2; exit 2; }
git -C "$rtos_idf_dir" submodule update --init --recursive --depth 1
cd -- "$rtos_idf_dir"
./install.sh esp32c6
git submodule status --recursive > "$rtos_project_dir/.local/sdk/esp-idf-submodules.txt"
printf '%s\n' 'ESP-IDF is prepared locally. Build through rtos; interactive fish configuration is unchanged.'
