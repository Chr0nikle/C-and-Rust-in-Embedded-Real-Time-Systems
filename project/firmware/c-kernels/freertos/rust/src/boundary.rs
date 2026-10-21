//! Raw FreeRTOS FFI and kernel-object state for the Rust adapter. The
//! checked application layer (lib.rs) reaches the kernel only through the
//! functions declared here, so the unchecked pointer work is confined to this
//! module and stays out of the application code.
#![allow(dead_code)]

use core::ffi::c_void;

/// `pdTRUE` from FreeRTOS.
pub(crate) const PD_TRUE: i32 = 1;

extern "C" {
    pub(crate) fn bench_rust_now_ticks() -> u64;
    pub(crate) fn bench_rust_ticks_per_second() -> u64;

    // Complete C measurement paths used by the C runner.  Calling these from
    // Rust keeps the timestamp boundaries identical for both languages.
    pub(crate) fn bench_isr_measure(ticks: *mut u64) -> i32;
    pub(crate) fn bench_pjit_measure(ticks: *mut u64) -> i32;
    pub(crate) fn bench_maxdis_measure(ticks: *mut u64) -> i32;

    // FreeRTOS `xQueueCreate`/`xQueueSend` are macros; call the underlying
    // functions the C build exports.
    pub(crate) fn xQueueGenericCreate(
        queue_length: u32,
        item_size: u32,
        queue_type: u8,
    ) -> *mut c_void;
    pub(crate) fn xQueueCreateMutex(queue_type: u8) -> *mut c_void;
    pub(crate) fn xQueueGenericSend(
        queue: *mut c_void,
        item: *const c_void,
        ticks_to_wait: u32,
        copy_position: u32,
    ) -> i32;
    pub(crate) fn xQueueReceive(queue: *mut c_void, buffer: *mut c_void, ticks_to_wait: u32)
        -> i32;
    pub(crate) fn xQueueSemaphoreTake(semaphore: *mut c_void, ticks_to_wait: u32) -> i32;
    pub(crate) fn vQueueDelete(queue: *mut c_void);
    pub(crate) fn pvPortMalloc(size: usize) -> *mut c_void;
    pub(crate) fn vPortFree(p: *mut c_void);
    pub(crate) fn vTaskDelay(ticks: u32);

    // Task-handoff primitives (the C `xTaskCreate`/`vTaskDelete` symbols).
    // Upstream FreeRTOS declares xTaskCreate as a real function; ESP-IDF's is a
    // static inline, so its real symbol is xTaskCreatePinnedToCore (below).
    #[cfg(not(target_arch = "riscv32"))]
    pub(crate) fn xTaskCreate(
        task: extern "C" fn(*mut c_void),
        name: *const u8,
        stack_depth: u32,
        arg: *mut c_void,
        priority: u32,
        created: *mut *mut c_void,
    ) -> i32;
    // ESP-IDF exposes xTaskCreate only as a static inline and provides the
    // pinned variant as the real symbol; upstream FreeRTOS (STM32F4) is the
    // other way around. task_create() picks the backend per target.
    #[cfg(target_arch = "riscv32")]
    pub(crate) fn xTaskCreatePinnedToCore(
        task: extern "C" fn(*mut c_void),
        name: *const u8,
        stack_depth: u32,
        arg: *mut c_void,
        priority: u32,
        created: *mut *mut c_void,
        core_id: i32,
    ) -> i32;
    pub(crate) fn vTaskDelete(task: *mut c_void);
    pub(crate) fn uxTaskPriorityGet(task: *mut c_void) -> u32;

    // Periodic jitter primitives.
    pub(crate) fn xTaskGetTickCount() -> u32;
    pub(crate) fn xTaskDelayUntil(previous_wake: *mut u32, time_increment: u32) -> i32;
    pub(crate) fn xTaskGetCurrentTaskHandle() -> *mut c_void;
    pub(crate) fn xQueueCreateCountingSemaphore(max_count: u32, initial_count: u32) -> *mut c_void;
    pub(crate) fn vTaskSuspendAll();
    pub(crate) fn xTaskResumeAll() -> i32;
    pub(crate) fn bench_rust_notify(task: *mut c_void, action: u32);
    pub(crate) fn bench_rust_notify_take(clear_on_exit: u32, ticks_to_wait: u32) -> u32;
    pub(crate) fn xTimerCreate(
        name: *const u8,
        period: u32,
        auto_reload: i32,
        timer_id: *mut c_void,
        callback: extern "C" fn(*mut c_void),
    ) -> *mut c_void;
    pub(crate) fn bench_rust_timer_start(timer: *mut c_void) -> i32;
    pub(crate) fn bench_rust_timer_delete(timer: *mut c_void) -> i32;
    pub(crate) fn bench_rust_tick_hz() -> u32;
    pub(crate) fn bench_rust_yield();
    pub(crate) fn bench_rust_nested_run(pend_h: u32) -> u32;
    pub(crate) fn bench_rust_isr_arm() -> i32;
    pub(crate) fn bench_rust_isr_wait() -> i32;
    pub(crate) fn bench_rust_isr_entry() -> u64;
    pub(crate) fn bench_rust_trig_drain() -> i32;
    pub(crate) fn bench_rust_trig_fire() -> u64;
    pub(crate) fn bench_rust_trig_wait() -> i32;
    pub(crate) fn bench_rust_trig_entry() -> u64;
    pub(crate) fn bench_rust_maxdis_begin() -> u64;
    pub(crate) fn bench_rust_maxdis_arm() -> i32;
    pub(crate) fn bench_rust_maxdis_wait(t0: u64) -> i32;
    pub(crate) fn bench_rust_maxdis_isr_ts() -> u64;
    pub(crate) fn bench_rust_pjit_wait() -> i32;
    pub(crate) fn bench_rust_pjit_read(ticks: *mut u64) -> i32;
    pub(crate) fn bench_rust_tick_take(ticks: *mut u64) -> i32;
    pub(crate) fn bench_rust_frag_probe(active: i32);
    pub(crate) fn bench_rust_frag_blocks() -> *mut *mut c_void;
    pub(crate) fn bench_rust_cpu_start(load_pct: u32) -> i32;
    pub(crate) fn bench_rust_cpu_arm();
    pub(crate) fn bench_rust_cpu_idle() -> u32;
    pub(crate) fn bench_rust_cpu_accum() -> u64;
    pub(crate) fn bench_rust_cpu_disarm();
    pub(crate) fn bench_rust_cpu_stop();
    pub(crate) fn xPortGetFreeHeapSize() -> usize;

    // The C firmware owns the controlled interrupt source so both languages
    // measure the same hardware/RTOS path. Returns a bench_status code.
    pub(crate) fn bench_isr_create() -> i32;
    pub(crate) fn bench_isr_destroy() -> i32;
    pub(crate) fn bench_trigger_isr_create() -> i32;
    pub(crate) fn bench_trigger_isr_destroy() -> i32;
    pub(crate) fn bench_pjit_create() -> i32;
    pub(crate) fn bench_pjit_set_load(load_pct: u32) -> i32;
    pub(crate) fn bench_pjit_destroy() -> i32;

    // Context-switch (case 5) and preemption (case 6) paths are owned by the C
    // firmware so both languages time the identical task paths.
    pub(crate) fn bench_yield_create() -> i32;
    pub(crate) fn bench_yield_now() -> i32;
    pub(crate) fn bench_yield_destroy() -> i32;
    pub(crate) fn bench_peers_set(count: u32) -> i32;
    pub(crate) fn bench_cpu_create() -> i32;
    pub(crate) fn bench_cpu_destroy() -> i32;
    pub(crate) fn bench_frag_create() -> i32;
    pub(crate) fn bench_frag_destroy() -> i32;

    // Cases 12-15 (uncontended sync / alloc / delay), C-owned so both languages
    // time the same FreeRTOS primitive.

    // Phase-2 cases 16/17/21/25/26. Implemented on the STM32F4 FreeRTOS board;
    // other FreeRTOS boards provide stubs returning Unsupported.
    pub(crate) fn bench_tick_create() -> i32;
    pub(crate) fn bench_tick_destroy() -> i32;
    pub(crate) fn bench_maxdis_create() -> i32;
    pub(crate) fn bench_maxdis_destroy() -> i32;
    pub(crate) fn bench_nested_create() -> i32;
    pub(crate) fn bench_nested_destroy() -> i32;
}

