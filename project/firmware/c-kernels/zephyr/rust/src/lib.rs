//! Zephyr Rust adapter for the STM32F4 Discovery benchmark.
//!
//! The Rust application core supplies the benchmark sequencing and sink bridge;
//! the native Zephyr primitives (k_msgq queue, k_sem handoff, absolute-tick
//! sleep, k_timer ISR) are reached through thin C shims in the Zephyr C port
//! (`firmware/zephyr/c/stm32f4/port.c`). Zephyr's `k_*` surface is macro/inline
//! and takes `k_timeout_t` structs by value, so a direct Rust FFI call cannot be
//! ABI-stable; the C path reaches the same `z_impl_*` through its port vtable,
//! so the shim call is the closest equivalent for both languages.
//!
//! Build with `cargo build --release --target thumbv7em-none-eabihf`; the
//! staticlib is linked into the Zephyr app when the CMake option
//! `BENCH_RUST=ON` is set (the `rtos zephyr` plugin does this for
//! `--language rust`).
//!
//! The C session keeps the UART/JSON protocol layer and calls
//! `rust_bench_run_case` per case, providing `bench_rust_*` shims and the
//! record/flush bridges.
#![no_std]

use core::ffi::c_void;
use rtos_bench::{run_case, Case, Error, Port, Settings, Sink};

mod boundary;

/// Case-19 transfer volume (1 MiB / 64 B), matching the C port's `TP_MSGS`.
const TP_MSGS: u32 = 16384;
/// Case-26 released-peer count, matching the C port's `ZPRIO_N`.
const PRIO_N: u32 = 4;

extern "C" {
    fn bench_rust_now_ticks() -> u64;
    fn bench_rust_elapsed_ticks(start: u64, end: u64) -> u64;
    fn bench_rust_ticks_per_second() -> u64;
    fn bench_rust_jitter_period() -> u64;

    // Complete C measurement paths used by the C runner.  Calling these from
    // Rust keeps the timestamp boundaries identical for both languages.
    fn bench_isr_measure(ticks: *mut u64) -> i32;
    fn bench_pjit_measure(ticks: *mut u64) -> i32;
    fn bench_maxdis_measure(ticks: *mut u64) -> i32;
    fn bench_rust_sleep_measure(ticks: *mut u64) -> i32;

    fn bench_rust_isr_create() -> i32;
    fn bench_rust_isr_arm() -> i32;
    fn bench_rust_isr_wait() -> i32;
    fn bench_rust_isr_entry() -> u64;
    fn bench_rust_isr_destroy() -> i32;
    fn bench_rust_trigger_isr_create() -> i32;
    fn bench_rust_trig_drain() -> i32;
    fn bench_rust_trig_fire() -> u64;
    fn bench_rust_trig_wait() -> i32;
    fn bench_rust_trig_entry() -> u64;
    fn bench_rust_trigger_isr_destroy() -> i32;
    fn bench_rust_pjit_create() -> i32;
    fn bench_rust_pjit_wait() -> i32;
    fn bench_rust_pjit_read(ticks: *mut u64) -> i32;
    fn bench_rust_pjit_destroy() -> i32;
    fn bench_rust_maxdis_create() -> i32;
    fn bench_rust_maxdis_begin() -> u64;
    fn bench_rust_maxdis_arm() -> i32;
    fn bench_rust_maxdis_wait(t0: u64) -> i32;
    fn bench_rust_maxdis_isr_ts() -> u64;
    fn bench_rust_maxdis_destroy() -> i32;
    fn bench_rust_nested_create() -> i32;
    fn bench_rust_nested_run(pend_h: u32) -> u32;
    fn bench_rust_nested_destroy() -> i32;
    fn bench_rust_frag_create() -> i32;
    fn bench_rust_frag_baseline() -> i64;
    fn bench_rust_frag_free_bytes() -> i64;
    fn bench_rust_frag_bytes() -> u32;
    fn bench_rust_frag_n() -> u32;
    fn bench_rust_frag_alloc(idx: u32) -> i32;
    fn bench_rust_frag_free(idx: u32);
    fn bench_rust_frag_try(bucket: u32) -> i32;
    fn bench_rust_frag_destroy() -> i32;
}

fn status(code: i32) -> Result<(), Error> {
    if code == 0 {
        Ok(())
    } else {
        Err(Error::Port)
    }
}

