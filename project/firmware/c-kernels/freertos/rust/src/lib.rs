//! FreeRTOS Rust adapter for the STM32F4 Discovery benchmark.
//!
//! The Rust application core supplies the measured work (DWT CYCCNT timer and
//! the native FreeRTOS queue) and the benchmark sequencing; the C session keeps
//! the UART/JSON protocol layer and calls into this crate per case. This keeps
//! the compared primitives native to each language while sharing one wire
//! contract (UART protocol v1).
//!
//! Build with `cargo build --release --target thumbv7em-none-eabihf` (hard-float,
//! to match the C firmware's `-mfloat-abi=hard`); the staticlib is linked into
//! the C firmware via `make RUST=1` in firmware/freertos/c/stm32f4.
//!
//! The C firmware provides `bench_rust_now_ticks` and `bench_rust_ticks_per_second`.
//! The native FreeRTOS symbols used directly are `xQueueGenericCreate`,
//! `xQueueGenericSend`, `xQueueReceive` and `vQueueDelete`.
#![no_std]

use core::ffi::c_void;
use rtos_bench::{run_case, Case, Error, Port, Settings, Sink};

mod boundary;
#[allow(unused_imports)]
use boundary::*;

/// Callback invoked once per retained sample; returns 0 on success.
pub type RecordFn =
    extern "C" fn(ctx: *mut c_void, case: u32, index: u32, ticks: u64, hz: u64) -> i32;
/// Variant-aware record callback (schema v2); optional (None falls back to
/// `RecordFn`). `variant` is payload bytes for case 18.
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

/// Native FreeRTOS port: DWT timer plus a capacity-one u32 queue.
pub struct FreertosPort {
    queue: *mut c_void,
    sem: *mut c_void,
    mutex_u: *mut c_void,
}

impl FreertosPort {
    /// An empty port; the queue is created by the runner outside timing.
    pub fn new() -> Self {
        Self {
            queue: core::ptr::null_mut(),
            sem: core::ptr::null_mut(),
            mutex_u: core::ptr::null_mut(),
        }
    }
}

impl Default for FreertosPort {
    fn default() -> Self {
        Self::new()
    }
}