/// Peer-task stack size. Upstream FreeRTOS sizes `xTaskCreate` stacks in words,
/// so 256 is ~1 KiB; ESP-IDF sizes the same argument in bytes, where 256
/// overflows on the first call, so the RISC-V build uses a byte count.
#[cfg(target_arch = "riscv32")]
pub(crate) const PEER_STACK: u32 = 4096;
#[cfg(not(target_arch = "riscv32"))]
pub(crate) const PEER_STACK: u32 = 256;

/// Create the handoff peer task with the target's FreeRTOS task-create symbol.
///
/// # Safety
/// Same contract as the underlying FreeRTOS call: `task` must be a valid
/// entry point and `created` a valid handle pointer.
pub(crate) unsafe fn task_create(
    task: extern "C" fn(*mut c_void),
    name: &core::ffi::CStr,
    arg: usize,
    priority: u32,
) -> *mut c_void {
    let name = name.as_ptr() as *const u8;
    let arg = arg as *mut c_void;
    let mut created: *mut c_void = core::ptr::null_mut();
    #[cfg(target_arch = "riscv32")]
    {
        const TSK_NO_AFFINITY: i32 = 0x7FFF_FFFF;
        let _ = xTaskCreatePinnedToCore(
            task,
            name,
            PEER_STACK,
            arg,
            priority,
            &mut created,
            TSK_NO_AFFINITY,
        );
    }
    #[cfg(not(target_arch = "riscv32"))]
    {
        let _ = xTaskCreate(task, name, PEER_STACK, arg, priority, &mut created);
    }
    created
}