/// As `status`, but preserves the runner's `BENCH_CORRUPT` for rejected
/// samples (no switch, no preemption) versus a port failure.
fn status_case(code: i32) -> Result<(), Error> {
    match code {
        0 => Ok(()),
        4 => Err(Error::Corrupt),
        5 => Err(Error::DeadlineMiss),
        _ => Err(Error::Port),
    }
}

/// Native Zephyr port: `k_cycle_get_64` timer plus Zephyr kernel primitives.
pub struct ZephyrPort {
    jitter_next: i64,
    jitter_prev: u64,
    sem: boundary::KSem,
}

impl ZephyrPort {
    /// An empty port; per-case resources are created by the runner.
    pub fn new() -> Self {
        Self {
            jitter_next: 0,
            jitter_prev: 0,
            sem: boundary::KSem::zeroed(),
        }
    }
}

impl Default for ZephyrPort {
    fn default() -> Self {
        Self::new()
    }
}

impl Port for ZephyrPort {
    fn now_ticks(&mut self) -> Result<u64, Error> {
        let ticks = unsafe { bench_rust_now_ticks() };
        if ticks == 0 {
            Err(Error::Port)
        } else {
            Ok(ticks)
        }
    }

    fn ticks_per_second(&self) -> u64 {
        unsafe { bench_rust_ticks_per_second() }
    }

    fn queue_create(&mut self) -> Result<(), Error> {
        boundary::queue_create().map_err(|_| Error::Port)
    }

    fn queue_send(&mut self, value: u32) -> Result<(), Error> {
        boundary::queue_send(value).map_err(|_| Error::Port)
    }

    fn queue_receive(&mut self) -> Result<u32, Error> {
        boundary::queue_receive().map_err(|_| Error::Port)
    }

    fn queue_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn handoff_create(&mut self) -> Result<(), Error> {
        boundary::handoff_create().map_err(|_| Error::Port)
    }

    fn handoff_measure(&mut self) -> Result<u64, Error> {
        boundary::handoff_measure().map_err(|_| Error::Port)
    }

    fn handoff_destroy(&mut self) -> Result<(), Error> {
        boundary::handoff_destroy();
        Ok(())
    }

    fn jitter_create(&mut self) -> Result<(), Error> {
        self.jitter_next = boundary::uptime_ticks() + 1;
        boundary::sleep_abs_ticks(self.jitter_next);
        self.jitter_prev = self.now_ticks()?;
        Ok(())
    }

    fn jitter_measure(&mut self) -> Result<u64, Error> {
        self.jitter_next += 1;
        boundary::sleep_abs_ticks(self.jitter_next);
        let now = self.now_ticks()?;
        let period = unsafe { bench_rust_jitter_period() };
        let interval = now.saturating_sub(self.jitter_prev);
        self.jitter_prev = now;
        Ok(interval.abs_diff(period))
    }

