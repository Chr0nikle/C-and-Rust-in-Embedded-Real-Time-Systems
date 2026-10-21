#
# NuttX CMake registration for the RTOS benchmark session application.
#
# This file is added to the NuttX build graph by the NuttX host plugin, which
# wires it into the apps tree (see plugins/nuttx/BUILD.md). It registers the
# benchmark as a built-in NuttX application following the examples/hello
# convention. The first SRCS entry contains main() and receives the
# -Dmain=rtos_bench_main alias during the build.
#
# The shared runner and header live in firmware/common/c (sibling of c-kernels).
#
# The nuttx_add_application function is not yet available when this file is
# first processed (it is included after add_subdirectory(nuttx-apps) in the
# top-level CMakeLists.txt). The if(CONFIG_EXAMPLES_RTOS_BENCH) guard ensures
# the call is only made on the second configure pass, after the defconfig has
# been expanded and the nuttx build infrastructure exists.
#
# With -DBENCH_RUST=ON (the plugin sets it for --language rust) the shared C
# runner is dropped and a Rust application core is linked instead; the C
# session keeps the UART/JSON protocol and calls rust_bench_run_case.
#

if(CONFIG_EXAMPLES_RTOS_BENCH)
  if(NOT DEFINED BENCH_BUILD_ID)
    if(CONFIG_ARCH_CHIP_ESP32C6)
      set(BENCH_BUILD_ID "nuttx-c6-bench-nogit")
    elseif(CONFIG_ARCH_CHIP_NRF52)
      set(BENCH_BUILD_ID "nuttx-nrf52840dk-bench-nogit")
    elseif(CONFIG_ARCH_CHIP_RP23XX)
      set(BENCH_BUILD_ID "nuttx-pico2-bench-nogit")
    else()
      set(BENCH_BUILD_ID "nuttx-stm32f4-bench-nogit")
    endif()
  endif()

  set(BENCH_SRCS
    main.c
    port.c
    isr.c
  )
  set(BENCH_DEFS BENCH_BUILD_ID="${BENCH_BUILD_ID}" BENCH_NUTTX_APP)

  if(BENCH_RUST)
    # Both images are soft-float (STM32F4 CONFIG_ARCH_FPU unset; the C6 has no
    # FPU), so the soft-float targets match the C ABI.
    if(CONFIG_ARCH_CHIP_ESP32C6)
      set(RUST_TARGET riscv32imac-unknown-none-elf)
    elseif(CONFIG_ARCH_CHIP_RP23XX)
      set(RUST_TARGET thumbv8m.main-none-eabihf)
    elseif(CONFIG_ARCH_CHIP_STM32H7)
      # The H7 build has the FPU (VFP) enabled, so the staticlib must match the
      # hard-float ABI.
      set(RUST_TARGET thumbv7em-none-eabihf)
    else()
      set(RUST_TARGET thumbv7em-none-eabi)
    endif()
    set(RUST_CRATE_DIR ${CMAKE_CURRENT_LIST_DIR}/../rust)
    set(RUST_ARTIFACT
        ${CMAKE_CURRENT_LIST_DIR}/../../../target/${RUST_TARGET}/release/librtos_firmware_nuttx.a)
    add_custom_command(
      OUTPUT ${RUST_ARTIFACT}
      COMMAND cargo build --release --target ${RUST_TARGET}
      WORKING_DIRECTORY ${RUST_CRATE_DIR}
      DEPENDS ${RUST_CRATE_DIR}/src/lib.rs
              ${RUST_CRATE_DIR}/src/boundary.rs
              ${RUST_CRATE_DIR}/Cargo.toml
              ${CMAKE_CURRENT_LIST_DIR}/../../../common/rust/src/lib.rs
      COMMENT "Building Rust NuttX adapter staticlib"
      VERBATIM)
    add_custom_target(bench_rust_adapter DEPENDS ${RUST_ARTIFACT})
    list(APPEND BENCH_DEFS BENCH_LANGUAGE_RUST)
    # Append to NUTTX_EXTRA_LIBRARIES (not target_link_libraries): the top-level
    # link reads this property after the app libraries, so the archive is scanned
    # after the objects that reference rust_bench_run_case. Linking it directly
    # placed it before libapps_rtos_bench.a and the symbol was discarded.
    set_property(GLOBAL APPEND PROPERTY NUTTX_EXTRA_LIBRARIES ${RUST_ARTIFACT})
    if(TARGET nuttx)
      add_dependencies(nuttx bench_rust_adapter)
    endif()
  else()
    list(APPEND BENCH_SRCS ${CMAKE_CURRENT_LIST_DIR}/../../../common/c/src/runner.c)
  endif()

  nuttx_add_application(
    NAME
    rtos_bench
    SRCS
    ${BENCH_SRCS}
    INCLUDE_DIRECTORIES
    ${CMAKE_CURRENT_LIST_DIR}/../../../common/c/include
    DEFINITIONS
    ${BENCH_DEFS}
    STACKSIZE
    8192
    PRIORITY
    100)

  target_compile_options(apps_rtos_bench PRIVATE "-O2")
endif()
