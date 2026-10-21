# Rivet RTOS integration recipe

Status: firmware + adapter + CLI wiring implemented. **27 of 29 cases** build,
flash and are hardware-verified on the STM32F4 Discovery (2026-10-07/08); the
canonical 1000-rep advertised suite passes. Only `14` and `24` are withheld
(they require a kernel allocator). Advertised set:

`0,1,2,3,4,5,6,7,8,9,10,11,12,13,15,16,17,18,19,20,21,22,23,25,26,27,28`.

Rivet (`rivet-rtos` 0.2.0, repo `habeelali/rivet-rtos`) is a **zero-allocation,
dual-tier Rust RTOS**: a preemptive tier (priorities 1..31, per-task stacks,
`PriorityMutex` with inheritance) above a cooperative tier (`async fn` tasks on
one shared stack, with `sync::Semaphore`/`Channel`/`Signal` and an async
`Sleep` future). No allocator; resource pools are compile-time sized.
`rivet::init()` spawns the cooperative executor as the lowest-priority
preemptive task, so the benchmark session (preemptive, priority 5) runs above
it and sleeps `.await`-driven cases down to it.

## Route

- **Board:** STM32F4 Discovery (USART2 PA2/PA3 @115200 via the external CP2102).
- **Language:** Rust only.
- **Toolchain:** stable Rust (`thumbv7em-none-eabi`), no nightly.
- **Clock:** 168 MHz (HSE 8 MHz PLL, M=8/N=336/P=2). DWT `CYCCNT` is the cycle
  source (32-bit; firmware only takes deltas).

## Layout

- `.local/source/rivet-rtos/` — pinned Rivet workspace (path dependency).
- `firmware/rust-kernels/rivet/rust/stm32f4/src/main.rs` — F407 session, BSP-independent
  cases, protocol.
- `firmware/rust-kernels/rivet/rust/stm32f4/src/bsp.rs` — folded F407 BSP + tick
  instrumentation.
- `firmware/rust-kernels/rivet/rust/stm32f4/src/coop.rs` — cooperative-tier (async) cases.
- `firmware/rust-kernels/rivet/rust/stm32f4/src/arena.rs` — shared case-28 arena.

## Case mapping

Preemptive tier (the session task itself):

- `0` timer_overhead — DWT read twice.
- `1` queue_roundtrip — `sync::Channel<u32,2>` (usable capacity 1)
  `try_send`/`try_recv` in one task.
- `3` periodic_jitter — `sleep_until` tick-anchored grid.
- `5` yield_roundtrip — a same-priority peer with only `yield_now()`.
- `6` preempt_latency — blocks on a `PriorityMutex` held by a low-priority
  holder that releases as soon as allowed.
- `8` mutex_priority_inheritance — L holds a `PriorityMutex`, H blocks, a
  medium-priority spinner runs ~2 ms; ≪ the spin, so inheritance is active.
- `10` trigger_to_isr — NVIC-pended EXTI0 through `rivet::irq`.
- `12` semaphore_latency — `sync::Semaphore<1>` `release` then `try_acquire`.
- `13` mutex_uncontended — `PriorityMutex` lock/unlock.
- `15` sleep_accuracy — `sleep_ms(1)` absolute deviation.
- `16` scheduler_overhead — `yield_now`.
- `20` nested_irq_overhead — EXTI1 (low) runs a fixed-iteration loop and pends
  the higher-priority EXTI2 ten times; the value is nested minus the
  uninterrupted baseline (min of three each). `BENCH_NESTED_CONTROL=1` control
  must read ~0. Measured 2822 ticks (16.8 µs) on the F4.
- `21` tick_overhead — the BSP `SysTick` body bracketed with DWT reads.
- `22` idle_cpu_overhead — accumulated tick-ISR cycles over a 500 ms idle
  window, as `1e6 * accum / window`.
- `23` cpu_overhead_under_load — a priority-1 worker spins `pct`% of an
  absolute 10 ms grid and accumulates its own busy cycles; load level carried as
  `variant`.
- `25` max_interrupt_disabled — a TIM4 one-shot 50 us probe; `RIVET_MAXDIS_CONTROL=1`
  masks for ~80 us and raises the delay.
- `26` priority_scheduling_correctness — four distinct-priority tasks append
  their priority then park (`RIVET_PRIO_INVERT=1` inversion control).
- `28` common_arena_fragmentation — runner-native 8 KiB arena.

Cooperative tier (`coop.rs`; the session triggers a non-blocking primitive and
sleeps so the executor runs the async peer). **Deviation:** the measured switch
is the executor's wake/poll path, not a PendSV context switch:

- `2` task_handoff — `Semaphore` release → blocked async peer's unblock.
- `4` isr_to_task — TIM2 one-shot ISR → `Signal` → async target resume.
- `7` ipc_pingpong — requester + echo tasks over two capacity-one channels.
- `9` timer_callback_latency — async `Sleep<10_000>` one-shot deviation.
- `11` periodic_jitter_loaded — TIM5 750 us release grid → async target;
  TIM7 137 us high-priority injector (`RIVET_PJIT_LOAD_OFF=1` control).
- `17` notification_latency — session `Signal` → blocked async target resume.
- `18` ipc_payload_sweep — 256-byte message channel, `variant` = size.
- `19` ipc_throughput — 1 MiB of 64-byte messages through a depth-8 channel.
- `27` zero_copy_ipc — copied (256 B) minus pointer-only round trip.

Withheld:

- `14` alloc_latency, `24` heap_fragmentation — Rivet is zero-allocation by
  design; there is no kernel heap to time or fragment.

## Implementation notes

- The 16 KiB kernel task-stack pool packs stacks with power-of-two alignment
  plus a 64-byte guard, so a single 8192-byte stack consumes the whole pool.
  The session therefore uses a dedicated `#[repr(align(8192))]` static (the
  same pattern Rivet uses for its async-idle task); auxiliary preemptive tasks
  use 512-byte pool stacks.
- Preemptive auxiliary tasks (case 5 peer, 6/8 holder/spinner) are spawned
  suspended and resumed only while their case runs. The case-23 worker is
  resident and idles on a short sleep when its level is 0.
- `ISR_ENTRY`/`NESTED_ENTRY` (cases 10/20) are cleared and spin-waited per
  sample so a stale entry from a prior rep cannot leak in, and are compared in
  u32.
- Build flags: `RIVET_PRIO_INVERT` (case 26 inversion control),
  `RIVET_MAXDIS_CONTROL` (case 25 positive control), `RIVET_PJIT_LOAD_OFF`
  (case 11 load-off control), `BENCH_NESTED_CONTROL` (case 20 positive
  control). `build.rs` emits the corresponding `rustc-check-cfg` entries.

## Build

```sh
cd firmware/rust-kernels/rivet/rust/stm32f4
BENCH_BUILD_ID=rivet-stm32f4-bench-<sha8> cargo build --release
# ELF: target/thumbv7em-none-eabi/release/rtos-bench-rivet
```

The linker script is passed via `cargo:rustc-link-arg=-Tlink-stm32f407.ld`
(the `cargo:linker-script` spelling used by the upstream BSPs is not a
recognised build-script key and silently drops the script).

## Flash

probe-rs, `STM32F407VGTx`, ST-Link `0483:3748:52C3BF6D`, ELF image, reset after
write.