    fn jitter_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn isr_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_isr_create() })
    }

    fn isr_measure(&mut self) -> Result<u64, Error> {
        // Use the C measurement path so the wake timestamp is captured
        // immediately after the semaphore take, exactly as in the C runner.
        let mut ticks = 0u64;
        status_case(unsafe { bench_isr_measure(&mut ticks) })?;
        Ok(ticks)
    }

    fn isr_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_isr_destroy() })
    }

    fn trigger_isr_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_trigger_isr_create() })
    }

    fn trigger_isr_measure(&mut self) -> Result<u64, Error> {
        let _ = unsafe { bench_rust_trig_drain() };
        let t0 = unsafe { bench_rust_trig_fire() };
        if unsafe { bench_rust_trig_wait() } != 0 {
            return Err(Error::Port);
        }
        let entry = unsafe { bench_rust_trig_entry() };
        if entry < t0 {
            return Err(Error::Corrupt);
        }
        Ok(entry - t0)
    }

    fn trigger_isr_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_trigger_isr_destroy() })
    }

    fn yield_create(&mut self) -> Result<(), Error> {
        boundary::yield_create().map_err(|_| Error::Port)
    }

    fn yield_now(&mut self) -> Result<(), Error> {
        boundary::yield_now().map_err(|_| Error::Corrupt)
    }

    fn yield_destroy(&mut self) -> Result<(), Error> {
        boundary::yield_destroy();
        Ok(())
    }

    fn peers_set(&mut self, count: u32) -> Result<(), Error> {
        boundary::peers_set(count).map_err(|_| Error::Port)
    }

    fn preempt_create(&mut self) -> Result<(), Error> {
        boundary::preempt_create().map_err(|_| Error::Port)
    }

    fn preempt_measure(&mut self) -> Result<u64, Error> {
        if boundary::preempt_ready() == 0 {
            return Err(Error::Port);
        }
        let seq = boundary::preempt_seq();
        let start = self.now_ticks()?;
        boundary::preempt_post().map_err(|_| Error::Port)?;
        if boundary::preempt_seq() == seq {
            return Err(Error::Corrupt);
        }
        let wake = boundary::preempt_wake();
        Ok(wake.saturating_sub(start))
    }

    fn preempt_destroy(&mut self) -> Result<(), Error> {
        boundary::preempt_destroy();
        Ok(())
    }

    fn ipc_create(&mut self) -> Result<(), Error> {
        boundary::ipc_create().map_err(|_| Error::Port)
    }

    fn ipc_measure(&mut self, value: u32) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        boundary::ipc_roundtrip(value).map_err(|_| Error::Port)?;
        let end = self.now_ticks()?;
        Ok(end.saturating_sub(start))
    }

    fn ipc_destroy(&mut self) -> Result<(), Error> {
        boundary::ipc_destroy();
        Ok(())
    }

    fn ipc_payload_create(&mut self) -> Result<(), Error> {
        boundary::ipc_payload_create().map_err(|_| Error::Port)
    }

    fn ipc_payload_measure(&mut self, payload_bytes: u32) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        boundary::ipc_payload_roundtrip(payload_bytes).map_err(|_| Error::Port)?;
        let end = self.now_ticks()?;
        boundary::ipc_payload_verify(payload_bytes).map_err(|_| Error::Corrupt)?;
        Ok(end.saturating_sub(start))
    }

    fn ipc_payload_destroy(&mut self) -> Result<(), Error> {
        boundary::ipc_payload_destroy();
        Ok(())
    }

    fn tp_create(&mut self) -> Result<(), Error> {
        boundary::tp_create().map_err(|_| Error::Port)
    }

    fn tp_measure(&mut self) -> Result<u64, Error> {
        boundary::tp_start(TP_MSGS).map_err(|_| Error::Port)?;
        let start = self.now_ticks()?;
        for _ in 0..TP_MSGS {
            boundary::tp_send().map_err(|_| Error::Port)?;
        }
        boundary::tp_done().map_err(|_| Error::Port)?;
        let end = self.now_ticks()?;
        // The C port owns the counter-width-specific subtraction (32-bit DWT
        // modular arithmetic on nRF52840, 64-bit monotonic ticks elsewhere).
        Ok(unsafe { bench_rust_elapsed_ticks(start, end) })
    }

    fn tp_destroy(&mut self) -> Result<(), Error> {
        boundary::tp_destroy();
        Ok(())
    }

    fn zcopy_create(&mut self) -> Result<(), Error> {
        boundary::zcopy_create().map_err(|_| Error::Port)
    }

    fn zcopy_measure(&mut self) -> Result<u64, Error> {
        boundary::zcopy_reset().map_err(|_| Error::Port)?;
        let copied = boundary::zcopy_copied().map_err(|_| Error::Corrupt)?;
        let zcopy = boundary::zcopy_ref().map_err(|_| Error::Corrupt)?;
        boundary::zcopy_verify().map_err(|_| Error::Corrupt)?;
        Ok(if copied > zcopy {
            copied - zcopy
        } else {
            u64::MAX
        })
    }

    fn zcopy_destroy(&mut self) -> Result<(), Error> {
        boundary::zcopy_destroy();
        Ok(())
    }

    fn mutex_create(&mut self) -> Result<(), Error> {
        boundary::mutex_pi_create().map_err(|_| Error::Port)
    }

    fn mutex_measure(&mut self) -> Result<u64, Error> {
        if boundary::mutex_pi_ready() == 0 {
            return Err(Error::Port);
        }
        boundary::mutex_pi_reset();
        boundary::mutex_pi_req().map_err(|_| Error::Port)?;
        boundary::mutex_pi_held().map_err(|_| Error::Port)?;
        boundary::mutex_pi_go();
        let t0 = self.now_ticks()?;
        boundary::mutex_pi_rel();
        boundary::mutex_pi_lock().map_err(|_| Error::Port)?;
        let t1 = self.now_ticks()?;
        boundary::mutex_pi_set_stop();
        boundary::mutex_pi_done().map_err(|_| Error::Port)?;
        boundary::mutex_pi_unlock().map_err(|_| Error::Port)?;
        Ok(t1.saturating_sub(t0))
    }

    fn mutex_destroy(&mut self) -> Result<(), Error> {
        boundary::mutex_pi_destroy();
        Ok(())
    }

    fn timer_create(&mut self) -> Result<(), Error> {
        boundary::timer_create().map_err(|_| Error::Port)
    }

    fn timer_measure(&mut self) -> Result<u64, Error> {
        boundary::timer_drain().map_err(|_| Error::Port)?;
        let t0 = self.now_ticks()?;
        boundary::timer_arm().map_err(|_| Error::Port)?;
        boundary::timer_wait().map_err(|_| Error::Port)?;
        let t1 = boundary::timer_fire();
        let period = self.ticks_per_second() / 100;
        let interval = t1.saturating_sub(t0);
        Ok(interval.abs_diff(period))
    }

    fn timer_destroy(&mut self) -> Result<(), Error> {
        boundary::timer_destroy();
        Ok(())
    }

    fn pjit_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_pjit_create() })
    }

    fn pjit_measure(&mut self) -> Result<u64, Error> {
        // The C path captures the wake timestamp immediately after the release
        // wait and performs the release-grid arithmetic on that timestamp.
        let mut ticks = 0u64;
        status_case(unsafe { bench_pjit_measure(&mut ticks) })?;
        Ok(ticks)
    }

    fn pjit_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_pjit_destroy() })
    }

    fn sem_create(&mut self) -> Result<(), Error> {
        boundary::sem_create(&mut self.sem).map_err(|_| Error::Port)
    }

    fn sem_measure(&mut self) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        boundary::sem_give(&mut self.sem);
        boundary::sem_take(&mut self.sem).map_err(|_| Error::Corrupt)?;
        let end = self.now_ticks()?;
        if end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn sem_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn mutex_u_create(&mut self) -> Result<(), Error> {
        boundary::mutex_u_create().map_err(|_| Error::Port)
    }

    fn mutex_u_measure(&mut self) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        boundary::mutex_u_lock().map_err(|_| Error::Port)?;
        boundary::mutex_u_unlock().map_err(|_| Error::Port)?;
        let end = self.now_ticks()?;
        if end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn mutex_u_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn alloc_create(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn alloc_measure(&mut self) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        let p = boundary::kalloc(64);
        if !p.is_null() {
            boundary::kfree(p);
        }
        let end = self.now_ticks()?;
        if p.is_null() || end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn alloc_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn sleep_create(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn sleep_measure(&mut self) -> Result<u64, Error> {
        // Use the C measurement path so the timestamp placement and the
        // k_msleep(1) call are identical to the C runner.
        let mut ticks = 0u64;
        status_case(unsafe { bench_rust_sleep_measure(&mut ticks) })?;
        Ok(ticks)
    }

    fn sleep_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn sched_create(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn sched_measure(&mut self) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        boundary::sched_yield();
        let end = self.now_ticks()?;
        if end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn sched_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn notify_create(&mut self) -> Result<(), Error> {
        boundary::notify_create().map_err(|_| Error::Port)
    }

    fn notify_measure(&mut self) -> Result<u64, Error> {
        boundary::notify_drain().map_err(|_| Error::Port)?;
        boundary::notify_go().map_err(|_| Error::Port)?;
        boundary::notify_done().map_err(|_| Error::Port)?;
        let end = self.now_ticks()?;
        let signal = boundary::notify_signal();
        if end < signal {
            return Err(Error::Corrupt);
        }
        Ok(end - signal)
    }

    fn notify_destroy(&mut self) -> Result<(), Error> {
        boundary::notify_destroy();
        Ok(())
    }

    fn prio_create(&mut self) -> Result<(), Error> {
        boundary::prio_create().map_err(|_| Error::Port)
    }

    fn prio_measure(&mut self) -> Result<u64, Error> {
        if boundary::prio_ready() == 0 {
            return Err(Error::Port);
        }
        boundary::prio_reset();
        for _ in 0..PRIO_N {
            boundary::prio_release().map_err(|_| Error::Port)?;
        }
        for _ in 0..PRIO_N {
            boundary::prio_all().map_err(|_| Error::Port)?;
        }
        let ok = boundary::prio_check();
        for _ in 0..PRIO_N {
            boundary::prio_ack();
        }
        Ok(if ok { 1 } else { 0 })
    }

    fn prio_destroy(&mut self) -> Result<(), Error> {
        boundary::prio_destroy();
        Ok(())
    }

    fn maxdis_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_maxdis_create() })
    }

    fn maxdis_measure(&mut self) -> Result<u64, Error> {
        // The shared C path owns the begin/arm/wait/ISR sequence, so no FFI
        // calls fall between the probe arming and the ISR timestamp.
        let mut ticks = 0u64;
        status_case(unsafe { bench_maxdis_measure(&mut ticks) })?;
        Ok(ticks)
    }

    fn maxdis_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_maxdis_destroy() })
    }

    fn nested_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_nested_create() })
    }

    fn nested_measure(&mut self) -> Result<u64, Error> {
        // Three baseline runs and three nested runs; the sample is the
        // difference of the per-mode minima (best case isolates the extra
        // work of the nested entry).
        let mut baseline_ticks = unsafe { bench_rust_nested_run(0) };
        let mut nested_ticks = unsafe { bench_rust_nested_run(1) };
        for _ in 0..2 {
            let baseline_sample = unsafe { bench_rust_nested_run(0) };
            let nested_sample = unsafe { bench_rust_nested_run(1) };
            if baseline_sample < baseline_ticks {
                baseline_ticks = baseline_sample;
            }
            if nested_sample < nested_ticks {
                nested_ticks = nested_sample;
            }
        }
        Ok(u64::from(nested_ticks.saturating_sub(baseline_ticks)))
    }

    fn nested_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_nested_destroy() })
    }

    fn frag_create(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_frag_create() })
    }

    fn frag_measure(&mut self) -> Result<u64, Error> {
        let baseline_rc = unsafe { bench_rust_frag_baseline() };
        if baseline_rc <= 0 {
            return Err(Error::Port);
        }
        let baseline_bytes = baseline_rc as u64;
        let mut margin_bytes = baseline_bytes / 8;
        if margin_bytes < 1024 {
            margin_bytes = 1024;
        }
        let block_bytes = u64::from(unsafe { bench_rust_frag_bytes() });
        let block_count = unsafe { bench_rust_frag_n() };
        let mut allocated = 0u32;
        while allocated < block_count
            && u64::from(allocated) * block_bytes + margin_bytes < baseline_bytes
        {
            if unsafe { bench_rust_frag_alloc(allocated) } != 0 {
                break;
            }
            allocated += 1;
        }
        if allocated < 8 {
            for i in 0..allocated {
                unsafe { bench_rust_frag_free(i) };
            }
            return Err(Error::Port);
        }
        for i in (0..allocated).step_by(2) {
            unsafe { bench_rust_frag_free(i) };
        }
        let total_free_rc = unsafe { bench_rust_frag_free_bytes() };
        if total_free_rc <= 0 {
            for i in (1..allocated).step_by(2) {
                unsafe { bench_rust_frag_free(i) };
            }
            return Err(Error::Port);
        }
        let total_free = total_free_rc as u64;
        let mut low = 0u64;
        let mut high = total_free;
        while low < high {
            let mid = low + high.saturating_sub(low).div_ceil(2);
            if unsafe { bench_rust_frag_try(mid as u32) } != 0 {
                low = mid;
            } else {
                high = mid - 1;
            }
        }
        let largest_free = low;
        for i in (1..allocated).step_by(2) {
            unsafe { bench_rust_frag_free(i) };
        }
        let fragmented_bytes = total_free.saturating_sub(largest_free);
        Ok(fragmented_bytes * 1_000_000 / total_free)
    }

    fn frag_destroy(&mut self) -> Result<(), Error> {
        status(unsafe { bench_rust_frag_destroy() })
    }
}

