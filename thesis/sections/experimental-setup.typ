// --- Standalone preview bootstrap ---------------------------------------------
// When this chapter is compiled on its own (CLI or editor preview), the block
// below applies the thesis template and bibliography so the chapter renders in
// context. When thesis.typ includes this file it first sets the
// "_chapter_embedded" state, which disables the bootstrap because the thesis
// already supplies the template and bibliography. This is preview scaffolding,
// not part of the assembled thesis layout.
#import "@preview/uastw-thesis:0.1.0" as uastw
#show: it => context {
 if state("_chapter_embedded", false).get() { it }
 else { uastw.uastw-thesis-page-setup(it) }
}
#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

= Experimental Setup

This chapter describes the hardware and software environment used for the experiments. It introduces the four development boards and their timing facilities, defines the evaluated RTOS configurations, and describes the common benchmarking framework. It also covers the build environment and the serial communication protocol; the measurement and comparison procedures are described in the following chapter.

== Hardware Platforms

The evaluation uses four development boards with Arm Cortex-M and RISC-V processors. Their main specifications are summarised in @hardware-boards. All timing measurements are performed on the devices themselves. The host is responsible for building, flashing and collecting the results.

#figure(
  {
    set text(size: 9pt)
    table(
      columns: (1.3fr, 1.2fr, 0.8fr, 0.75fr, 0.75fr, 0.65fr),
      inset: (x: 4pt, y: 3pt),
      align: (left, left, left, right, right, right),
      table.header(
        [*Board*],
        [*Microcontroller*],
        [*Core*],
        [*Max clock*],
        [*Flash*],
        [*SRAM*],
      ),
      [STM32F4 Discovery],
      [STM32F407VGT6],
      [Cortex-M4F],
      [168 MHz],
      [1 MiB],
      [192 KiB],

      [FK723M1 core board],
      [STM32H723ZGT6],
      [Cortex-M7],
      [550 MHz],
      [1 MiB],
      [564 KiB],

      [ESP32-C6 DevKitC],
      [ESP32-C6],
      [RISC-V],
      [160 MHz],
      [8 MiB (ext.)],
      [512 KiB],

      [nRF52840 DK],
      [nRF52840],
      [Cortex-M4F],
      [64 MHz],
      [1 MiB],
      [256 KiB],
    )
  },
  caption: [Hardware platforms used in the evaluation. Clock frequencies are rated maxima.],
) <hardware-boards>

The *STM32F4 Discovery* uses its onboard ST-LINK/V2 interface for programming and debugging. Benchmark communication takes place through USART2 on PA2 and PA3 using an external CP2102 USB-to-UART bridge. FreeRTOS and NuttX use the Data Watchpoint and Trace (DWT) cycle counter for timing measurements, while Zephyr uses the Cortex-M SysTick counter. Hardware timers support interrupt-related experiments @stm32f4discovery.

The *FK723M1 core board* operates at 520 MHz in the evaluated configuration, below its rated maximum of 550 MHz @fk723m1. Programming and debugging require an external ST-LINK/V2 probe. The benchmark console uses USART1 on PA9 and PA10 through a CP2102 bridge at 115200 baud. FreeRTOS and NuttX use the DWT cycle counter for timing measurements, while Zephyr uses the Cortex-M SysTick counter. The Zephyr configuration uses the upstream `fk723m1_zgt6` board target with a 25 MHz crystal and a dedicated clock overlay.

The *ESP32-C6 DevKitC* uses its integrated USB Serial/JTAG peripheral for programming, debugging and benchmark communication @esp32c6devkitc. Timing measurements use the counter facilities provided by the corresponding RTOS port.

The *nRF52840 DK* provides SWD debugging and a virtual serial port through its onboard J-Link interface @nrf52840dk. The benchmark console uses UART0 on P0.06 and P0.08. Timing measurements use the DWT cycle counter, which must be re-enabled when necessary after low-power transitions.

== Timing Sources

The measurement-counter frequency is not always the same as the processor clock frequency. On the Arm boards, FreeRTOS and NuttX use the DWT cycle counter, while Zephyr uses the Cortex-M SysTick counter on the STM32 boards and DWT on the nRF52840. The ESP32-C6 uses different timing sources depending on the RTOS. @timing-sources summarises the counter frequencies used to convert recorded ticks into time units.

#figure(
  {
    set text(size: 9pt)
    table(
      columns: (1.2fr, 1.5fr, 1.9fr, 0.9fr),
      inset: (x: 4pt, y: 3pt),
      align: (left, left, left, right),
      table.header([*Platform*], [*RTOS*], [*Timing source*], [*Frequency*]),
      [STM32F4], [FreeRTOS, NuttX], [DWT CYCCNT], [168 MHz],
      [STM32F4], [Zephyr], [SysTick-based 64-bit software counter], [168 MHz],
      [STM32H723], [FreeRTOS, NuttX], [DWT CYCCNT], [520 MHz],
      [STM32H723], [Zephyr], [SysTick-based 64-bit software counter], [520 MHz],
      [nRF52840], [FreeRTOS, Zephyr, NuttX], [DWT CYCCNT], [64 MHz],
      [ESP32-C6], [FreeRTOS], [SYSTIMER], [16 MHz],
      [ESP32-C6], [Zephyr], [SYSTIMER], [16 MHz],
      [ESP32-C6], [NuttX], [RISC-V performance counter], [160 MHz],
    )
  },
  caption: [Timing sources and counter frequencies used in the matched configurations.],
) <timing-sources>