/// `queueQUEUE_TYPE_BASE`.
pub(crate) const QUEUE_TYPE_BASE: u8 = 0;
/// `queueQUEUE_TYPE_BINARY_SEMAPHORE` (a capacity-one queue created with the
/// shared type byte; `xQueueGenericCreate` leaves it empty either way).
pub(crate) const QUEUE_TYPE_BINARY_SEMAPHORE: u8 = 1;
/// `queueQUEUE_TYPE_MUTEX`. `xQueueCreateMutex` initialises the count to
/// available; `xQueueGenericCreate` alone would leave it empty and block a
/// `portMAX_DELAY` take.
pub(crate) const QUEUE_TYPE_MUTEX: u8 = 1;
/// `queueSEND_TO_BACK`.
pub(crate) const SEND_TO_BACK: u32 = 0;
/// `portMAX_DELAY`.
pub(crate) const PORT_MAX_DELAY: u32 = 0xFFFF_FFFF;
/// `eIncrement` notify action, passed to `xTaskNotify` as a `u32`.
pub(crate) const ENOTIFY_INCREMENT: u32 = 2;
/// One millisecond expressed in the port's tick base, computed per call so the
/// same adapter works for the STM32F4 DWT (168 MHz cycles) and the C6 esp_timer
/// (microseconds).
#[inline]
pub(crate) fn jitter_period_ticks(ticks_per_second: u64) -> u64 {
    ticks_per_second / 1000
}

/// Give a binary semaphore from a task context.
pub(crate) unsafe fn semaphore_give(semaphore: *mut c_void) -> i32 {
    xQueueGenericSend(semaphore, core::ptr::null(), 0, SEND_TO_BACK)
}

/// Create a binary semaphore (capacity one, zero-length items).
pub(crate) unsafe fn semaphore_create() -> *mut c_void {
    xQueueGenericCreate(1, 0, QUEUE_TYPE_BINARY_SEMAPHORE)
}

// Handoff task state. Accessed only from the benchmark task and its peer, which
// are serialized by the semaphores; the C session never touches these.
pub(crate) static mut S_SIGNAL: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_DONE: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PEER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_WAKE_TICKS: u64 = 0;
pub(crate) static mut S_JITTER_LAST: u32 = 0;
pub(crate) static mut S_JITTER_PREV: u64 = 0;