/// Callback invoked once per retained sample; returns 0 on success.
pub type RecordFn =
    extern "C" fn(ctx: *mut c_void, case: u32, index: u32, ticks: u64, hz: u64) -> i32;
/// Variant-aware record callback (schema v2); optional.
pub type RecordVariantFn = extern "C" fn(
    ctx: *mut c_void,
    case: u32,
    variant: u32,
    index: u32,
    ticks: u64,
    hz: u64,
) -> i32;
/// Callback invoked after a case completes; returns 0 on success.
pub type FlushFn = extern "C" fn(ctx: *mut c_void) -> i32;

fn case_id_for(case: Case) -> u32 {
    match case {
        Case::TimerOverhead => 0,
        Case::QueueRoundtrip => 1,
        Case::TaskHandoff => 2,
        Case::PeriodicJitter => 3,
        Case::IsrToTask => 4,
        Case::YieldRoundtrip => 5,
        Case::PreemptLatency => 6,
        Case::IpcPingpong => 7,
        Case::MutexPriority => 8,
        Case::TimerCallback => 9,
        Case::TriggerToIsr => 10,
        Case::PeriodicJitterLoaded => 11,
        Case::SemaphoreLatency => 12,
        Case::MutexUncontended => 13,
        Case::AllocLatency => 14,
        Case::SleepAccuracy => 15,
        Case::NotificationLatency => 17,
        Case::SchedulerOverhead => 16,
        Case::PrioritySchedulingCorrectness => 26,
        Case::TickOverhead => 21,
        Case::MaxInterruptDisabled => 25,
        Case::NestedIrqOverhead => 20,
        Case::IpcPayloadSweep => 18,
        Case::IpcThroughput => 19,
        Case::ZeroCopyIpc => 27,
        Case::IdleCpuOverhead => 22,
        Case::CpuOverheadUnderLoad => 23,
        Case::HeapFragmentation => 24,
        Case::CommonArenaFragmentation => 28,
    }
}

