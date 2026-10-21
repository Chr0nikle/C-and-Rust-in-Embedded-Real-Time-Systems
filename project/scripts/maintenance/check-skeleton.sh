#!/usr/bin/env bash
# Host-only checks. No SDK installation, hardware discovery, serial access or flashing.
set -euo pipefail
rtos_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$#" != 0 ]]; then
    printf '%s\n' 'Unexpected check-script arguments' >&2
    exit 2
fi
cd -- "$rtos_project_dir"
# Print each command before running it, so a failing check is identifiable.
check() {
    printf '[check] '
    printf '%q ' "$@"
    printf '\n'
    "$@"
}
check python3 -m unittest discover -s scripts/maintenance/tests -v
check cargo fmt --all -- --check
check cargo clippy --workspace --all-targets --offline --locked -- -D warnings
check cargo test --workspace --offline --locked
check cargo fmt --manifest-path firmware/Cargo.toml --all -- --check
check cargo clippy --manifest-path firmware/Cargo.toml --workspace --all-targets --offline --locked -- -D warnings
check cargo test --manifest-path firmware/Cargo.toml --workspace --offline --locked
check bash -n scripts/setup/install.sh scripts/setup/uninstall.sh scripts/maintenance/check-skeleton.sh
if command -v shellcheck >/dev/null 2>&1; then
    check shellcheck scripts/setup/install.sh scripts/setup/uninstall.sh scripts/maintenance/check-skeleton.sh
else
    printf '%s\n' '[skip] ShellCheck is not installed; bash -n still ran.'
fi
mkdir -p target/host-tests
check cc -std=c11 -Wall -Wextra -Werror -pedantic -Ifirmware/common/c/include \
    firmware/common/c/src/runner.c firmware/common/c/tests/runner.c -o target/host-tests/c-runner
check target/host-tests/c-runner
# The freertos adapter has migrated to a real ESP-IDF firmware (firmware/
# c-kernels/freertos/c/main/main.c) plus an STM32F4 Makefile build; the ESP-IDF
# kernel is reached through this one freertos route for the C6, so there is no
# separate esp-idf adapter to syntax-check. The zephyr adapter includes Zephyr
# SDK headers (zephyr/kernel.h) that are unavailable on the host, so it is
# excluded. NuttX lives under the C-kernel group.
for rtos_adapter in nuttx; do
    check cc -std=c11 -Wall -Wextra -Werror -pedantic -Ifirmware/common/c/include \
        -fsyntax-only "firmware/c-kernels/$rtos_adapter/c/main.c" "firmware/c-kernels/$rtos_adapter/c/port.c"
done