pub(crate) extern "C" fn peer_entry(_arg: *mut c_void) {
    unsafe {
        loop {
            if xQueueSemaphoreTake(S_SIGNAL, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            S_WAKE_TICKS = bench_rust_now_ticks();
            let _ = semaphore_give(S_DONE);
        }
    }
}

// Preempt latency (case 6): a peer one priority above blocks on a binary
// semaphore; the give preempts it, it timestamps and re-blocks.
pub(crate) static mut S_PREEMPT_SEM: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PREEMPT_TASK: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PREEMPT_WAKE: u64 = 0;
pub(crate) static mut S_PREEMPT_SEQ: u32 = 0;
pub(crate) static mut S_PREEMPT_READY: u32 = 0;

pub(crate) extern "C" fn preempt_peer_entry(_arg: *mut c_void) {
    unsafe {
        S_PREEMPT_READY = 1;
        loop {
            if xQueueSemaphoreTake(S_PREEMPT_SEM, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            S_PREEMPT_WAKE = bench_rust_now_ticks();
            S_PREEMPT_SEQ += 1;
        }
    }
}

// IPC ping-pong (case 7): a peer echoes a u32 request.
pub(crate) static mut S_IPC_REQ: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_IPC_REP: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_IPC_PEER: *mut c_void = core::ptr::null_mut();

pub(crate) extern "C" fn ipc_peer_entry(_arg: *mut c_void) {
    unsafe {
        let mut received_value: u32 = 0;
        loop {
            if xQueueReceive(
                S_IPC_REQ,
                &mut received_value as *mut u32 as *mut c_void,
                PORT_MAX_DELAY,
            ) == PD_TRUE
            {
                let _ = xQueueGenericSend(
                    S_IPC_REP,
                    &received_value as *const u32 as *const c_void,
                    PORT_MAX_DELAY,
                    SEND_TO_BACK,
                );
            }
        }
    }
}

// Direct-to-task notification (case 17).
pub(crate) static mut S_NOTIFY_GO: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_NOTIFY_TARGET: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_NOTIFY_PEER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_NOTIFY_SIGNAL: u64 = 0;

pub(crate) extern "C" fn notify_peer_entry(_arg: *mut c_void) {
    unsafe {
        loop {
            if xQueueSemaphoreTake(S_NOTIFY_GO, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            S_NOTIFY_SIGNAL = bench_rust_now_ticks();
            bench_rust_notify(S_NOTIFY_TARGET, ENOTIFY_INCREMENT);
        }
    }
}

// Software-timer callback (case 9).
pub(crate) static mut S_TIMER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_TIMER_SEM: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_TIMER_FIRE: u64 = 0;

pub(crate) extern "C" fn timer_cb(_t: *mut c_void) {
    unsafe {
        S_TIMER_FIRE = bench_rust_now_ticks();
        let _ = semaphore_give(S_TIMER_SEM);
    }
}

// IPC payload sweep (case 18): one request/reply queue pair per size.
pub(crate) const IPC_PAYLOAD_N: usize = 5;
pub(crate) const IPC_PAYLOAD_BYTES: [u32; IPC_PAYLOAD_N] = [1, 4, 16, 64, 256];
pub(crate) static mut S_IPC_PAYLOAD_REQ: [*mut c_void; IPC_PAYLOAD_N] =
    [core::ptr::null_mut(); IPC_PAYLOAD_N];
pub(crate) static mut S_IPC_PAYLOAD_REP: [*mut c_void; IPC_PAYLOAD_N] =
    [core::ptr::null_mut(); IPC_PAYLOAD_N];
pub(crate) static mut S_IPC_PAYLOAD_PEER: [*mut c_void; IPC_PAYLOAD_N] =
    [core::ptr::null_mut(); IPC_PAYLOAD_N];
pub(crate) static mut S_IPC_PAYLOAD_TX: [u8; 256] = [0; 256];
pub(crate) static mut S_IPC_PAYLOAD_RX: [u8; 256] = [0; 256];

pub(crate) extern "C" fn ipc_payload_peer_entry(arg: *mut c_void) {
    unsafe {
        let idx = arg as usize;
        let mut local = [0u8; 256];
        loop {
            if xQueueReceive(
                S_IPC_PAYLOAD_REQ[idx],
                local.as_mut_ptr() as *mut c_void,
                PORT_MAX_DELAY,
            ) == PD_TRUE
            {
                let _ = xQueueGenericSend(
                    S_IPC_PAYLOAD_REP[idx],
                    local.as_ptr() as *const c_void,
                    PORT_MAX_DELAY,
                    SEND_TO_BACK,
                );
            }
        }
    }
}

// IPC throughput (case 19): a higher-priority peer drains a bounded queue.
pub(crate) const TP_MSG_BYTES: u32 = 64;
pub(crate) const TP_MSGS: u32 = 16384;
pub(crate) const TP_QUEUE_DEPTH: u32 = 8;
pub(crate) static mut S_TP_Q: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_TP_DONE: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_TP_PEER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_TP_TX: [u8; 64] = [0; 64];
pub(crate) static mut S_TP_TARGET: u32 = 0;
pub(crate) static mut S_TP_SEEN: u32 = 0;
pub(crate) static mut S_TP_QUIT: i32 = 0;
pub(crate) static mut S_TP_ACTIVE: i32 = 0;

pub(crate) extern "C" fn tp_peer_entry(_arg: *mut c_void) {
    unsafe {
        let mut buf = [0u8; 64];
        loop {
            if xQueueReceive(S_TP_Q, buf.as_mut_ptr() as *mut c_void, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            if S_TP_QUIT != 0 {
                break;
            }
            S_TP_SEEN += 1;
            if S_TP_SEEN >= S_TP_TARGET {
                let _ = semaphore_give(S_TP_DONE);
            }
        }
    }
}

// Priority-inheritance mutex (case 7): H blocks on a mutex held by L while M spins.
pub(crate) static mut S_MUTEX: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_REQ: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_HELD: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_REL: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_GO: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_DONE: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_TASK: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_L_TASK: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_M_STOP: i32 = 0;
pub(crate) const M_SPIN_TICKS: u64 = 336_000; // ~2 ms at 168 MHz

pub(crate) extern "C" fn mutex_low_entry(_arg: *mut c_void) {
    unsafe {
        loop {
            if xQueueSemaphoreTake(S_M_REQ, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            if xQueueSemaphoreTake(S_MUTEX, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            let _ = semaphore_give(S_M_HELD);
            let _ = xQueueSemaphoreTake(S_M_REL, PORT_MAX_DELAY);
            let _ = semaphore_give(S_MUTEX);
        }
    }
}

pub(crate) extern "C" fn mutex_med_entry(_arg: *mut c_void) {
    unsafe {
        loop {
            if xQueueSemaphoreTake(S_M_GO, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            let start = bench_rust_now_ticks();
            while S_M_STOP == 0 && bench_rust_now_ticks().wrapping_sub(start) < M_SPIN_TICKS {}
            let _ = semaphore_give(S_M_DONE);
        }
    }
}

// Priority scheduling conformance (case 26): four peers at increasing priority.
pub(crate) const PRIO_N: usize = 4;
pub(crate) static mut S_PRIO_TASK: [*mut c_void; PRIO_N] = [core::ptr::null_mut(); PRIO_N];
pub(crate) static mut S_PRIO_RELEASE: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PRIO_ALL: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PRIO_ACK: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_PRIO_N: u32 = 0;
pub(crate) static mut S_PRIO_TRACE: [u8; PRIO_N] = [0; PRIO_N];

pub(crate) extern "C" fn prio_peer_entry(arg: *mut c_void) {
    unsafe {
        let id = arg as u8;
        loop {
            if xQueueSemaphoreTake(S_PRIO_RELEASE, PORT_MAX_DELAY) != PD_TRUE {
                continue;
            }
            if S_PRIO_N < PRIO_N as u32 {
                S_PRIO_TRACE[S_PRIO_N as usize] = id;
                S_PRIO_N += 1;
            }
            let _ = semaphore_give(S_PRIO_ALL);
            let _ = xQueueSemaphoreTake(S_PRIO_ACK, PORT_MAX_DELAY);
        }
    }
}

// Zero-copy IPC (case 27): a copied 256-byte leg and a pointer-reference leg.
pub(crate) const ZCOPY_BYTES: usize = 256;
pub(crate) static mut S_ZC_CP_REQ: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_CP_REP: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_ZC_REQ: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_ZC_REP: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_CP_PEER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_ZC_PEER: *mut c_void = core::ptr::null_mut();
pub(crate) static mut S_ZC_TX: [u8; 256] = [0; 256];
pub(crate) static mut S_ZC_RX: [u8; 256] = [0; 256];
pub(crate) static mut S_ZC_SHARED: [u8; 256] = [0; 256];

pub(crate) extern "C" fn zcopy_copy_peer_entry(_arg: *mut c_void) {
    unsafe {
        let mut local = [0u8; 256];
        loop {
            if xQueueReceive(
                S_ZC_CP_REQ,
                local.as_mut_ptr() as *mut c_void,
                PORT_MAX_DELAY,
            ) == PD_TRUE
            {
                let _ = xQueueGenericSend(
                    S_ZC_CP_REP,
                    local.as_ptr() as *const c_void,
                    PORT_MAX_DELAY,
                    SEND_TO_BACK,
                );
            }
        }
    }
}

pub(crate) extern "C" fn zcopy_ref_peer_entry(_arg: *mut c_void) {
    unsafe {
        let mut received_ptr: *mut u8 = core::ptr::null_mut();
        loop {
            if xQueueReceive(
                S_ZC_ZC_REQ,
                &mut received_ptr as *mut *mut u8 as *mut c_void,
                PORT_MAX_DELAY,
            ) == PD_TRUE
            {
                let _ = xQueueGenericSend(
                    S_ZC_ZC_REP,
                    &received_ptr as *const *mut u8 as *const c_void,
                    PORT_MAX_DELAY,
                    SEND_TO_BACK,
                );
            }
        }
    }
}

/// Send a u32 through a u32-item queue.
pub(crate) unsafe fn queue_send_u32(queue: *mut c_void, value: u32, ticks: u32) -> bool {
    let item = value;
    xQueueGenericSend(
        queue,
        &item as *const u32 as *const c_void,
        ticks,
        SEND_TO_BACK,
    ) == PD_TRUE
}

/// Receive a u32 from a u32-item queue.
pub(crate) unsafe fn queue_recv_u32(queue: *mut c_void, ticks: u32) -> Option<u32> {
    let mut value: u32 = 0;
    if xQueueReceive(queue, &mut value as *mut u32 as *mut c_void, ticks) == PD_TRUE {
        Some(value)
    } else {
        None
    }
}

/// Realign the jitter grid to the tick boundary.
pub(crate) unsafe fn jitter_align() {
    let _ = xTaskDelayUntil(core::ptr::addr_of_mut!(S_JITTER_LAST), 1);
}

/// Realign the jitter grid, then return the post-delay DWT timestamp.
pub(crate) unsafe fn jitter_step() -> u64 {
    let _ = xTaskDelayUntil(core::ptr::addr_of_mut!(S_JITTER_LAST), 1);
    bench_rust_now_ticks()
}

/// Store a task handle into the ipc-payload peer table.
pub(crate) unsafe fn ipc_payload_peer_set(idx: usize, handle: *mut c_void) {
    (core::ptr::addr_of_mut!(S_IPC_PAYLOAD_PEER) as *mut *mut c_void)
        .add(idx)
        .write(handle);
}

/// Store a task handle into the priority-scheduling peer table.
pub(crate) unsafe fn prio_task_set(idx: usize, handle: *mut c_void) {
    (core::ptr::addr_of_mut!(S_PRIO_TASK) as *mut *mut c_void)
        .add(idx)
        .write(handle);
}

/// Create the software timer used by the timer-callback case.
pub(crate) unsafe fn timer_create(period_ticks: u32) -> *mut c_void {
    xTimerCreate(
        c"bench_t".as_ptr() as *const u8,
        period_ticks,
        0,
        core::ptr::null_mut(),
        timer_cb,
    )
}

// ---- IPC ping-pong, payload sweep, throughput, zero-copy, priority trace ----

/// Fill the ipc-payload transmit buffer.
pub(crate) unsafe fn ipc_payload_fill() {
    let tx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_TX) as *mut u8;
    for i in 0..256 {
        *tx.add(i) = 0xA5u8 ^ (i as u8);
    }
}

/// Create the per-size request/reply queues; false on allocation failure.
pub(crate) unsafe fn ipc_payload_open() -> bool {
    for i in 0..IPC_PAYLOAD_N {
        S_IPC_PAYLOAD_REQ[i] = xQueueGenericCreate(1, IPC_PAYLOAD_BYTES[i], QUEUE_TYPE_BASE);
        S_IPC_PAYLOAD_REP[i] = xQueueGenericCreate(1, IPC_PAYLOAD_BYTES[i], QUEUE_TYPE_BASE);
        if S_IPC_PAYLOAD_REQ[i].is_null() || S_IPC_PAYLOAD_REP[i].is_null() {
            return false;
        }
    }
    true
}

/// Spawn the ipc-payload peer for one size; false on failure.
pub(crate) unsafe fn ipc_payload_spawn(idx: usize, priority: u32) -> bool {
    let handle = task_create(ipc_payload_peer_entry, c"bench_ipc_payload", idx, priority);
    ipc_payload_peer_set(idx, handle);
    !handle.is_null()
}

/// Send the fixed transmit buffer through the request queue `idx`.
pub(crate) unsafe fn ipc_payload_send(idx: usize) -> bool {
    xQueueGenericSend(
        S_IPC_PAYLOAD_REQ[idx],
        core::ptr::addr_of!(S_IPC_PAYLOAD_TX) as *const c_void,
        PORT_MAX_DELAY,
        SEND_TO_BACK,
    ) == PD_TRUE
}

/// Receive into the fixed receive buffer from the reply queue `idx`.
pub(crate) unsafe fn ipc_payload_recv(idx: usize) -> bool {
    xQueueReceive(
        S_IPC_PAYLOAD_REP[idx],
        core::ptr::addr_of_mut!(S_IPC_PAYLOAD_RX) as *mut c_void,
        PORT_MAX_DELAY,
    ) == PD_TRUE
}

/// Verify the first `payload_len` received bytes match the transmit buffer.
pub(crate) unsafe fn ipc_payload_verify(payload_len: usize) -> bool {
    let tx = core::ptr::addr_of!(S_IPC_PAYLOAD_TX) as *const u8;
    let rx = core::ptr::addr_of!(S_IPC_PAYLOAD_RX) as *const u8;
    for i in 0..payload_len {
        if *rx.add(i) != *tx.add(i) {
            return false;
        }
    }
    true
}

/// Tear down the ipc-payload peers and queues.
pub(crate) unsafe fn ipc_payload_close() {
    for i in 0..IPC_PAYLOAD_N {
        if !S_IPC_PAYLOAD_PEER[i].is_null() {
            vTaskDelete(S_IPC_PAYLOAD_PEER[i]);
            S_IPC_PAYLOAD_PEER[i] = core::ptr::null_mut();
        }
        if !S_IPC_PAYLOAD_REQ[i].is_null() {
            vQueueDelete(S_IPC_PAYLOAD_REQ[i]);
            S_IPC_PAYLOAD_REQ[i] = core::ptr::null_mut();
        }
        if !S_IPC_PAYLOAD_REP[i].is_null() {
            vQueueDelete(S_IPC_PAYLOAD_REP[i]);
            S_IPC_PAYLOAD_REP[i] = core::ptr::null_mut();
        }
    }
}

/// Fill the throughput transmit buffer.
pub(crate) unsafe fn tp_fill() {
    let tx = core::ptr::addr_of_mut!(S_TP_TX) as *mut u8;
    for i in 0..TP_MSG_BYTES as usize {
        *tx.add(i) = 0x5Au8 ^ (i as u8);
    }
}

/// Send one throughput message.
pub(crate) unsafe fn tp_send() -> bool {
    xQueueGenericSend(
        S_TP_Q,
        core::ptr::addr_of!(S_TP_TX) as *const c_void,
        PORT_MAX_DELAY,
        SEND_TO_BACK,
    ) == PD_TRUE
}

/// Fill the zero-copy transmit and shared buffers.
pub(crate) unsafe fn zc_fill() {
    let tx = core::ptr::addr_of_mut!(S_ZC_TX) as *mut u8;
    let sh = core::ptr::addr_of_mut!(S_ZC_SHARED) as *mut u8;
    for i in 0..ZCOPY_BYTES {
        let byte = 0x3Cu8 ^ (i as u8);
        *tx.add(i) = byte;
        *sh.add(i) = byte;
    }
}

/// Copied leg: send then receive the fixed buffers.
pub(crate) unsafe fn zc_cp_send() -> bool {
    xQueueGenericSend(
        S_ZC_CP_REQ,
        core::ptr::addr_of!(S_ZC_TX) as *const c_void,
        PORT_MAX_DELAY,
        SEND_TO_BACK,
    ) == PD_TRUE
}
pub(crate) unsafe fn zc_cp_recv() -> bool {
    xQueueReceive(
        S_ZC_CP_REP,
        core::ptr::addr_of_mut!(S_ZC_RX) as *mut c_void,
        PORT_MAX_DELAY,
    ) == PD_TRUE
}
pub(crate) unsafe fn zc_cp_verify() -> bool {
    let tx = core::ptr::addr_of!(S_ZC_TX) as *const u8;
    let rx = core::ptr::addr_of!(S_ZC_RX) as *const u8;
    for i in 0..ZCOPY_BYTES {
        if *rx.add(i) != *tx.add(i) {
            return false;
        }
    }
    true
}

/// Zero-copy leg: send the shared-buffer pointer and check it returns unchanged.
pub(crate) unsafe fn zc_zc_roundtrip() -> bool {
    let shared = core::ptr::addr_of_mut!(S_ZC_SHARED) as *mut u8;
    let refp: *mut u8 = shared;
    let mut back: *mut u8 = core::ptr::null_mut();
    if xQueueGenericSend(
        S_ZC_ZC_REQ,
        &refp as *const *mut u8 as *const c_void,
        PORT_MAX_DELAY,
        SEND_TO_BACK,
    ) != PD_TRUE
    {
        return false;
    }
    if xQueueReceive(
        S_ZC_ZC_REP,
        &mut back as *mut *mut u8 as *mut c_void,
        PORT_MAX_DELAY,
    ) != PD_TRUE
    {
        return false;
    }
    back == shared
}

/// Verify the shared buffer still matches the transmit buffer.
pub(crate) unsafe fn zc_shared_verify() -> bool {
    let tx = core::ptr::addr_of!(S_ZC_TX) as *const u8;
    let shared = core::ptr::addr_of!(S_ZC_SHARED) as *const u8;
    for i in 0..ZCOPY_BYTES {
        if *shared.add(i) != *tx.add(i) {
            return false;
        }
    }
    true
}

/// True when the recorded priority trace is in strict descending-priority order.
pub(crate) unsafe fn prio_trace_ok() -> bool {
    if S_PRIO_N != PRIO_N as u32 {
        return false;
    }
    let trace = core::ptr::addr_of!(S_PRIO_TRACE) as *const u8;
    for i in 0..PRIO_N {
        if *trace.add(i) != (PRIO_N as u8 - 1 - i as u8) {
            return false;
        }
    }
    true
}

/// Set the malloc-failed-hook probe latch.
pub(crate) unsafe fn frag_probe(active: i32) {
    bench_rust_frag_probe(active);
}
/// Free heap in bytes.
pub(crate) unsafe fn frag_free_heap() -> usize {
    xPortGetFreeHeapSize()
}
/// Allocate one 256-byte block into slot `idx`; false on failure.
pub(crate) unsafe fn frag_alloc(idx: usize) -> bool {
    let p = pvPortMalloc(256);
    if p.is_null() {
        return false;
    }
    *bench_rust_frag_blocks().add(idx) = p;
    true
}
/// Free slot `idx` if held.
pub(crate) unsafe fn frag_free(idx: usize) {
    let blocks = bench_rust_frag_blocks();
    let p = *blocks.add(idx);
    if !p.is_null() {
        vPortFree(p);
        *blocks.add(idx) = core::ptr::null_mut();
    }
}
/// Probe whether a `size_bytes`-byte allocation succeeds.
pub(crate) unsafe fn frag_try(size_bytes: usize) -> bool {
    let p = pvPortMalloc(size_bytes);
    if p.is_null() {
        false
    } else {
        vPortFree(p);
        true
    }
}
/// Tear down the priority-scheduling peers and semaphores.
pub(crate) unsafe fn prio_teardown() {
    let tasks = core::ptr::addr_of_mut!(S_PRIO_TASK) as *mut *mut c_void;
    for i in 0..PRIO_N {
        let t = tasks.add(i);
        if !(*t).is_null() {
            vTaskDelete(*t);
            *t = core::ptr::null_mut();
        }
    }
    if !S_PRIO_RELEASE.is_null() {
        vQueueDelete(S_PRIO_RELEASE);
        S_PRIO_RELEASE = core::ptr::null_mut();
    }
    if !S_PRIO_ALL.is_null() {
        vQueueDelete(S_PRIO_ALL);
        S_PRIO_ALL = core::ptr::null_mut();
    }
    if !S_PRIO_ACK.is_null() {
        vQueueDelete(S_PRIO_ACK);
        S_PRIO_ACK = core::ptr::null_mut();
    }
}