The ports provide the timing sources and handle hardware-specific requirements. Counter rollover occurs when a finite-width counter reaches its limit and wraps; elapsed-time calculations must account for this wrap. Counters may also need to be re-enabled after low-power transitions.

== RTOS Configurations

The evaluation covers five RTOS kernels across 27 configurations. FreeRTOS, Zephyr and NuttX are each evaluated with C and Rust applications on all four hardware platforms, forming 12 matched C/Rust pairs. The two Rust-native kernels are evaluated separately: FreeRTOS-in-Rust on the STM32F4 and STM32H723, and Rivet on the STM32F4. These three additional configurations are used for kernel-level comparisons rather than direct application-language comparisons.

Ten additional systems were integrated during development but excluded from the final evaluation: Tock, Hubris, Embassy, RTIC, Ariel OS, RIOT OS, RT-Thread, ChibiOS/RT, Eclipse ThreadX and RTEMS. Their differing execution models, unsupported benchmark operations or incomplete C/Rust implementations prevented sufficiently comparable measurements.

#block(breakable: false)[
  #heading(level: 2)[Benchmarking Framework]

  To compare C and Rust across different hardware platforms and RTOS kernels, a common benchmarking framework was developed. It separates experiment management on the host from workload execution on the target device. This allows the same benchmark definitions to be used across configurations while excluding host-side communication from timing measurements.
]

The framework consists of a command-line interface, a core library, system-specific adapters, and benchmark runners written in C and Rust. The host manages experiment configurations, builds and flashes firmware, starts measurement runs, and validates the results. The adapters handle platform-specific build and flashing procedures, while the device-side runners execute the workloads and record timing values using hardware counters.

On the device, the workloads are implemented in shared C and Rust runners. Both use a port abstraction to access kernel operations and timing facilities. The C implementation uses a table of callbacks, while Rust uses a trait. This separates the benchmark logic from the underlying RTOS and board-specific code.

Timing is performed either directly in the runner or within a port-specific measurement routine, depending on the operation.

== Build Environment

To support reproducibility and traceability, each build uses an isolated directory and is identified by its source revisions, target architecture, programming language, configuration, benchmark version, and compiler. Cached firmware is reused only when these properties match. Raw device output, validated measurements, and experiment metadata are retained so that results can be traced back to the configurations under which they were obtained.

The matched C and Rust configurations use nominal optimisation level 2: `-O2` for C and `opt-level=2` for Rust. These settings do not imply identical optimisation passes or generated code. The C benchmark code is compiled with `-O2`, while Rust uses release-profile optimisation level 2. The Rust code is compiled as a static library and linked into the firmware using the corresponding C toolchain. The underlying kernels retain their own build configurations, so the application and kernel code do not necessarily use identical optimisation settings. The relevant compiler versions and build settings are summarised in @build-settings.

#figure(
  {
    set text(size: 9pt)
    table(
      columns: (1.3fr, 1.5fr, 1.0fr),
      inset: (x: 4pt, y: 3pt),
      align: (left, left, right),
      table.header([*Component*], [*Compiler / setting*], [*Value*]),
      [C, Arm boards], [GCC], [14.2.1],
      [C, ESP32-C6], [GCC], [14.2.0],
      [Rust, all boards], [rustc], [1.98.1],
      [C benchmark applications], [Optimisation], [`-O2`],
      [Rust applications], [Optimisation], [`opt-level=2`],
      [Rust applications], [Panic strategy], [`panic=abort`],
      [Rust applications], [Code generation units], [`codegen-units=1`],
      [Rust applications], [Rust LTO], [Enabled],
      [NuttX and Zephyr kernels], [Optimisation], [`-Os`],
    )
  },
  caption: [Compiler versions and relevant optimisation settings for the matched C/Rust configurations. Rust is linked as a static library; its LTO setting does not imply cross-language LTO.],
) <build-settings>

== Communication Protocol

The host and target device exchange commands and measurement data through a line-oriented serial protocol using JSON messages. Each message includes a protocol version and type, allowing benchmark data to be distinguished from unrelated console output.
Communication follows a defined sequence, beginning with a handshake to establish the device identity and available benchmarks. The host sends a `hello` message containing a random challenge, which the device answers with a `ready` response. Once the handshake is complete, a `start` request specifies the workloads and sample counts. The device then reports individual measurements and signals the end of the run with a `complete` message.

The device validates incoming requests, while the host verifies device identity, sample order, sample counts, and successful completion.

Samples are buffered and transmitted after the timed operations.

#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}
