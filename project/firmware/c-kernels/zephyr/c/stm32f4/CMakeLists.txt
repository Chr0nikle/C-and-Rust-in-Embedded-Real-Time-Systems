cmake_minimum_required(VERSION 3.20)
project(zephyr_bench)

# Link the Rust application core (firmware/c-kernels/zephyr/rust) instead of the shared C
# runner. The rtos zephyr plugin sets this for `--language rust`; the default
# stays the C path so existing evidence is unchanged.
option(BENCH_RUST "Link the Rust application core" OFF)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

set(BENCH_SRC
  main.c
  port.c
)
if(NOT BENCH_RUST)
  list(APPEND BENCH_SRC ${CMAKE_CURRENT_SOURCE_DIR}/../../../../common/c/src/runner.c)
endif()

target_sources(app PRIVATE ${BENCH_SRC})
set_source_files_properties(${BENCH_SRC} PROPERTIES COMPILE_OPTIONS "-O2")
target_include_directories(app PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../../../common/c/include)

if(BENCH_RUST)
  set(BENCH_LANG_TAG "rust")
  # The C image is soft-float (CONFIG_FPU is unset), so the staticlib must be
  # built for the soft-float EABI; an eabihf library would not match the C ABI.
  set(RUST_TARGET thumbv7em-none-eabi)
  set(RUST_CRATE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../rust)
  set(RUST_ARTIFACT
      ${CMAKE_CURRENT_SOURCE_DIR}/../../../../target/${RUST_TARGET}/release/librtos_firmware_zephyr.a)
  add_custom_command(
    OUTPUT ${RUST_ARTIFACT}
    COMMAND cargo build --release --target ${RUST_TARGET}
    WORKING_DIRECTORY ${RUST_CRATE_DIR}
    DEPENDS ${RUST_CRATE_DIR}/src/lib.rs
            ${RUST_CRATE_DIR}/src/boundary.rs
            ${RUST_CRATE_DIR}/Cargo.toml
            ${CMAKE_CURRENT_SOURCE_DIR}/../../../../common/rust/src/lib.rs
    COMMENT "Building Rust Zephyr adapter staticlib"
    VERBATIM)
  add_custom_target(bench_rust_adapter DEPENDS ${RUST_ARTIFACT})
  add_dependencies(app bench_rust_adapter)
  target_link_libraries(app PRIVATE ${RUST_ARTIFACT})
  target_compile_definitions(app PRIVATE BENCH_LANGUAGE_RUST)
else()
  set(BENCH_LANG_TAG "bench")
endif()

# Inject the git short SHA as the protocol build_id (must match the
# rtos zephyr plugin's compute_stm32f4_build_id: "zephyr-stm32f4-<tag>-<sha8>").
execute_process(
    COMMAND git -C ${CMAKE_CURRENT_SOURCE_DIR} rev-parse --short=8 HEAD
    OUTPUT_VARIABLE BENCH_GIT_SHA
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET)
if(NOT BENCH_GIT_SHA)
    set(BENCH_GIT_SHA "nogit")
endif()
target_compile_definitions(app PRIVATE
    BENCH_BUILD_ID="zephyr-stm32f4-${BENCH_LANG_TAG}-${BENCH_GIT_SHA}")

# Positive control for case 25 (max_interrupt_disabled): MAXDIS_CONTROL=1 in the
# environment masks interrupts for ~80 us before the TIM5 probe fires. A changed
# value needs a fresh configure (clean `rtos build`).
if(DEFINED ENV{MAXDIS_CONTROL} AND NOT "$ENV{MAXDIS_CONTROL}" STREQUAL "")
    target_compile_definitions(app PRIVATE MAXDIS_CONTROL=$ENV{MAXDIS_CONTROL})
endif()

# Positive control for case 20 (nested_irq_overhead): NESTED_CONTROL=1 disables
# the higher-priority line so the delta collapses to ~0. A changed value needs a
# fresh configure (clean `rtos build`).
if(DEFINED ENV{NESTED_CONTROL} AND NOT "$ENV{NESTED_CONTROL}" STREQUAL "")
    target_compile_definitions(app PRIVATE NESTED_CONTROL=$ENV{NESTED_CONTROL})
endif()
