# freertos-in-rust integration recipe

Status: firmware + adapter + CLI wiring implemented and hardware-verified
(full 29-case advertised suite at 1000 samples, 2026-10-08).

`freertos-in-rust` (crates.io `freertos-in-rust`, repo `apullin/freertos-in-rust`)
is a **pure-Rust, `no_std` translation of the FreeRTOS kernel** (baseline
`V10.4.3`). It links no FreeRTOS C code; only the Cortex-M context-switch
assembly is non-Rust. Unlike the repo's existing FreeRTOS `-rust` variant
(a Rust harness over the C kernel), this is a Rust kernel in its own right.

## Route

- **Board:** STM32F4 Discovery (USART2 PA2/PA3 @115200 via the external CP2102).
- **Language:** Rust (native Rust benchmark).
- **Toolchain:** stable Rust (`thumbv7em-none-eabihf`), no nightly.
- **Port:** `port-cortex-m4f` (Cortex-M4F with FPU — matches the F407).

## Configuration constraints (fixed in `src/config.rs`)

- `configMAX_PRIORITIES = 28` (priorities 0..27) — bumped from the upstream
  default of 5 so the benchmark (session task at 5, case-11 hog at 27) can run
  on this kernel.
- `configTICK_RATE_HZ = 1000` (1 ms tick).
- `configTOTAL_HEAP_SIZE = 32 KiB` (heap-4, raised from the upstream 6 KiB).
- `user-config` feature: the app supplies `FREERTOS_CONFIG_CPU_CLOCK_HZ` (168 MHz).

## Case subset

Advertised: all 29 cases, `0-28`.

- `0 timer_overhead` — DWT `CYCCNT` (168 MHz) read twice.
- `1 queue_roundtrip` — `Queue<u32>` capacity-1 `try_send`/`try_receive`.
- `2 task_handoff` — binary semaphore give/take wake latency.
- `3 periodic_jitter` — `xTaskDelayUntil` grid, tick-anchored (~0).
- `4 isr_to_task` — TIM2 one-shot ISR gives a semaphore; entry-to-task wake.
- `5 yield_roundtrip` — same-priority peer, `delay(0)` round trip.
- `6 preempt_latency` — TIM3 one-shot releases a high task that preempts a
  spinning low task.
- `7 ipc_pingpong` — request/reply `Queue` pair with an echo peer.
- `8 mutex_priority_inheritance` — L holds, M spins (gated), H blocks; ~13 us.
- `9 timer_callback_latency` — one-shot software timer dispatch.
- `10 trigger_to_isr` — EXTI0 software pend; a bare handler timestamps entry.
- `11 periodic_jitter_loaded` — TIM5 750 µs release grid plus a TIM7 137 µs
  busy-loop injector.
- `12 semaphore_latency` — `BinarySemaphore` give/take.
- `13 mutex_uncontended` — `Mutex<u32>` lock/unlock.
- `14 alloc_latency` — `pvPortMalloc(64)`/`vPortFree` on heap-4.
- `15 sleep_accuracy` — `vTaskDelay(1)` deviation, tick-aligned.
- `16 scheduler_overhead` — `delay(0)` scheduler pass.
- `17 notification_latency` — `EventGroup` set/wait wake latency.
- `18 ipc_payload_sweep` — 512-byte message buffers, payload {1,4,16,64,256}
  cycled via the protocol `variant` field.
- `19 ipc_throughput` — 1 MiB in 64-byte messages through a queue.
- `20 nested_irq_overhead` — EXTI1 (low) runs a fixed-iteration loop and pends
  EXTI2 (high) ten times; the value is nested minus the uninterrupted baseline
  (min of three each). `BENCH_NESTED_CONTROL=1` disables the higher pend and
  must report ~0. Measured 7889 ticks (46.9 µs) on the F4.
- `21 tick_overhead` — the kernel tick ISR body, bracketed with DWT reads.
- `22 idle_cpu_overhead` — tick-ISR cycles over a 500 ms idle window, in ppm.
- `23 cpu_overhead_under_load` — a graded busy worker at 25/50/75/100 %, in ppm.
- `24 heap_fragmentation` — 256-byte holes on heap-4, largest-block binary search.
- `25 max_interrupt_disabled` — a TIM4 50 µs one-shot masked by a critical section.
- `26 priority_scheduling_correctness` — three tasks at distinct priorities.
- `27 zero_copy_ipc` — copied 256-byte queue minus pointer queue.
- `28 common_arena_fragmentation` — runner-native 8 KiB arena.

`configTOTAL_HEAP_SIZE` is raised from the upstream 6 KiB to 32 KiB (a vendored
`config.rs` amendment): the benchmark's retained queues/buffers consume ~5.8 KiB
of the shared heap, leaving only ~350 bytes free at case 24, so the hole pattern
could not form. Case 24 now fills 256-byte blocks until a free-heap margin
remains (mirroring the C reference's margin) and reads 727086 ppm, comparable in
order to the C kernel's 674166 ppm. The metric is identical.

## Build

```sh
cd firmware/rust-kernels/freertos-in-rust/rust
BENCH_BUILD_ID=freertos-in-rust-stm32f4-bench-<sha8> cargo build --release
# ELF: target/thumbv7em-none-eabihf/release/rtos-bench-freertos-in-rust
```

## Flash

probe-rs, `STM32F407VGTx`, ST-Link `0483:3748:52C3BF6D`, ELF image, reset after
write (same as the other STM32F4 Rust adapters).