impl Port for FreertosPort {
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
        self.queue =
            unsafe { xQueueGenericCreate(1, core::mem::size_of::<u32>() as u32, QUEUE_TYPE_BASE) };
        if self.queue.is_null() {
            Err(Error::Port)
        } else {
            Ok(())
        }
    }

    fn queue_send(&mut self, value: u32) -> Result<(), Error> {
        if self.queue.is_null() {
            return Err(Error::Port);
        }
        if unsafe { queue_send_u32(self.queue, value, 0) } {
            Ok(())
        } else {
            Err(Error::Port)
        }
    }

    fn queue_receive(&mut self) -> Result<u32, Error> {
        if self.queue.is_null() {
            return Err(Error::Port);
        }
        unsafe { queue_recv_u32(self.queue, 0) }.ok_or(Error::Port)
    }

    fn queue_destroy(&mut self) -> Result<(), Error> {
        if !self.queue.is_null() {
            unsafe { vQueueDelete(self.queue) };
            self.queue = core::ptr::null_mut();
        }
        Ok(())
    }

    fn handoff_create(&mut self) -> Result<(), Error> {
        unsafe {
            S_SIGNAL = semaphore_create();
            S_DONE = semaphore_create();
            if S_SIGNAL.is_null() || S_DONE.is_null() {
                return Err(Error::Port);
            }
            // Peer runs one priority above the benchmark task so the signal
            // preempts immediately, measuring the full wake path.
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            S_PEER = task_create(peer_entry, c"bench_peer", 0, priority);
            if S_PEER.is_null() {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn handoff_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_SIGNAL.is_null() || S_DONE.is_null() {
                return Err(Error::Port);
            }
            if semaphore_give(S_SIGNAL) != PD_TRUE {
                return Err(Error::Port);
            }
            if xQueueSemaphoreTake(S_DONE, PORT_MAX_DELAY) != PD_TRUE {
                return Err(Error::Port);
            }
            Ok(S_WAKE_TICKS)
        }
    }

    fn handoff_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_PEER.is_null() {
                vTaskDelete(S_PEER);
                S_PEER = core::ptr::null_mut();
            }
            if !S_SIGNAL.is_null() {
                vQueueDelete(S_SIGNAL);
                S_SIGNAL = core::ptr::null_mut();
            }
            if !S_DONE.is_null() {
                vQueueDelete(S_DONE);
                S_DONE = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn jitter_create(&mut self) -> Result<(), Error> {
        unsafe {
            S_JITTER_LAST = xTaskGetTickCount();
            // Align to the tick grid so every measured interval is one period.
            jitter_align();
            S_JITTER_PREV = bench_rust_now_ticks();
        }
        Ok(())
    }

    fn jitter_measure(&mut self) -> Result<u64, Error> {
        let period = jitter_period_ticks(self.ticks_per_second());
        unsafe {
            let now = jitter_step();
            let interval = now.saturating_sub(S_JITTER_PREV);
            S_JITTER_PREV = now;
            Ok(interval.abs_diff(period))
        }
    }

    fn jitter_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn isr_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_isr_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn isr_measure(&mut self) -> Result<u64, Error> {
        // Use the C measurement path so the wake timestamp is captured
        // immediately after the semaphore take, exactly as in the C runner.
        let mut ticks = 0u64;
        match unsafe { bench_isr_measure(&mut ticks) } {
            0 => Ok(ticks),
            4 => Err(Error::Corrupt),
            _ => Err(Error::Port),
        }
    }

    fn isr_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_isr_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn trigger_isr_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_trigger_isr_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
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
        match unsafe { bench_trigger_isr_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn pjit_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_pjit_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn pjit_measure(&mut self) -> Result<u64, Error> {
        // The C path captures the wake timestamp immediately after the release
        // wait and performs the release-grid arithmetic on that timestamp.
        let mut ticks = 0u64;
        match unsafe { bench_pjit_measure(&mut ticks) } {
            0 => Ok(ticks),
            4 => Err(Error::Corrupt),
            5 => Err(Error::DeadlineMiss),
            _ => Err(Error::Port),
        }
    }

    fn pjit_set_load(&mut self, load_pct: u32) -> Result<(), Error> {
        match unsafe { bench_pjit_set_load(load_pct) } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn pjit_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_pjit_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn yield_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_yield_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn yield_now(&mut self) -> Result<(), Error> {
        match unsafe { bench_yield_now() } {
            0 => Ok(()),
            4 => Err(Error::Corrupt),
            _ => Err(Error::Port),
        }
    }

    fn yield_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_yield_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn peers_set(&mut self, count: u32) -> Result<(), Error> {
        match unsafe { bench_peers_set(count) } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn preempt_create(&mut self) -> Result<(), Error> {
        unsafe {
            S_PREEMPT_TASK = core::ptr::null_mut();
            S_PREEMPT_WAKE = 0;
            S_PREEMPT_SEQ = 0;
            S_PREEMPT_READY = 0;
            S_PREEMPT_SEM = semaphore_create();
            if S_PREEMPT_SEM.is_null() {
                return Err(Error::Port);
            }
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            S_PREEMPT_TASK = task_create(preempt_peer_entry, c"bench_bpreempt", 0, priority);
            if S_PREEMPT_TASK.is_null() {
                return Err(Error::Port);
            }
            for _ in 0..1000 {
                if S_PREEMPT_READY != 0 {
                    break;
                }
                bench_rust_yield();
            }
            if S_PREEMPT_READY == 0 {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn preempt_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_PREEMPT_SEM.is_null() || S_PREEMPT_TASK.is_null() {
                return Err(Error::Port);
            }
            let seq = S_PREEMPT_SEQ;
            let t0 = bench_rust_now_ticks() as u32;
            if semaphore_give(S_PREEMPT_SEM) != PD_TRUE {
                return Err(Error::Port);
            }
            if S_PREEMPT_SEQ == seq {
                return Err(Error::Corrupt);
            }
            Ok(u64::from((S_PREEMPT_WAKE as u32).wrapping_sub(t0)))
        }
    }

    fn preempt_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_PREEMPT_TASK.is_null() {
                vTaskDelete(S_PREEMPT_TASK);
                S_PREEMPT_TASK = core::ptr::null_mut();
            }
            if !S_PREEMPT_SEM.is_null() {
                vQueueDelete(S_PREEMPT_SEM);
                S_PREEMPT_SEM = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn ipc_create(&mut self) -> Result<(), Error> {
        unsafe {
            let item = core::mem::size_of::<u32>() as u32;
            S_IPC_REQ = xQueueGenericCreate(1, item, QUEUE_TYPE_BASE);
            S_IPC_REP = xQueueGenericCreate(1, item, QUEUE_TYPE_BASE);
            if S_IPC_REQ.is_null() || S_IPC_REP.is_null() {
                return Err(Error::Port);
            }
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            S_IPC_PEER = task_create(ipc_peer_entry, c"bench_ipc", 0, priority);
            if S_IPC_PEER.is_null() {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn ipc_measure(&mut self, value: u32) -> Result<u64, Error> {
        unsafe {
            if S_IPC_REQ.is_null() || S_IPC_REP.is_null() {
                return Err(Error::Port);
            }
            let start = bench_rust_now_ticks();
            if !queue_send_u32(S_IPC_REQ, value, PORT_MAX_DELAY) {
                return Err(Error::Port);
            }
            let received_value = match queue_recv_u32(S_IPC_REP, PORT_MAX_DELAY) {
                Some(received) => received,
                None => return Err(Error::Port),
            };
            let end = bench_rust_now_ticks();
            if received_value != value {
                return Err(Error::Corrupt);
            }
            Ok(end - start)
        }
    }

    fn ipc_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_IPC_PEER.is_null() {
                vTaskDelete(S_IPC_PEER);
                S_IPC_PEER = core::ptr::null_mut();
            }
            if !S_IPC_REQ.is_null() {
                vQueueDelete(S_IPC_REQ);
                S_IPC_REQ = core::ptr::null_mut();
            }
            if !S_IPC_REP.is_null() {
                vQueueDelete(S_IPC_REP);
                S_IPC_REP = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn ipc_payload_create(&mut self) -> Result<(), Error> {
        unsafe {
            ipc_payload_fill();
            if !ipc_payload_open() {
                return Err(Error::Port);
            }
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            for i in 0..IPC_PAYLOAD_N {
                if !ipc_payload_spawn(i, priority) {
                    return Err(Error::Port);
                }
            }
        }
        Ok(())
    }

    fn ipc_payload_measure(&mut self, payload_bytes: u32) -> Result<u64, Error> {
        unsafe {
            let idx = match IPC_PAYLOAD_BYTES.iter().position(|&b| b == payload_bytes) {
                Some(i) => i,
                None => return Err(Error::Port),
            };
            let start = bench_rust_now_ticks();
            if !ipc_payload_send(idx) {
                return Err(Error::Port);
            }
            if !ipc_payload_recv(idx) {
                return Err(Error::Port);
            }
            let end = bench_rust_now_ticks();
            if !ipc_payload_verify(payload_bytes as usize) {
                return Err(Error::Corrupt);
            }
            Ok(end - start)
        }
    }

    fn ipc_payload_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            ipc_payload_close();
        }
        Ok(())
    }

    fn tp_create(&mut self) -> Result<(), Error> {
        unsafe {
            tp_fill();
            S_TP_QUIT = 0;
            S_TP_SEEN = 0;
            S_TP_TARGET = 0;
            S_TP_Q = xQueueGenericCreate(TP_QUEUE_DEPTH, TP_MSG_BYTES, QUEUE_TYPE_BASE);
            S_TP_DONE = semaphore_create();
            if S_TP_Q.is_null() || S_TP_DONE.is_null() {
                return Err(Error::Port);
            }
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            S_TP_PEER = task_create(tp_peer_entry, c"bench_tp", 0, priority);
            if S_TP_PEER.is_null() {
                return Err(Error::Port);
            }
            S_TP_ACTIVE = 1;
        }
        Ok(())
    }

    fn tp_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_TP_ACTIVE == 0 {
                return Err(Error::Port);
            }
            S_TP_SEEN = 0;
            S_TP_TARGET = TP_MSGS;
            let mut total_ticks: u64 = 0;
            let mut previous_ticks = bench_rust_now_ticks() as u32;
            for _ in 0..TP_MSGS {
                if !tp_send() {
                    return Err(Error::Port);
                }
                let now = bench_rust_now_ticks() as u32;
                total_ticks += u64::from(now.wrapping_sub(previous_ticks));
                previous_ticks = now;
            }
            if xQueueSemaphoreTake(S_TP_DONE, PORT_MAX_DELAY) != PD_TRUE {
                return Err(Error::Port);
            }
            let now = bench_rust_now_ticks() as u32;
            total_ticks += u64::from(now.wrapping_sub(previous_ticks));
            Ok(total_ticks)
        }
    }

    fn tp_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if S_TP_ACTIVE != 0 {
                S_TP_QUIT = 1;
                vTaskDelete(S_TP_PEER);
                S_TP_PEER = core::ptr::null_mut();
                vQueueDelete(S_TP_Q);
                S_TP_Q = core::ptr::null_mut();
                vQueueDelete(S_TP_DONE);
                S_TP_DONE = core::ptr::null_mut();
                S_TP_ACTIVE = 0;
            }
        }
        Ok(())
    }

    fn zcopy_create(&mut self) -> Result<(), Error> {
        unsafe {
            zc_fill();
            S_ZC_CP_REQ = xQueueGenericCreate(1, ZCOPY_BYTES as u32, QUEUE_TYPE_BASE);
            S_ZC_CP_REP = xQueueGenericCreate(1, ZCOPY_BYTES as u32, QUEUE_TYPE_BASE);
            let pointer_size_bytes = core::mem::size_of::<*mut u8>() as u32;
            S_ZC_ZC_REQ = xQueueGenericCreate(1, pointer_size_bytes, QUEUE_TYPE_BASE);
            S_ZC_ZC_REP = xQueueGenericCreate(1, pointer_size_bytes, QUEUE_TYPE_BASE);
            if S_ZC_CP_REQ.is_null()
                || S_ZC_CP_REP.is_null()
                || S_ZC_ZC_REQ.is_null()
                || S_ZC_ZC_REP.is_null()
            {
                return Err(Error::Port);
            }
            let priority = uxTaskPriorityGet(core::ptr::null_mut()) + 1;
            S_ZC_CP_PEER = task_create(zcopy_copy_peer_entry, c"bench_zccp", 0, priority);
            if S_ZC_CP_PEER.is_null() {
                return Err(Error::Port);
            }
            S_ZC_ZC_PEER = task_create(zcopy_ref_peer_entry, c"bench_zcref", 0, priority);
            if S_ZC_ZC_PEER.is_null() {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn zcopy_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_ZC_CP_REQ.is_null() || S_ZC_ZC_REQ.is_null() {
                return Err(Error::Port);
            }
            let start = bench_rust_now_ticks();
            if !zc_cp_send() {
                return Err(Error::Port);
            }
            if !zc_cp_recv() {
                return Err(Error::Port);
            }
            let copied = bench_rust_now_ticks() - start;
            if !zc_cp_verify() {
                return Err(Error::Corrupt);
            }
            let start2 = bench_rust_now_ticks();
            if !zc_zc_roundtrip() {
                return Err(Error::Corrupt);
            }
            let zcopy = bench_rust_now_ticks() - start2;
            if !zc_shared_verify() {
                return Err(Error::Corrupt);
            }
            Ok(if copied > zcopy {
                copied - zcopy
            } else {
                u64::MAX
            })
        }
    }

    fn zcopy_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_ZC_CP_PEER.is_null() {
                vTaskDelete(S_ZC_CP_PEER);
                S_ZC_CP_PEER = core::ptr::null_mut();
            }
            if !S_ZC_ZC_PEER.is_null() {
                vTaskDelete(S_ZC_ZC_PEER);
                S_ZC_ZC_PEER = core::ptr::null_mut();
            }
            if !S_ZC_CP_REQ.is_null() {
                vQueueDelete(S_ZC_CP_REQ);
                S_ZC_CP_REQ = core::ptr::null_mut();
            }
            if !S_ZC_CP_REP.is_null() {
                vQueueDelete(S_ZC_CP_REP);
                S_ZC_CP_REP = core::ptr::null_mut();
            }
            if !S_ZC_ZC_REQ.is_null() {
                vQueueDelete(S_ZC_ZC_REQ);
                S_ZC_ZC_REQ = core::ptr::null_mut();
            }
            if !S_ZC_ZC_REP.is_null() {
                vQueueDelete(S_ZC_ZC_REP);
                S_ZC_ZC_REP = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn cpu_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_cpu_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn cpu_measure(&mut self, load_pct: u32) -> Result<u64, Error> {
        const WINDOW_MS: u32 = 500;
        const PPM_SCALE: u64 = 1_000_000;
        unsafe {
            if load_pct > 0 && bench_rust_cpu_start(load_pct) != 0 {
                return Err(Error::Port);
            }
            bench_rust_cpu_arm();
            let idle0 = if load_pct > 0 {
                bench_rust_cpu_idle()
            } else {
                0
            };
            let start = bench_rust_now_ticks() as u32;
            vTaskDelay(WINDOW_MS * bench_rust_tick_hz() / 1000);
            let elapsed_ticks = (bench_rust_now_ticks() as u32).wrapping_sub(start);
            let idle1 = if load_pct > 0 {
                bench_rust_cpu_idle()
            } else {
                0
            };
            let accumulated_ticks = bench_rust_cpu_accum();
            bench_rust_cpu_disarm();
            if load_pct > 0 {
                bench_rust_cpu_stop();
            }
            if elapsed_ticks == 0 {
                return Err(Error::Port);
            }
            if load_pct == 0 {
                return Ok(accumulated_ticks * PPM_SCALE / u64::from(elapsed_ticks));
            }
            let idle_delta_ticks = u64::from(idle1.wrapping_sub(idle0));
            let true_idle_ticks = idle_delta_ticks.saturating_sub(accumulated_ticks);
            let busy_ticks = u64::from(elapsed_ticks).saturating_sub(true_idle_ticks);
            Ok(busy_ticks * PPM_SCALE / u64::from(elapsed_ticks))
        }
    }

    fn cpu_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_cpu_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn frag_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_frag_create() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn frag_measure(&mut self) -> Result<u64, Error> {
        const FRAG_N: usize = 1024;
        const FRAG_MARGIN: usize = 16384;
        unsafe {
            frag_probe(1);
            let mut allocated = 0usize;
            while allocated < FRAG_N && frag_free_heap() > FRAG_MARGIN {
                if !frag_alloc(allocated) {
                    break;
                }
                allocated += 1;
            }
            if allocated < 8 {
                for i in 0..allocated {
                    frag_free(i);
                }
                frag_probe(0);
                return Err(Error::Port);
            }
            let mut i = 0usize;
            while i < allocated {
                frag_free(i);
                i += 2;
            }
            let total_free_bytes = frag_free_heap();
            let (mut low, mut high) = (0usize, total_free_bytes);
            while low < high {
                let mid = low + high.saturating_sub(low).div_ceil(2);
                if frag_try(mid) {
                    low = mid;
                } else {
                    high = mid - 1;
                }
            }
            let largest_free_bytes = low;
            frag_probe(0);
            let mut j = 1usize;
            while j < allocated {
                frag_free(j);
                j += 2;
            }
            if total_free_bytes == 0 {
                return Err(Error::Port);
            }
            let fragmented_bytes = total_free_bytes.saturating_sub(largest_free_bytes);
            Ok((fragmented_bytes as u64) * 1_000_000 / (total_free_bytes as u64))
        }
    }

    fn frag_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_frag_destroy() } {
            0 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn mutex_create(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_MUTEX.is_null() {
                return Ok(());
            }
            let task_priority = uxTaskPriorityGet(core::ptr::null_mut());
            if task_priority < 2 {
                return Err(Error::Port);
            }
            S_MUTEX = xQueueCreateMutex(QUEUE_TYPE_MUTEX);
            S_M_REQ = semaphore_create();
            S_M_HELD = semaphore_create();
            S_M_REL = semaphore_create();
            S_M_GO = semaphore_create();
            S_M_DONE = semaphore_create();
            if S_MUTEX.is_null()
                || S_M_REQ.is_null()
                || S_M_HELD.is_null()
                || S_M_REL.is_null()
                || S_M_GO.is_null()
                || S_M_DONE.is_null()
            {
                return Err(Error::Port);
            }
            S_M_TASK = task_create(mutex_med_entry, c"bench_m", 0, task_priority - 1);
            if S_M_TASK.is_null() {
                return Err(Error::Port);
            }
            S_L_TASK = task_create(mutex_low_entry, c"bench_l", 0, task_priority - 2);
            if S_L_TASK.is_null() {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn mutex_measure(&mut self) -> Result<u64, Error> {
        let (t0, t1) = unsafe {
            if S_MUTEX.is_null() {
                return Err(Error::Port);
            }
            S_M_STOP = 0;
            if semaphore_give(S_M_REQ) != PD_TRUE {
                return Err(Error::Port);
            }
            if xQueueSemaphoreTake(S_M_HELD, PORT_MAX_DELAY) != PD_TRUE {
                return Err(Error::Port);
            }
            let _ = semaphore_give(S_M_GO);
            let t0 = self.now_ticks()?;
            let _ = semaphore_give(S_M_REL);
            if xQueueSemaphoreTake(S_MUTEX, PORT_MAX_DELAY) != PD_TRUE {
                return Err(Error::Port);
            }
            let t1 = self.now_ticks()?;
            S_M_STOP = 1;
            let _ = xQueueSemaphoreTake(S_M_DONE, PORT_MAX_DELAY);
            let _ = semaphore_give(S_MUTEX);
            (t0, t1)
        };
        Ok(t1 - t0)
    }

    fn mutex_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_M_TASK.is_null() {
                vTaskDelete(S_M_TASK);
                S_M_TASK = core::ptr::null_mut();
            }
            if !S_L_TASK.is_null() {
                vTaskDelete(S_L_TASK);
                S_L_TASK = core::ptr::null_mut();
            }
            if !S_MUTEX.is_null() {
                vQueueDelete(S_MUTEX);
                S_MUTEX = core::ptr::null_mut();
            }
            if !S_M_REQ.is_null() {
                vQueueDelete(S_M_REQ);
                S_M_REQ = core::ptr::null_mut();
            }
            if !S_M_HELD.is_null() {
                vQueueDelete(S_M_HELD);
                S_M_HELD = core::ptr::null_mut();
            }
            if !S_M_REL.is_null() {
                vQueueDelete(S_M_REL);
                S_M_REL = core::ptr::null_mut();
            }
            if !S_M_GO.is_null() {
                vQueueDelete(S_M_GO);
                S_M_GO = core::ptr::null_mut();
            }
            if !S_M_DONE.is_null() {
                vQueueDelete(S_M_DONE);
                S_M_DONE = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn timer_create(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_TIMER.is_null() {
                return Ok(());
            }
            S_TIMER_SEM = semaphore_create();
            if S_TIMER_SEM.is_null() {
                return Err(Error::Port);
            }
            let period = bench_rust_tick_hz() / 100; // 10 ms
            S_TIMER = timer_create(period);
            if S_TIMER.is_null() {
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn timer_measure(&mut self) -> Result<u64, Error> {
        let nominal = self.ticks_per_second() / 100;
        unsafe {
            if S_TIMER.is_null() {
                return Err(Error::Port);
            }
            let _ = xQueueSemaphoreTake(S_TIMER_SEM, 0); // drain stale signal
            let t0 = bench_rust_now_ticks();
            if bench_rust_timer_start(S_TIMER) != PD_TRUE {
                return Err(Error::Port);
            }
            if xQueueSemaphoreTake(S_TIMER_SEM, PORT_MAX_DELAY) != PD_TRUE {
                return Err(Error::Port);
            }
            let interval = S_TIMER_FIRE.saturating_sub(t0);
            Ok(interval.abs_diff(nominal))
        }
    }

    fn timer_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_TIMER.is_null() {
                let _ = bench_rust_timer_delete(S_TIMER);
                S_TIMER = core::ptr::null_mut();
            }
            if !S_TIMER_SEM.is_null() {
                vQueueDelete(S_TIMER_SEM);
                S_TIMER_SEM = core::ptr::null_mut();
            }
        }
        Ok(())
    }

    fn sem_create(&mut self) -> Result<(), Error> {
        self.sem = unsafe { semaphore_create() };
        if self.sem.is_null() {
            return Err(Error::Port);
        }
        let _ = unsafe { xQueueSemaphoreTake(self.sem, 0) }; // arm the first take
        Ok(())
    }

    fn sem_measure(&mut self) -> Result<u64, Error> {
        if self.sem.is_null() {
            return Err(Error::Port);
        }
        let start = self.now_ticks()?;
        if unsafe { semaphore_give(self.sem) } != PD_TRUE {
            return Err(Error::Port);
        }
        if unsafe { xQueueSemaphoreTake(self.sem, 0) } != PD_TRUE {
            return Err(Error::Corrupt);
        }
        let end = self.now_ticks()?;
        if end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn sem_destroy(&mut self) -> Result<(), Error> {
        if !self.sem.is_null() {
            unsafe { vQueueDelete(self.sem) };
            self.sem = core::ptr::null_mut();
        }
        Ok(())
    }

    fn mutex_u_create(&mut self) -> Result<(), Error> {
        self.mutex_u = unsafe { xQueueCreateMutex(QUEUE_TYPE_MUTEX) };
        if self.mutex_u.is_null() {
            Err(Error::Port)
        } else {
            Ok(())
        }
    }

    fn mutex_u_measure(&mut self) -> Result<u64, Error> {
        if self.mutex_u.is_null() {
            return Err(Error::Port);
        }
        let start = self.now_ticks()?;
        if unsafe { xQueueSemaphoreTake(self.mutex_u, PORT_MAX_DELAY) } != PD_TRUE {
            return Err(Error::Port);
        }
        if unsafe { semaphore_give(self.mutex_u) } != PD_TRUE {
            return Err(Error::Port);
        }
        let end = self.now_ticks()?;
        if end < start {
            return Err(Error::Corrupt);
        }
        Ok(end - start)
    }

    fn mutex_u_destroy(&mut self) -> Result<(), Error> {
        if !self.mutex_u.is_null() {
            unsafe { vQueueDelete(self.mutex_u) };
            self.mutex_u = core::ptr::null_mut();
        }
        Ok(())
    }

    fn alloc_create(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn alloc_measure(&mut self) -> Result<u64, Error> {
        let start = self.now_ticks()?;
        let p = unsafe { pvPortMalloc(64) };
        if !p.is_null() {
            unsafe { vPortFree(p) };
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
        let start = self.now_ticks()?;
        unsafe { vTaskDelay(1) };
        let end = self.now_ticks()?;
        let nominal = self.ticks_per_second() / 1000;
        Ok(end.saturating_sub(start).abs_diff(nominal))
    }

    fn sched_create(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn sched_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            for _ in 0..4 {
                let before = xTaskGetTickCount();
                let start = bench_rust_now_ticks();
                bench_rust_yield();
                let end = bench_rust_now_ticks();
                if xTaskGetTickCount() == before && end >= start {
                    return Ok(end - start);
                }
            }
        }
        Err(Error::Corrupt)
    }

    fn sched_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }

    fn notify_create(&mut self) -> Result<(), Error> {
        unsafe {
            S_NOTIFY_GO = semaphore_create();
            if S_NOTIFY_GO.is_null() {
                return Err(Error::Port);
            }
            S_NOTIFY_TARGET = xTaskGetCurrentTaskHandle();
            // Peer runs one priority below so it cannot signal before we block.
            let priority = uxTaskPriorityGet(core::ptr::null_mut()).saturating_sub(1);
            S_NOTIFY_PEER = task_create(notify_peer_entry, c"bench_notify", 0, priority);
            if S_NOTIFY_PEER.is_null() {
                vQueueDelete(S_NOTIFY_GO);
                S_NOTIFY_GO = core::ptr::null_mut();
                return Err(Error::Port);
            }
        }
        Ok(())
    }

    fn notify_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_NOTIFY_GO.is_null() {
                return Err(Error::Port);
            }
            let _ = bench_rust_notify_take(1, 0); // drain stale notification
            if semaphore_give(S_NOTIFY_GO) != PD_TRUE {
                return Err(Error::Port);
            }
            if bench_rust_notify_take(1, PORT_MAX_DELAY) == 0 {
                return Err(Error::Port);
            }
            let end = bench_rust_now_ticks();
            if end < S_NOTIFY_SIGNAL {
                return Err(Error::Corrupt);
            }
            Ok(end - S_NOTIFY_SIGNAL)
        }
    }

    fn notify_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            if !S_NOTIFY_PEER.is_null() {
                vTaskDelete(S_NOTIFY_PEER);
                S_NOTIFY_PEER = core::ptr::null_mut();
            }
            if !S_NOTIFY_GO.is_null() {
                vQueueDelete(S_NOTIFY_GO);
                S_NOTIFY_GO = core::ptr::null_mut();
            }
            S_NOTIFY_TARGET = core::ptr::null_mut();
        }
        Ok(())
    }

    fn prio_create(&mut self) -> Result<(), Error> {
        unsafe {
            S_PRIO_RELEASE = xQueueCreateCountingSemaphore(PRIO_N as u32, 0);
            S_PRIO_ALL = xQueueCreateCountingSemaphore(PRIO_N as u32, 0);
            S_PRIO_ACK = xQueueCreateCountingSemaphore(PRIO_N as u32, 0);
            if S_PRIO_RELEASE.is_null() || S_PRIO_ALL.is_null() || S_PRIO_ACK.is_null() {
                return Err(Error::Port);
            }
            let base = uxTaskPriorityGet(core::ptr::null_mut());
            for i in 0..PRIO_N {
                let priority = base + 1 + i as u32;
                let bench_h = task_create(prio_peer_entry, c"bench_prio", i, priority);
                prio_task_set(i, bench_h);
                if bench_h.is_null() {
                    return Err(Error::Port);
                }
            }
        }
        Ok(())
    }

    fn prio_measure(&mut self) -> Result<u64, Error> {
        unsafe {
            if S_PRIO_RELEASE.is_null() {
                return Err(Error::Port);
            }
            S_PRIO_N = 0;
            vTaskSuspendAll();
            for _ in 0..PRIO_N {
                let _ = semaphore_give(S_PRIO_RELEASE);
            }
            let _ = xTaskResumeAll();
            for _ in 0..PRIO_N {
                if xQueueSemaphoreTake(S_PRIO_ALL, PORT_MAX_DELAY) != PD_TRUE {
                    return Err(Error::Port);
                }
            }
            let ok = prio_trace_ok();
            for _ in 0..PRIO_N {
                let _ = semaphore_give(S_PRIO_ACK);
            }
            Ok(if ok { 1 } else { 0 })
        }
    }

    fn prio_destroy(&mut self) -> Result<(), Error> {
        unsafe {
            prio_teardown();
        }
        Ok(())
    }

    fn tick_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_tick_create() } {
            0 => Ok(()),
            2 => Err(Error::Unsupported),
            _ => Err(Error::Port),
        }
    }

    fn tick_measure(&mut self) -> Result<u64, Error> {
        // The tick trace hook latches one sample per SysTick; wait for it.
        for _ in 0..3 {
            let mut ticks = 0u64;
            if unsafe { bench_rust_tick_take(&mut ticks) } != 0 {
                return Ok(ticks);
            }
            unsafe { vTaskDelay(1) };
        }
        Err(Error::Port)
    }

    fn tick_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_tick_destroy() } {
            0 | 2 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn maxdis_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_maxdis_create() } {
            0 => Ok(()),
            2 => Err(Error::Unsupported),
            _ => Err(Error::Port),
        }
    }

    fn maxdis_measure(&mut self) -> Result<u64, Error> {
        // The shared C path owns the begin/arm/wait/ISR sequence, so no FFI
        // calls fall between the probe arming and the ISR timestamp.
        let mut ticks = 0u64;
        match unsafe { bench_maxdis_measure(&mut ticks) } {
            0 => Ok(ticks),
            4 => Err(Error::Corrupt),
            _ => Err(Error::Port),
        }
    }

    fn maxdis_destroy(&mut self) -> Result<(), Error> {
        match unsafe { bench_maxdis_destroy() } {
            0 | 2 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn nested_create(&mut self) -> Result<(), Error> {
        match unsafe { bench_nested_create() } {
            0 => Ok(()),
            2 => Err(Error::Unsupported),
            _ => Err(Error::Port),
        }
    }

    fn nested_measure(&mut self) -> Result<u64, Error> {
        // Min of three runs each; the least-interfered run best isolates the
        // fixed work, so the difference is the higher-ISR nesting cost.
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
        match unsafe { bench_nested_destroy() } {
            0 | 2 => Ok(()),
            _ => Err(Error::Port),
        }
    }

    fn sleep_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
}

/// Map a case to its wire protocol ID (shared by `record` and `record_variant`).
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
        let case_id = case_id_for(case);
        if (self.record)(self.ctx, case_id, index, ticks, hz) == 0 {
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
            let case_id = case_id_for(case);
            if record(self.ctx, case_id, variant, index, ticks, hz) == 0 {
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

/// Run one case with native FreeRTOS primitives. Returns 0 on success.
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
        22 => Case::IdleCpuOverhead,
        23 => Case::CpuOverheadUnderLoad,
        24 => Case::HeapFragmentation,
        28 => Case::CommonArenaFragmentation,
        _ => return -2,
    };
    let mut port = FreertosPort::new();
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