/// Sink that forwards each retained sample to the C session callback.
struct CallbackSink {
    ctx: *mut c_void,
    record: RecordFn,
    record_variant: Option<RecordVariantFn>,
    flush: FlushFn,
}

impl Sink for CallbackSink {
    fn record(&mut self, case: Case, index: u32, ticks: u64, hz: u64) -> Result<(), Error> {
        if (self.record)(self.ctx, case_id_for(case), index, ticks, hz) == 0 {
            Ok(())
        } else {
            Err(Error::Port)
        }
    }

    fn record_variant(
        &mut self,
        case: Case,
        variant: u32,
        index: u32,
        ticks: u64,
        hz: u64,
    ) -> Result<(), Error> {
        if let Some(record) = self.record_variant {
            if record(self.ctx, case_id_for(case), variant, index, ticks, hz) == 0 {
                return Ok(());
            }
            return Err(Error::Port);
        }
        self.record(case, index, ticks, hz)
    }

    fn flush(&mut self) -> Result<(), Error> {
        if (self.flush)(self.ctx) == 0 {
            Ok(())
        } else {
            Err(Error::Port)
        }
    }
}

fn error_code(error: Error) -> i32 {
    match error {
        Error::Invalid => -1,
        Error::Unsupported => -2,
        Error::Port => -3,
        Error::Corrupt => -4,
        Error::DeadlineMiss => -5,
    }
}

