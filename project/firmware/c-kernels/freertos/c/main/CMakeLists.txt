idf_component_register(SRCS "main.c"
                            "bench.c"
                            "../../../../common/c/src/runner.c"
                        INCLUDE_DIRS "."
                            "../../../../common/c/include"
                        REQUIRES log freertos esp_driver_usb_serial_jtag esp_timer esp_driver_gptimer)

target_compile_options(${COMPONENT_LIB} PRIVATE "-O2" "-Wno-error=stringop-truncation")

# Link the Rust application core (firmware/c-kernels/freertos/rust) instead of the shared C
# runner. The rtos freertos plugin sets this for `--language rust`.
option(BENCH_RUST "Link the Rust application core" OFF)

if(BENCH_RUST)
  set(BENCH_LANG_TAG "rust")
  # ESP32-C6 is RV32IMAC with no FPU; the soft-float target matches the IDF C ABI.
  set(RUST_TARGET riscv32imac-unknown-none-elf)
  set(RUST_CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/../../rust)
  set(RUST_ARTIFACT
      ${CMAKE_CURRENT_LIST_DIR}/../../../../target/${RUST_TARGET}/release/librtos_firmware_freertos.a)
  add_custom_command(
    OUTPUT ${RUST_ARTIFACT}
    COMMAND cargo build --release --target ${RUST_TARGET}
    WORKING_DIRECTORY ${RUST_CRATE_DIR}
    DEPENDS ${RUST_CRATE_DIR}/src/lib.rs
            ${RUST_CRATE_DIR}/src/boundary.rs
            ${RUST_CRATE_DIR}/Cargo.toml
            ${CMAKE_CURRENT_LIST_DIR}/../../../../common/rust/src/lib.rs
    COMMENT "Building Rust FreeRTOS adapter staticlib"
    VERBATIM)
  add_custom_target(bench_rust_adapter DEPENDS ${RUST_ARTIFACT})
  add_dependencies(${COMPONENT_LIB} bench_rust_adapter)
  # INTERFACE so the archive reaches the final app link, after the component
  # objects that reference rust_bench_run_case.
  target_link_libraries(${COMPONENT_LIB} INTERFACE ${RUST_ARTIFACT})
  target_compile_definitions(${COMPONENT_LIB} PRIVATE BENCH_LANGUAGE_RUST)
else()
  set(BENCH_LANG_TAG "bench")
endif()

# Inject the git short SHA as the protocol build_id
# ("freertos-c6-bench-<sha8>" / "freertos-c6-rust-<sha8>").
execute_process(
    COMMAND git -C ${CMAKE_SOURCE_DIR} rev-parse --short=8 HEAD
    OUTPUT_VARIABLE BENCH_GIT_SHA
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET)
if(NOT BENCH_GIT_SHA)
    set(BENCH_GIT_SHA "nogit")
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE
    BENCH_BUILD_ID="freertos-c6-${BENCH_LANG_TAG}-${BENCH_GIT_SHA}")

# Positive control for case 20 (nested_irq_overhead): NESTED_CONTROL=1 disables
# the higher interrupt so the reported delta falls to ~0.
set(BENCH_NESTED_CONTROL "$ENV{NESTED_CONTROL}" CACHE STRING "case 20 positive control (0/1)")
if(BENCH_NESTED_CONTROL STREQUAL "")
    set(BENCH_NESTED_CONTROL 0)
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE NESTED_CONTROL=${BENCH_NESTED_CONTROL})