/// Run one case with native Zephyr primitives. Returns 0 on success.
///
/// # Safety
/// `record` and `flush` must be valid C function pointers for the whole call;
/// `ctx` is passed through unchanged.
#[no_mangle]
pub unsafe extern "C" fn rust_bench_run_case(
    case_id: u32,
    warmup: u32,
    repetitions: u32,
    ctx: *mut c_void,
    record: RecordFn,
    record_variant: Option<RecordVariantFn>,
    flush: FlushFn,
) -> i32 {
    let case = match case_id {
        0 => Case::TimerOverhead,
        1 => Case::QueueRoundtrip,
        2 => Case::TaskHandoff,
        3 => Case::PeriodicJitter,
        4 => Case::IsrToTask,
        5 => Case::YieldRoundtrip,
        6 => Case::PreemptLatency,
        7 => Case::IpcPingpong,
        8 => Case::MutexPriority,
        9 => Case::TimerCallback,
        10 => Case::TriggerToIsr,
        11 => Case::PeriodicJitterLoaded,
        12 => Case::SemaphoreLatency,
        13 => Case::MutexUncontended,
        14 => Case::AllocLatency,
        15 => Case::SleepAccuracy,
        17 => Case::NotificationLatency,
        16 => Case::SchedulerOverhead,
        26 => Case::PrioritySchedulingCorrectness,
        21 => Case::TickOverhead,
        25 => Case::MaxInterruptDisabled,
        20 => Case::NestedIrqOverhead,
        18 => Case::IpcPayloadSweep,
        19 => Case::IpcThroughput,
        27 => Case::ZeroCopyIpc,
        24 => Case::HeapFragmentation,
        28 => Case::CommonArenaFragmentation,
        _ => return -2,
    };
    let mut port = ZephyrPort::new();
    let mut sink = CallbackSink {
        ctx,
        record,
        record_variant,
        flush,
    };
    match run_case(
        &mut port,
        &mut sink,
        case,
        Settings {
            warmup,
            repetitions,
        },
    ) {
        Ok(()) => 0,
        Err(error) => error_code(error),
    }
}

/// Number of cases this adapter can execute.
#[no_mangle]
pub extern "C" fn rust_bench_case_count() -> u32 {
    5
}

#[cfg(not(test))]
#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {
        core::hint::spin_loop();
    }
}
