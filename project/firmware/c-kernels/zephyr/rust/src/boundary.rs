//! Zephyr kernel-object FFI boundary.
//!
//! Native Rust calls into the ABI-stable `z_impl_k_*` kernel symbols (the
//! non-inline implementations behind Zephyr's `static inline` `k_*` wrappers).
//! The C port keeps only the timer/ISR register shims and the record/flush
//! bridges; the case control flow stays in `lib.rs`. Keeping the boundary here
//! makes the Rust-side unchecked work explicit and enumerable, the same way the
//! FreeRTOS and NuttX adapters' `boundary.rs` do.
//!
//! Zephyr v4.4.2, CONFIG_USERSPACE off, single core. Kernel-object mirrors are
//! sized/aligned with compile-time asserts so a config change cannot silently
//! corrupt the calls; the peer-thread stacks replicate `K_THREAD_STACK_DEFINE`
//! (an MPU-guarded, 64-byte-aligned object whose usable region follows a
//! 64-byte reserved guard).
#![allow(clippy::needless_range_loop)]

use core::ffi::{c_int, c_void};
use core::sync::atomic::{AtomicI32, AtomicU32, Ordering};

/// Zephyr `k_timeout_t`: `{ k_ticks_t ticks; }` with `k_ticks_t = int64_t`.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct KTimeout {
    pub ticks: i64,
}

/// `K_NO_WAIT` (poll, never block).
pub const K_NO_WAIT: KTimeout = KTimeout { ticks: 0 };
/// `K_FOREVER`.
#[allow(dead_code)]
pub const K_FOREVER: KTimeout = KTimeout { ticks: -1 };

/// `Z_TICK_ABS(t) = K_TICKS_FOREVER - 1 - t` (`K_TIMEOUT_ABS_TICKS`).
pub const fn abs_ticks(t: i64) -> KTimeout {
    KTimeout { ticks: -2 - t }
}

// Kernel-object opaque mirrors (sizes verified against Zephyr v4.4.2 on the
// STM32F4 config; align 8 where the struct embeds a `k_timeout_t`/`_timeout`).

/// `struct k_sem` opaque mirror: 16 B, align 4.
#[repr(C, align(4))]
#[derive(Clone, Copy)]
pub struct KSem {
    _data: [u8; 16],
}

/// `struct k_msgq` opaque mirror: 40 B, align 4.
#[repr(C, align(4))]
#[derive(Clone, Copy)]
pub struct KMsgq {
    _data: [u8; 40],
}

/// `struct k_mutex` opaque mirror: 20 B, align 4.
#[repr(C, align(4))]
#[derive(Clone, Copy)]
pub struct KMutex {
    _data: [u8; 20],
}

/// `struct k_timer` opaque mirror: 56 B, align 8.
#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub struct KTimer {
    _data: [u8; 56],
}

/// `struct k_thread` opaque mirror. The real size is config- and arch-dependent:
/// 128 B (ARM, no CONFIG_THREAD_MONITOR), 144 B (ARM, CONFIG_THREAD_MONITOR=y),
/// 232 B (RISC-V, larger `struct _callee_saved`). The mirror must be at least
/// the largest size for the target so `z_impl_k_thread_create` never overflows
/// a TCB into the next static; the C port asserts the real size stays under it.
#[cfg(target_arch = "arm")]
const KTHREAD_SIZE: usize = 144;
#[cfg(not(target_arch = "arm"))]
const KTHREAD_SIZE: usize = 256;

#[repr(C, align(8))]
#[derive(Clone, Copy)]
pub struct KThread {
    _data: [u8; KTHREAD_SIZE],
}

/// An MPU-guarded thread stack object: `K_THREAD_STACK_DEFINE` equivalent.
/// The usable region is `N - STACK_RESERVED` bytes at `base + STACK_RESERVED`.
#[repr(C, align(64))]
#[derive(Clone, Copy)]
pub struct ThreadStack<const N: usize> {
    pub data: [u8; N],
}

impl<const N: usize> ThreadStack<N> {
    pub const fn zeroed() -> Self {
        Self { data: [0u8; N] }
    }
}

impl KSem {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 16] }
    }
}
impl KMsgq {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 40] }
    }
}
impl KMutex {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 20] }
    }
}
impl KTimer {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 56] }
    }
}
impl KThread {
    pub const fn zeroed() -> Self {
        Self {
            _data: [0u8; KTHREAD_SIZE],
        }
    }
}

// Stack geometry (STM32F4, CONFIG_STACK_ALIGN_DOUBLE_WORD + MPU stack guard):
//   ARCH_STACK_PTR_ALIGN     = 8
//   K_KERNEL_STACK_RESERVED  = 64  (MPU_GUARD_ALIGN_AND_SIZE)
//   Z_KERNEL_STACK_OBJ_ALIGN = 64  (Z_MPU_GUARD_ALIGN)
const STACK_PTR_ALIGN: usize = 8;
const STACK_RESERVED: usize = 64;
const STACK_OBJ_ALIGN: usize = 64;

// Overflow-free ceil-division; `div_ceil` would compute `x + a - 1` first and
// overflow for large `x`, so keep the manual form.
#[allow(clippy::manual_div_ceil)]
const fn round_up(x: usize, a: usize) -> usize {
    (x + a - 1) / a * a
}

/// `K_KERNEL_STACK_LEN(size)`: the total object length for a `size` usable
/// stack, which `z_impl_k_thread_create` recomputes from the passed size.
pub const fn stack_obj_len(usable: usize) -> usize {
    round_up(
        round_up(usable, STACK_PTR_ALIGN) + STACK_RESERVED,
        STACK_OBJ_ALIGN,
    )
}

const _: () = {
    assert!(core::mem::size_of::<KTimeout>() == 8);
    assert!(core::mem::align_of::<KTimeout>() == 8);
    assert!(core::mem::size_of::<KSem>() == 16);
    assert!(core::mem::align_of::<KSem>() == 4);
    assert!(core::mem::size_of::<KMsgq>() == 40);
    assert!(core::mem::align_of::<KMsgq>() == 4);
    assert!(core::mem::size_of::<KMutex>() == 20);
    assert!(core::mem::align_of::<KMutex>() == 4);
    assert!(core::mem::size_of::<KTimer>() == 56);
    assert!(core::mem::align_of::<KTimer>() == 8);
    assert!(KTHREAD_SIZE >= 144);
    assert!(core::mem::size_of::<KThread>() == KTHREAD_SIZE);
    assert!(core::mem::align_of::<KThread>() == 8);
};

// Core kernel calls. `now_ticks`/`ticks_per_second` stay in the C port (the
// cycle counter is board-specific); `coop_prio`/`ms_to_ticks` are config
// macro pass-throughs.
extern "C" {
    fn bench_rust_now_ticks() -> u64;
    fn bench_rust_ticks_per_second() -> u64;
    fn bench_rust_coop_prio(x: c_int) -> c_int;
    fn bench_rust_ms_to_ticks(ms: u32) -> i64;

    fn z_impl_k_sem_init(sem: *mut KSem, initial_count: u32, limit: u32) -> c_int;
    fn z_impl_k_sem_give(sem: *mut KSem);
    fn z_impl_k_sem_take(sem: *mut KSem, timeout: KTimeout) -> c_int;

    fn k_msgq_init(msgq: *mut KMsgq, buffer: *mut c_void, msg_size: usize, max_msgs: u32);
    fn z_impl_k_msgq_put(msgq: *mut KMsgq, data: *const c_void, timeout: KTimeout) -> c_int;
    fn z_impl_k_msgq_get(msgq: *mut KMsgq, data: *mut c_void, timeout: KTimeout) -> c_int;

    fn z_impl_k_mutex_init(mutex: *mut KMutex);
    fn z_impl_k_mutex_lock(mutex: *mut KMutex, timeout: KTimeout) -> c_int;
    fn z_impl_k_mutex_unlock(mutex: *mut KMutex) -> c_int;

    fn k_timer_init(
        timer: *mut KTimer,
        expiry: Option<extern "C" fn(*mut KTimer)>,
        stop: Option<extern "C" fn(*mut KTimer)>,
    );
    fn z_impl_k_timer_start(timer: *mut KTimer, duration: KTimeout, period: KTimeout);
    fn z_impl_k_timer_stop(timer: *mut KTimer);

    fn z_impl_k_thread_create(
        new_thread: *mut KThread,
        stack: *mut c_void,
        stack_size: usize,
        entry: extern "C" fn(*mut c_void, *mut c_void, *mut c_void),
        p1: *mut c_void,
        p2: *mut c_void,
        p3: *mut c_void,
        prio: c_int,
        options: u32,
        delay: KTimeout,
    ) -> *mut KThread;
    fn z_impl_k_thread_join(thread: *mut KThread, timeout: KTimeout) -> c_int;
    fn z_impl_k_thread_priority_get(thread: *mut KThread) -> c_int;
    fn z_impl_k_sched_current_thread_query() -> *mut KThread;
    fn z_impl_k_sleep(timeout: KTimeout) -> i32;
    fn z_impl_k_yield();
    fn z_impl_k_uptime_ticks() -> i64;

    fn k_malloc(size: usize) -> *mut c_void;
    fn k_free(ptr: *mut c_void);
}

/// A `k_msgq` message buffer: word-aligned like the C port's `__aligned(4)`.
#[repr(C, align(4))]
#[derive(Clone, Copy)]
pub struct MsgBuf<const N: usize> {
    pub data: [u8; N],
}

impl<const N: usize> MsgBuf<N> {
    pub const fn zeroed() -> Self {
        Self { data: [0u8; N] }
    }
}

// ---------------------------------------------------------------- queue (1)

static mut S_QUEUE: KMsgq = KMsgq::zeroed();
static mut S_QUEUE_BUF: MsgBuf<4> = MsgBuf::zeroed();

pub fn queue_create() -> Result<(), ()> {
    unsafe {
        k_msgq_init(
            core::ptr::addr_of_mut!(S_QUEUE),
            core::ptr::addr_of_mut!(S_QUEUE_BUF).cast::<c_void>(),
            4,
            1,
        );
        Ok(())
    }
}

pub fn queue_send(value: u32) -> Result<(), ()> {
    unsafe {
        let payload = value;
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_QUEUE),
            &payload as *const u32 as *const c_void,
            K_NO_WAIT,
        ) != 0
        {
            return Err(());
        }
        Ok(())
    }
}

pub fn queue_receive() -> Result<u32, ()> {
    unsafe {
        let mut received_value = 0u32;
        if z_impl_k_msgq_get(
            core::ptr::addr_of_mut!(S_QUEUE),
            &mut received_value as *mut u32 as *mut c_void,
            K_NO_WAIT,
        ) != 0
        {
            return Err(());
        }
        Ok(received_value)
    }
}

// ----------------------------------------------------------- semaphore (12)

pub fn sem_create(sem: &mut KSem) -> Result<(), ()> {
    sem_create_limit(sem, 1)
}

/// Initialize a counting semaphore with `limit` (the prio case needs 4).
pub fn sem_create_limit(sem: &mut KSem, limit: u32) -> Result<(), ()> {
    unsafe {
        if z_impl_k_sem_init(sem as *mut KSem, 0, limit) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn sem_give(sem: &mut KSem) {
    unsafe {
        z_impl_k_sem_give(sem as *mut KSem);
    }
}

pub fn sem_take(sem: &mut KSem) -> Result<(), ()> {
    unsafe {
        if z_impl_k_sem_take(sem as *mut KSem, K_NO_WAIT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

/// Blocking take (`K_FOREVER`) for the peer synchronisation used by handoff,
/// preempt, notify, prio and mutex-PI.
pub fn sem_take_forever(sem: &mut KSem) -> Result<(), ()> {
    unsafe {
        if z_impl_k_sem_take(sem as *mut KSem, K_FOREVER) != 0 {
            return Err(());
        }
        Ok(())
    }
}

/// Nonblocking take used to drain a stale semaphore token; Ok when drained.
pub fn sem_trytake(sem: &mut KSem) -> Result<(), ()> {
    unsafe {
        if z_impl_k_sem_take(sem as *mut KSem, K_NO_WAIT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

// ------------------------------------------------------ uncontended mutex (13)

static mut S_MUTEX_U: KMutex = KMutex::zeroed();

pub fn mutex_u_create() -> Result<(), ()> {
    unsafe {
        z_impl_k_mutex_init(core::ptr::addr_of_mut!(S_MUTEX_U));
        Ok(())
    }
}

pub fn mutex_u_lock() -> Result<(), ()> {
    unsafe {
        if z_impl_k_mutex_lock(core::ptr::addr_of_mut!(S_MUTEX_U), K_FOREVER) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_u_unlock() -> Result<(), ()> {
    unsafe {
        if z_impl_k_mutex_unlock(core::ptr::addr_of_mut!(S_MUTEX_U)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

// -------------------------------------------------------------- alloc (14)

pub fn kalloc(size_bytes: usize) -> *mut c_void {
    unsafe { k_malloc(size_bytes) }
}

pub fn kfree(p: *mut c_void) {
    unsafe { k_free(p) }
}

// -------------------------------------------------------------- sleep (15)

// --------------------------------------------------------------- sched (16)

pub fn sched_yield() {
    unsafe {
        z_impl_k_yield();
    }
}

// ---------------------------------------------------------------- threads

/// Current thread priority (`k_thread_priority_get(k_current_get())`).
pub fn current_priority() -> c_int {
    unsafe { z_impl_k_thread_priority_get(z_impl_k_sched_current_thread_query()) }
}

/// `K_PRIO_COOP(x)`: cooperative priority `x` (1 = highest coop).
pub fn coop_prio(x: c_int) -> c_int {
    unsafe { bench_rust_coop_prio(x) }
}

/// Create a peer thread at `prio` on `stack` (the whole stack object).
pub fn thread_create(
    thread: &mut KThread,
    stack_base: *mut c_void,
    usable: usize,
    entry: extern "C" fn(*mut c_void, *mut c_void, *mut c_void),
    p1: *mut c_void,
    prio: c_int,
) -> Result<(), ()> {
    unsafe {
        z_impl_k_thread_create(
            thread as *mut KThread,
            stack_base,
            usable,
            entry,
            p1,
            core::ptr::null_mut(),
            core::ptr::null_mut(),
            prio,
            0,
            K_NO_WAIT,
        );
        Ok(())
    }
}

pub fn thread_join(thread: &mut KThread) {
    unsafe {
        z_impl_k_thread_join(thread as *mut KThread, K_FOREVER);
    }
}

// ------------------------------------------------------------ handoff (2)

static mut S_HANDOFF_SIGNAL: KSem = KSem::zeroed();
static mut S_HANDOFF_DONE: KSem = KSem::zeroed();
static mut S_HANDOFF_THREAD: KThread = KThread::zeroed();
static mut S_HANDOFF_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_HANDOFF_WAKE: u64 = 0;
static mut S_HANDOFF_QUIT: c_int = 0;

extern "C" fn handoff_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_HANDOFF_SIGNAL));
            if *core::ptr::addr_of_mut!(S_HANDOFF_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_HANDOFF_WAKE) = bench_rust_now_ticks();
            sem_give(&mut *core::ptr::addr_of_mut!(S_HANDOFF_DONE));
        }
    }
}

pub fn handoff_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_HANDOFF_QUIT) = 0;
        sem_create(&mut *core::ptr::addr_of_mut!(S_HANDOFF_SIGNAL))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_HANDOFF_DONE))?;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_HANDOFF_THREAD),
            core::ptr::addr_of_mut!(S_HANDOFF_STACK) as *mut c_void,
            2048,
            handoff_peer_entry,
            core::ptr::null_mut(),
            coop_prio(1),
        )?;
        Ok(())
    }
}

/// Signal the peer and return the tick at which it unblocked.
pub fn handoff_measure() -> Result<u64, ()> {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_HANDOFF_SIGNAL));
        sem_take_forever(&mut *core::ptr::addr_of_mut!(S_HANDOFF_DONE))?;
        Ok(*core::ptr::addr_of_mut!(S_HANDOFF_WAKE))
    }
}

pub fn handoff_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_HANDOFF_QUIT) = 1;
        sem_give(&mut *core::ptr::addr_of_mut!(S_HANDOFF_SIGNAL));
        thread_join(&mut *core::ptr::addr_of_mut!(S_HANDOFF_THREAD));
    }
}

// -------------------------------------------------------------- jitter (3)

/// Absolute-sleep one tick, matching `k_sleep(K_TIMEOUT_ABS_TICKS(target))`.
pub fn sleep_abs_ticks(target: i64) {
    unsafe {
        z_impl_k_sleep(abs_ticks(target));
    }
}

pub fn uptime_ticks() -> i64 {
    unsafe { z_impl_k_uptime_ticks() }
}

// --------------------------------------------------------------- yield (5)

static mut S_YIELD_THREAD: KThread = KThread::zeroed();
static mut S_YIELD_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
// Cross-thread flags are atomics (the C port's `volatile`): the peer writes
// these, the benchmark thread reads them, with only a `k_yield` between them.
static S_YIELD_PEER_RUNS: AtomicU32 = AtomicU32::new(0);
static S_YIELD_PEER_STARTED: AtomicU32 = AtomicU32::new(0);
static S_YIELD_QUIT: AtomicI32 = AtomicI32::new(0);

const PEERS_MAX_EXTRA: usize = 32;
static mut S_PEERS_THREADS: [KThread; PEERS_MAX_EXTRA] = [KThread::zeroed(); PEERS_MAX_EXTRA];
static mut S_PEERS_STACKS: [ThreadStack<{ stack_obj_len(512) }>; PEERS_MAX_EXTRA] =
    [ThreadStack::zeroed(); PEERS_MAX_EXTRA];
static S_PEERS_QUIT: [AtomicI32; PEERS_MAX_EXTRA] = [const { AtomicI32::new(0) }; PEERS_MAX_EXTRA];
static mut S_PEERS_COUNT: u32 = 0;

extern "C" fn yield_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    S_YIELD_PEER_STARTED.store(1, Ordering::Relaxed);
    while S_YIELD_QUIT.load(Ordering::Relaxed) == 0 {
        S_YIELD_PEER_RUNS.fetch_add(1, Ordering::Relaxed);
        unsafe {
            z_impl_k_yield();
        }
    }
}

extern "C" fn peers_extra_entry(arg: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    let idx = arg as usize;
    while S_PEERS_QUIT[idx].load(Ordering::Relaxed) == 0 {
        unsafe {
            z_impl_k_yield();
        }
    }
}

pub fn peers_set(count: u32) -> Result<(), ()> {
    unsafe {
        let want = if count > 0 { count - 1 } else { 0 };
        if want > PEERS_MAX_EXTRA as u32 {
            return Err(());
        }
        let prio = current_priority();
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) > want {
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) -= 1;
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            S_PEERS_QUIT[i].store(1, Ordering::Relaxed);
            z_impl_k_yield();
            thread_join(&mut *core::ptr::addr_of_mut!(S_PEERS_THREADS[i]));
        }
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) < want {
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            S_PEERS_QUIT[i].store(0, Ordering::Relaxed);
            thread_create(
                &mut *core::ptr::addr_of_mut!(S_PEERS_THREADS[i]),
                core::ptr::addr_of_mut!(S_PEERS_STACKS[i]) as *mut c_void,
                512,
                peers_extra_entry,
                i as *mut c_void,
                prio,
            )?;
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) += 1;
        }
        Ok(())
    }
}

pub fn yield_create() -> Result<(), ()> {
    unsafe {
        S_YIELD_QUIT.store(0, Ordering::Relaxed);
        S_YIELD_PEER_RUNS.store(0, Ordering::Relaxed);
        S_YIELD_PEER_STARTED.store(0, Ordering::Relaxed);
        let prio = current_priority();
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_YIELD_THREAD),
            core::ptr::addr_of_mut!(S_YIELD_STACK) as *mut c_void,
            2048,
            yield_peer_entry,
            core::ptr::null_mut(),
            prio,
        )?;
        for _ in 0..1000 {
            if S_YIELD_PEER_STARTED.load(Ordering::Relaxed) != 0 {
                break;
            }
            z_impl_k_yield();
        }
        if S_YIELD_PEER_STARTED.load(Ordering::Relaxed) == 0 {
            return Err(());
        }
        Ok(())
    }
}

/// One scheduler round trip to the equal-priority peer and back.
pub fn yield_now() -> Result<(), ()> {
    if S_YIELD_PEER_STARTED.load(Ordering::Relaxed) == 0 {
        return Err(());
    }
    let before = S_YIELD_PEER_RUNS.load(Ordering::Relaxed);
    for _ in 0..64 {
        unsafe {
            z_impl_k_yield();
        }
        if S_YIELD_PEER_RUNS.load(Ordering::Relaxed) != before {
            return Ok(());
        }
    }
    Err(())
}

pub fn yield_destroy() {
    unsafe {
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) > 0 {
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) -= 1;
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            S_PEERS_QUIT[i].store(1, Ordering::Relaxed);
            z_impl_k_yield();
            thread_join(&mut *core::ptr::addr_of_mut!(S_PEERS_THREADS[i]));
        }
        if S_YIELD_PEER_STARTED.load(Ordering::Relaxed) != 0 {
            S_YIELD_QUIT.store(1, Ordering::Relaxed);
            z_impl_k_yield();
            thread_join(&mut *core::ptr::addr_of_mut!(S_YIELD_THREAD));
            S_YIELD_PEER_STARTED.store(0, Ordering::Relaxed);
        }
    }
}

// ------------------------------------------------------------- preempt (6)

static mut S_PREEMPT_SEM: KSem = KSem::zeroed();
static mut S_PREEMPT_THREAD: KThread = KThread::zeroed();
static mut S_PREEMPT_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_PREEMPT_WAKE: u64 = 0;
static mut S_PREEMPT_SEQ: u32 = 0;
static mut S_PREEMPT_READY: u32 = 0;
static mut S_PREEMPT_QUIT: c_int = 0;

extern "C" fn preempt_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        *core::ptr::addr_of_mut!(S_PREEMPT_READY) = 1;
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_PREEMPT_SEM));
            if *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) = bench_rust_now_ticks();
            *core::ptr::addr_of_mut!(S_PREEMPT_SEQ) += 1;
        }
    }
}

pub fn preempt_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_SEQ) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_READY) = 0;
        sem_create(&mut *core::ptr::addr_of_mut!(S_PREEMPT_SEM))?;
        let prio = current_priority() - 1;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_PREEMPT_THREAD),
            core::ptr::addr_of_mut!(S_PREEMPT_STACK) as *mut c_void,
            2048,
            preempt_peer_entry,
            core::ptr::null_mut(),
            prio,
        )?;
        for _ in 0..1000 {
            if *core::ptr::addr_of_mut!(S_PREEMPT_READY) != 0 {
                break;
            }
            z_impl_k_yield();
        }
        if *core::ptr::addr_of_mut!(S_PREEMPT_READY) == 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn preempt_ready() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_PREEMPT_READY) }
}

pub fn preempt_seq() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_PREEMPT_SEQ) }
}

pub fn preempt_post() -> Result<(), ()> {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_PREEMPT_SEM));
        Ok(())
    }
}

pub fn preempt_wake() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) }
}

pub fn preempt_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_PREEMPT_READY) != 0 {
            *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) = 1;
            sem_give(&mut *core::ptr::addr_of_mut!(S_PREEMPT_SEM));
            thread_join(&mut *core::ptr::addr_of_mut!(S_PREEMPT_THREAD));
            *core::ptr::addr_of_mut!(S_PREEMPT_READY) = 0;
        }
    }
}

// --------------------------------------------------------------- ipc (7)

static mut S_IPC_REQ: KMsgq = KMsgq::zeroed();
static mut S_IPC_REP: KMsgq = KMsgq::zeroed();
static mut S_IPC_REQ_BUF: MsgBuf<4> = MsgBuf::zeroed();
static mut S_IPC_REP_BUF: MsgBuf<4> = MsgBuf::zeroed();
static mut S_IPC_THREAD: KThread = KThread::zeroed();
static mut S_IPC_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_IPC_QUIT: c_int = 0;
static mut S_IPC_ACTIVE: c_int = 0;

extern "C" fn ipc_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        loop {
            let mut received_value = 0u32;
            let _ = z_impl_k_msgq_get(
                core::ptr::addr_of_mut!(S_IPC_REQ),
                &mut received_value as *mut u32 as *mut c_void,
                K_FOREVER,
            );
            if *core::ptr::addr_of_mut!(S_IPC_QUIT) != 0 {
                break;
            }
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_IPC_REP),
                &received_value as *const u32 as *const c_void,
                K_FOREVER,
            );
        }
    }
}

pub fn ipc_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_IPC_QUIT) = 0;
        k_msgq_init(
            core::ptr::addr_of_mut!(S_IPC_REQ),
            core::ptr::addr_of_mut!(S_IPC_REQ_BUF).cast::<c_void>(),
            4,
            1,
        );
        k_msgq_init(
            core::ptr::addr_of_mut!(S_IPC_REP),
            core::ptr::addr_of_mut!(S_IPC_REP_BUF).cast::<c_void>(),
            4,
            1,
        );
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_IPC_THREAD),
            core::ptr::addr_of_mut!(S_IPC_STACK) as *mut c_void,
            2048,
            ipc_peer_entry,
            core::ptr::null_mut(),
            coop_prio(1),
        )?;
        *core::ptr::addr_of_mut!(S_IPC_ACTIVE) = 1;
        Ok(())
    }
}

pub fn ipc_roundtrip(value: u32) -> Result<u32, ()> {
    unsafe {
        let payload = value;
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_IPC_REQ),
            &payload as *const u32 as *const c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        let mut received_value = 0u32;
        if z_impl_k_msgq_get(
            core::ptr::addr_of_mut!(S_IPC_REP),
            &mut received_value as *mut u32 as *mut c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        if received_value != value {
            return Err(());
        }
        Ok(received_value)
    }
}

pub fn ipc_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_IPC_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_IPC_QUIT) = 1;
            let sentinel = 0u32;
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_IPC_REQ),
                &sentinel as *const u32 as *const c_void,
                K_FOREVER,
            );
            thread_join(&mut *core::ptr::addr_of_mut!(S_IPC_THREAD));
            *core::ptr::addr_of_mut!(S_IPC_ACTIVE) = 0;
        }
    }
}

// ----------------------------------------------------- ipc payload sweep (18)

const IPC_PAYLOAD_N: usize = 5;
const IPC_PAYLOAD_MAX: usize = 256;
static mut S_IPC_PAYLOAD_REQ: [KMsgq; IPC_PAYLOAD_N] = [KMsgq::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_REP: [KMsgq; IPC_PAYLOAD_N] = [KMsgq::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_REQ_BUF: [MsgBuf<IPC_PAYLOAD_MAX>; IPC_PAYLOAD_N] =
    [MsgBuf::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_REP_BUF: [MsgBuf<IPC_PAYLOAD_MAX>; IPC_PAYLOAD_N] =
    [MsgBuf::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_THREADS: [KThread; IPC_PAYLOAD_N] = [KThread::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_STACKS: [ThreadStack<{ stack_obj_len(2048) }>; IPC_PAYLOAD_N] =
    [ThreadStack::zeroed(); IPC_PAYLOAD_N];
static mut S_IPC_PAYLOAD_TX: [u8; IPC_PAYLOAD_MAX] = [0u8; IPC_PAYLOAD_MAX];
static mut S_IPC_PAYLOAD_RX: [u8; IPC_PAYLOAD_MAX] = [0u8; IPC_PAYLOAD_MAX];
static mut S_IPC_PAYLOAD_QUIT: c_int = 0;
static mut S_IPC_PAYLOAD_ACTIVE: c_int = 0;

extern "C" fn ipc_payload_peer_entry(arg: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        let idx = arg as usize;
        let mut buf = [0u8; IPC_PAYLOAD_MAX];
        loop {
            let _ = z_impl_k_msgq_get(
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ[idx]),
                buf.as_mut_ptr() as *mut c_void,
                K_FOREVER,
            );
            if *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) != 0 {
                break;
            }
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP[idx]),
                buf.as_ptr() as *const c_void,
                K_FOREVER,
            );
        }
    }
}

fn ipc_payload_index(payload_bytes: u32) -> Option<usize> {
    const SIZES: [u32; IPC_PAYLOAD_N] = [1, 4, 16, 64, 256];
    SIZES.iter().position(|&s| s == payload_bytes)
}

pub fn ipc_payload_create() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_TX);
        for i in 0..IPC_PAYLOAD_MAX {
            (*tx)[i] = 0xA5u8 ^ (i as u8);
        }
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) = 0;
        const SIZES: [u32; IPC_PAYLOAD_N] = [1, 4, 16, 64, 256];
        for i in 0..IPC_PAYLOAD_N {
            k_msgq_init(
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ[i]),
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ_BUF[i]).cast::<c_void>(),
                SIZES[i] as usize,
                1,
            );
            k_msgq_init(
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP[i]),
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP_BUF[i]).cast::<c_void>(),
                SIZES[i] as usize,
                1,
            );
            thread_create(
                &mut *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_THREADS[i]),
                core::ptr::addr_of_mut!(S_IPC_PAYLOAD_STACKS[i]) as *mut c_void,
                2048,
                ipc_payload_peer_entry,
                i as *mut c_void,
                coop_prio(1),
            )?;
        }
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_ACTIVE) = 1;
        Ok(())
    }
}

pub fn ipc_payload_roundtrip(payload_bytes: u32) -> Result<(), ()> {
    unsafe {
        let idx = ipc_payload_index(payload_bytes).ok_or(())?;
        let tx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_TX);
        let rx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_RX);
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ[idx]),
            (*tx).as_ptr() as *const c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        if z_impl_k_msgq_get(
            core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP[idx]),
            (*rx).as_mut_ptr() as *mut c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        Ok(())
    }
}

pub fn ipc_payload_verify(payload_bytes: u32) -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_TX);
        let rx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_RX);
        for i in 0..payload_bytes as usize {
            if (*rx)[i] != (*tx)[i] {
                return Err(());
            }
        }
        Ok(())
    }
}

pub fn ipc_payload_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) = 1;
            let tx = core::ptr::addr_of_mut!(S_IPC_PAYLOAD_TX);
            for i in 0..IPC_PAYLOAD_N {
                let _ = z_impl_k_msgq_put(
                    core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ[i]),
                    (*tx).as_ptr() as *const c_void,
                    K_FOREVER,
                );
                thread_join(&mut *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_THREADS[i]));
            }
            *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_ACTIVE) = 0;
        }
    }
}

// ------------------------------------------------------ ipc throughput (19)

const TP_MSG_BYTES: usize = 64;
const TP_QUEUE_DEPTH: usize = 8;
static mut S_TP_Q: KMsgq = KMsgq::zeroed();
static mut S_TP_Q_BUF: MsgBuf<{ TP_MSG_BYTES * TP_QUEUE_DEPTH }> = MsgBuf::zeroed();
static mut S_TP_DONE: KSem = KSem::zeroed();
static mut S_TP_THREAD: KThread = KThread::zeroed();
static mut S_TP_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_TP_TX: [u8; TP_MSG_BYTES] = [0u8; TP_MSG_BYTES];
static mut S_TP_TARGET: u32 = 0;
static mut S_TP_SEEN: u32 = 0;
static mut S_TP_QUIT: c_int = 0;
static mut S_TP_ACTIVE: c_int = 0;

extern "C" fn tp_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        let mut buf = [0u8; TP_MSG_BYTES];
        loop {
            if z_impl_k_msgq_get(
                core::ptr::addr_of_mut!(S_TP_Q),
                buf.as_mut_ptr() as *mut c_void,
                K_FOREVER,
            ) != 0
            {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_TP_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_TP_SEEN) += 1;
            if *core::ptr::addr_of_mut!(S_TP_SEEN) >= *core::ptr::addr_of_mut!(S_TP_TARGET) {
                sem_give(&mut *core::ptr::addr_of_mut!(S_TP_DONE));
            }
        }
    }
}

pub fn tp_create() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_TP_TX);
        for i in 0..TP_MSG_BYTES {
            (*tx)[i] = 0x5Au8 ^ (i as u8);
        }
        *core::ptr::addr_of_mut!(S_TP_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_TP_SEEN) = 0;
        *core::ptr::addr_of_mut!(S_TP_TARGET) = 0;
        k_msgq_init(
            core::ptr::addr_of_mut!(S_TP_Q),
            core::ptr::addr_of_mut!(S_TP_Q_BUF).cast::<c_void>(),
            TP_MSG_BYTES,
            TP_QUEUE_DEPTH as u32,
        );
        sem_create(&mut *core::ptr::addr_of_mut!(S_TP_DONE))?;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_TP_THREAD),
            core::ptr::addr_of_mut!(S_TP_STACK) as *mut c_void,
            2048,
            tp_peer_entry,
            core::ptr::null_mut(),
            coop_prio(1),
        )?;
        *core::ptr::addr_of_mut!(S_TP_ACTIVE) = 1;
        Ok(())
    }
}

pub fn tp_start(target: u32) -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_TP_SEEN) = 0;
        *core::ptr::addr_of_mut!(S_TP_TARGET) = target;
        Ok(())
    }
}

pub fn tp_send() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_TP_TX);
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_TP_Q),
            (*tx).as_ptr() as *const c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        Ok(())
    }
}

pub fn tp_done() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_TP_DONE)) }
}

pub fn tp_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_TP_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_TP_QUIT) = 1;
            let tx = core::ptr::addr_of_mut!(S_TP_TX);
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_TP_Q),
                (*tx).as_ptr() as *const c_void,
                K_FOREVER,
            );
            thread_join(&mut *core::ptr::addr_of_mut!(S_TP_THREAD));
            *core::ptr::addr_of_mut!(S_TP_ACTIVE) = 0;
        }
    }
}

// --------------------------------------------------------- zero-copy (27)

const ZCOPY_BYTES: usize = 256;
static mut S_ZCOPY_CP_REQ: KMsgq = KMsgq::zeroed();
static mut S_ZCOPY_CP_REP: KMsgq = KMsgq::zeroed();
static mut S_ZCOPY_ZC_REQ: KMsgq = KMsgq::zeroed();
static mut S_ZCOPY_ZC_REP: KMsgq = KMsgq::zeroed();
static mut S_ZCOPY_CP_REQ_BUF: MsgBuf<ZCOPY_BYTES> = MsgBuf::zeroed();
static mut S_ZCOPY_CP_REP_BUF: MsgBuf<ZCOPY_BYTES> = MsgBuf::zeroed();
static mut S_ZCOPY_ZC_REQ_BUF: MsgBuf<4> = MsgBuf::zeroed();
static mut S_ZCOPY_ZC_REP_BUF: MsgBuf<4> = MsgBuf::zeroed();
static mut S_ZCOPY_CP_THREAD: KThread = KThread::zeroed();
static mut S_ZCOPY_ZC_THREAD: KThread = KThread::zeroed();
static mut S_ZCOPY_CP_STACK: ThreadStack<{ stack_obj_len(1024) }> = ThreadStack::zeroed();
static mut S_ZCOPY_ZC_STACK: ThreadStack<{ stack_obj_len(1024) }> = ThreadStack::zeroed();
static mut S_ZCOPY_TX: [u8; ZCOPY_BYTES] = [0u8; ZCOPY_BYTES];
static mut S_ZCOPY_RX: [u8; ZCOPY_BYTES] = [0u8; ZCOPY_BYTES];
static mut S_ZCOPY_SHARED: [u8; ZCOPY_BYTES] = [0u8; ZCOPY_BYTES];
static mut S_ZCOPY_QUIT: c_int = 0;
static mut S_ZCOPY_ACTIVE: c_int = 0;

extern "C" fn zcopy_copy_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        let mut buf = [0u8; ZCOPY_BYTES];
        loop {
            if z_impl_k_msgq_get(
                core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
                buf.as_mut_ptr() as *mut c_void,
                K_FOREVER,
            ) != 0
            {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) != 0 {
                break;
            }
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
                buf.as_ptr() as *const c_void,
                K_FOREVER,
            );
        }
    }
}

extern "C" fn zcopy_ref_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        let mut refb = [0u8; 4];
        loop {
            if z_impl_k_msgq_get(
                core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ),
                refb.as_mut_ptr() as *mut c_void,
                K_FOREVER,
            ) != 0
            {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) != 0 {
                break;
            }
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP),
                refb.as_ptr() as *const c_void,
                K_FOREVER,
            );
        }
    }
}

pub fn zcopy_create() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        for i in 0..ZCOPY_BYTES {
            (*tx)[i] = 0x3Cu8 ^ (i as u8);
            (*shared)[i] = 0x3Cu8 ^ (i as u8);
        }
        *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) = 0;
        k_msgq_init(
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ_BUF).cast::<c_void>(),
            ZCOPY_BYTES,
            1,
        );
        k_msgq_init(
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REP_BUF).cast::<c_void>(),
            ZCOPY_BYTES,
            1,
        );
        k_msgq_init(
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ),
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ_BUF).cast::<c_void>(),
            4,
            1,
        );
        k_msgq_init(
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP),
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP_BUF).cast::<c_void>(),
            4,
            1,
        );
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_ZCOPY_CP_THREAD),
            core::ptr::addr_of_mut!(S_ZCOPY_CP_STACK) as *mut c_void,
            1024,
            zcopy_copy_peer_entry,
            core::ptr::null_mut(),
            coop_prio(1),
        )?;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_ZCOPY_ZC_THREAD),
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_STACK) as *mut c_void,
            1024,
            zcopy_ref_peer_entry,
            core::ptr::null_mut(),
            coop_prio(1),
        )?;
        *core::ptr::addr_of_mut!(S_ZCOPY_ACTIVE) = 1;
        Ok(())
    }
}

pub fn zcopy_reset() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        for i in 0..ZCOPY_BYTES {
            (*shared)[i] = (*tx)[i];
        }
        Ok(())
    }
}

pub fn zcopy_copied() -> Result<u64, ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        let rx = core::ptr::addr_of_mut!(S_ZCOPY_RX);
        let start = bench_rust_now_ticks();
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
            (*tx).as_ptr() as *const c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        if z_impl_k_msgq_get(
            core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
            (*rx).as_mut_ptr() as *mut c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        let end = bench_rust_now_ticks();
        for i in 0..ZCOPY_BYTES {
            if (*rx)[i] != (*tx)[i] {
                return Err(());
            }
        }
        Ok(end - start)
    }
}

pub fn zcopy_ref() -> Result<u64, ()> {
    unsafe {
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        let refb = (shared as u32).to_ne_bytes();
        let start = bench_rust_now_ticks();
        if z_impl_k_msgq_put(
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ),
            refb.as_ptr() as *const c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        let mut back = [0u8; 4];
        if z_impl_k_msgq_get(
            core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP),
            back.as_mut_ptr() as *mut c_void,
            K_FOREVER,
        ) != 0
        {
            return Err(());
        }
        let end = bench_rust_now_ticks();
        if u32::from_ne_bytes(back) != shared as u32 {
            return Err(());
        }
        Ok(end - start)
    }
}

pub fn zcopy_verify() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        for i in 0..ZCOPY_BYTES {
            if (*shared)[i] != (*tx)[i] {
                return Err(());
            }
        }
        Ok(())
    }
}

pub fn zcopy_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_ZCOPY_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) = 1;
            let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
            let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
            let refp = (shared as u32).to_ne_bytes();
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
                (*tx).as_ptr() as *const c_void,
                K_FOREVER,
            );
            let _ = z_impl_k_msgq_put(
                core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ),
                refp.as_ptr() as *const c_void,
                K_FOREVER,
            );
            thread_join(&mut *core::ptr::addr_of_mut!(S_ZCOPY_CP_THREAD));
            thread_join(&mut *core::ptr::addr_of_mut!(S_ZCOPY_ZC_THREAD));
            *core::ptr::addr_of_mut!(S_ZCOPY_ACTIVE) = 0;
        }
    }
}

// -------------------------------------------- priority-inheritance mutex (8)

const M_SPIN_HZ_DIV: u64 = 500; // 2 ms spin at ticks_per_second

static mut S_M_MUTEX: KMutex = KMutex::zeroed();
static mut S_M_REQ: KSem = KSem::zeroed();
static mut S_M_HELD: KSem = KSem::zeroed();
static mut S_M_REL: KSem = KSem::zeroed();
static mut S_M_GO: KSem = KSem::zeroed();
static mut S_M_DONE: KSem = KSem::zeroed();
static mut S_M_THREAD: KThread = KThread::zeroed();
static mut S_L_THREAD: KThread = KThread::zeroed();
static mut S_M_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_L_STACK: ThreadStack<{ stack_obj_len(2048) }> = ThreadStack::zeroed();
static mut S_M_QUIT: c_int = 0;
static mut S_M_STOP: c_int = 0;
static mut S_M_MADE: u32 = 0;

extern "C" fn mutex_low_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_M_REQ));
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
            let _ = z_impl_k_mutex_lock(core::ptr::addr_of_mut!(S_M_MUTEX), K_FOREVER);
            sem_give(&mut *core::ptr::addr_of_mut!(S_M_HELD));
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_M_REL));
            let _ = z_impl_k_mutex_unlock(core::ptr::addr_of_mut!(S_M_MUTEX));
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
        }
    }
}

extern "C" fn mutex_med_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_M_GO));
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
            let start = bench_rust_now_ticks();
            let limit = bench_rust_ticks_per_second() / M_SPIN_HZ_DIV;
            while *core::ptr::addr_of_mut!(S_M_STOP) == 0
                && (bench_rust_now_ticks() - start) < limit
            {
                core::hint::spin_loop();
            }
            sem_give(&mut *core::ptr::addr_of_mut!(S_M_DONE));
        }
    }
}

pub fn mutex_pi_create() -> Result<(), ()> {
    unsafe {
        if *core::ptr::addr_of_mut!(S_M_MADE) != 0 {
            return Ok(());
        }
        sem_create(&mut *core::ptr::addr_of_mut!(S_M_REQ))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_M_HELD))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_M_REL))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_M_GO))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_M_DONE))?;
        z_impl_k_mutex_init(core::ptr::addr_of_mut!(S_M_MUTEX));
        *core::ptr::addr_of_mut!(S_M_QUIT) = 0;
        let base = current_priority();
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_M_THREAD),
            core::ptr::addr_of_mut!(S_M_STACK) as *mut c_void,
            2048,
            mutex_med_entry,
            core::ptr::null_mut(),
            base + 1,
        )?;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_L_THREAD),
            core::ptr::addr_of_mut!(S_L_STACK) as *mut c_void,
            2048,
            mutex_low_entry,
            core::ptr::null_mut(),
            base + 2,
        )?;
        *core::ptr::addr_of_mut!(S_M_MADE) = 1;
        Ok(())
    }
}

pub fn mutex_pi_ready() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_M_MADE) }
}

pub fn mutex_pi_reset() {
    unsafe {
        *core::ptr::addr_of_mut!(S_M_STOP) = 0;
    }
}

pub fn mutex_pi_req() -> Result<(), ()> {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_M_REQ));
        Ok(())
    }
}

pub fn mutex_pi_held() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_M_HELD)) }
}

pub fn mutex_pi_go() {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_M_GO));
    }
}

pub fn mutex_pi_rel() {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_M_REL));
    }
}

pub fn mutex_pi_lock() -> Result<(), ()> {
    unsafe {
        if z_impl_k_mutex_lock(core::ptr::addr_of_mut!(S_M_MUTEX), K_FOREVER) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_set_stop() {
    unsafe {
        *core::ptr::addr_of_mut!(S_M_STOP) = 1;
    }
}

pub fn mutex_pi_done() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_M_DONE)) }
}

pub fn mutex_pi_unlock() -> Result<(), ()> {
    unsafe {
        if z_impl_k_mutex_unlock(core::ptr::addr_of_mut!(S_M_MUTEX)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_M_MADE) != 0 {
            *core::ptr::addr_of_mut!(S_M_QUIT) = 1;
            sem_give(&mut *core::ptr::addr_of_mut!(S_M_REQ));
            sem_give(&mut *core::ptr::addr_of_mut!(S_M_REL));
            sem_give(&mut *core::ptr::addr_of_mut!(S_M_GO));
            thread_join(&mut *core::ptr::addr_of_mut!(S_M_THREAD));
            thread_join(&mut *core::ptr::addr_of_mut!(S_L_THREAD));
            *core::ptr::addr_of_mut!(S_M_MADE) = 0;
        }
    }
}

// ---------------------------------------------------------- timer callback (9)

static mut S_TIMER: KTimer = KTimer::zeroed();
static mut S_TIMER_SEM: KSem = KSem::zeroed();
static mut S_TIMER_FIRE: u64 = 0;

extern "C" fn timer_expiry(_t: *mut KTimer) {
    unsafe {
        *core::ptr::addr_of_mut!(S_TIMER_FIRE) = bench_rust_now_ticks();
        sem_give(&mut *core::ptr::addr_of_mut!(S_TIMER_SEM));
    }
}

pub fn timer_create() -> Result<(), ()> {
    unsafe {
        sem_create(&mut *core::ptr::addr_of_mut!(S_TIMER_SEM))?;
        k_timer_init(core::ptr::addr_of_mut!(S_TIMER), Some(timer_expiry), None);
        Ok(())
    }
}

pub fn timer_drain() -> Result<(), ()> {
    unsafe {
        while sem_trytake(&mut *core::ptr::addr_of_mut!(S_TIMER_SEM)).is_ok() {}
        Ok(())
    }
}

pub fn timer_arm() -> Result<(), ()> {
    unsafe {
        let dur = KTimeout {
            ticks: bench_rust_ms_to_ticks(10),
        };
        z_impl_k_timer_start(core::ptr::addr_of_mut!(S_TIMER), dur, K_NO_WAIT);
        Ok(())
    }
}

pub fn timer_wait() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_TIMER_SEM)) }
}

pub fn timer_fire() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_TIMER_FIRE) }
}

pub fn timer_destroy() {
    unsafe {
        z_impl_k_timer_stop(core::ptr::addr_of_mut!(S_TIMER));
    }
}

// ------------------------------------------------------------ notification (17)

static mut S_NOTIFY_GO: KSem = KSem::zeroed();
static mut S_NOTIFY_DONE: KSem = KSem::zeroed();
static mut S_NOTIFY_THREAD: KThread = KThread::zeroed();
static mut S_NOTIFY_STACK: ThreadStack<{ stack_obj_len(1024) }> = ThreadStack::zeroed();
static mut S_NOTIFY_SIGNAL: u64 = 0;
static mut S_NOTIFY_QUIT: c_int = 0;
static mut S_NOTIFY_ACTIVE: c_int = 0;

extern "C" fn notify_peer_entry(_a: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_NOTIFY_GO));
            if *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_NOTIFY_SIGNAL) = bench_rust_now_ticks();
            sem_give(&mut *core::ptr::addr_of_mut!(S_NOTIFY_DONE));
        }
    }
}

pub fn notify_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_NOTIFY_ACTIVE) = 0;
        sem_create(&mut *core::ptr::addr_of_mut!(S_NOTIFY_GO))?;
        sem_create(&mut *core::ptr::addr_of_mut!(S_NOTIFY_DONE))?;
        let prio = current_priority() + 1;
        thread_create(
            &mut *core::ptr::addr_of_mut!(S_NOTIFY_THREAD),
            core::ptr::addr_of_mut!(S_NOTIFY_STACK) as *mut c_void,
            1024,
            notify_peer_entry,
            core::ptr::null_mut(),
            prio,
        )?;
        *core::ptr::addr_of_mut!(S_NOTIFY_ACTIVE) = 1;
        Ok(())
    }
}

pub fn notify_drain() -> Result<(), ()> {
    unsafe {
        while sem_trytake(&mut *core::ptr::addr_of_mut!(S_NOTIFY_DONE)).is_ok() {}
        Ok(())
    }
}

pub fn notify_go() -> Result<(), ()> {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_NOTIFY_GO));
        Ok(())
    }
}

pub fn notify_done() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_NOTIFY_DONE)) }
}

pub fn notify_signal() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_NOTIFY_SIGNAL) }
}

pub fn notify_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_NOTIFY_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) = 1;
            sem_give(&mut *core::ptr::addr_of_mut!(S_NOTIFY_GO));
            thread_join(&mut *core::ptr::addr_of_mut!(S_NOTIFY_THREAD));
            *core::ptr::addr_of_mut!(S_NOTIFY_ACTIVE) = 0;
        }
    }
}

// ---------------------------------------------------------- priority (26)

const PRIO_N: usize = 4;
static mut S_PRIO_RELEASE: KSem = KSem::zeroed();
static mut S_PRIO_ALL: KSem = KSem::zeroed();
static mut S_PRIO_ACK: KSem = KSem::zeroed();
static mut S_PRIO_THREADS: [KThread; PRIO_N] = [KThread::zeroed(); PRIO_N];
static mut S_PRIO_STACKS: [ThreadStack<{ stack_obj_len(1024) }>; PRIO_N] =
    [ThreadStack::zeroed(); PRIO_N];
static mut S_PRIO_N: u32 = 0;
static mut S_PRIO_TRACE: [u8; PRIO_N] = [0; PRIO_N];
static mut S_PRIO_QUIT: c_int = 0;
static mut S_PRIO_ACTIVE: u32 = 0;

extern "C" fn prio_peer_entry(arg: *mut c_void, _b: *mut c_void, _c: *mut c_void) {
    unsafe {
        let id = arg as usize as u8;
        loop {
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_PRIO_RELEASE));
            if *core::ptr::addr_of_mut!(S_PRIO_QUIT) != 0 {
                break;
            }
            let recorded_count = *core::ptr::addr_of_mut!(S_PRIO_N);
            if recorded_count < PRIO_N as u32 {
                *core::ptr::addr_of_mut!(S_PRIO_TRACE[recorded_count as usize]) = id;
                *core::ptr::addr_of_mut!(S_PRIO_N) = recorded_count + 1;
            }
            sem_give(&mut *core::ptr::addr_of_mut!(S_PRIO_ALL));
            let _ = sem_take_forever(&mut *core::ptr::addr_of_mut!(S_PRIO_ACK));
        }
    }
}

pub fn prio_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_PRIO_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) = 0;
        sem_create_limit(&mut *core::ptr::addr_of_mut!(S_PRIO_RELEASE), PRIO_N as u32)?;
        sem_create_limit(&mut *core::ptr::addr_of_mut!(S_PRIO_ALL), PRIO_N as u32)?;
        sem_create_limit(&mut *core::ptr::addr_of_mut!(S_PRIO_ACK), PRIO_N as u32)?;
        for i in 0..PRIO_N {
            // id PRIO_N-1 gets the highest cooperative priority (K_PRIO_COOP(1)).
            let prio = coop_prio((PRIO_N - i) as c_int);
            thread_create(
                &mut *core::ptr::addr_of_mut!(S_PRIO_THREADS[i]),
                core::ptr::addr_of_mut!(S_PRIO_STACKS[i]) as *mut c_void,
                1024,
                prio_peer_entry,
                i as *mut c_void,
                prio,
            )?;
        }
        *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) = 1;
        Ok(())
    }
}

pub fn prio_ready() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) }
}

pub fn prio_reset() {
    unsafe {
        *core::ptr::addr_of_mut!(S_PRIO_N) = 0;
    }
}

pub fn prio_release() -> Result<(), ()> {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_PRIO_RELEASE));
        Ok(())
    }
}

pub fn prio_all() -> Result<(), ()> {
    unsafe { sem_take_forever(&mut *core::ptr::addr_of_mut!(S_PRIO_ALL)) }
}

pub fn prio_check() -> bool {
    unsafe {
        if *core::ptr::addr_of_mut!(S_PRIO_N) != PRIO_N as u32 {
            return false;
        }
        for i in 0..PRIO_N {
            if *core::ptr::addr_of_mut!(S_PRIO_TRACE[i]) != (PRIO_N - 1 - i) as u8 {
                return false;
            }
        }
        true
    }
}

pub fn prio_ack() {
    unsafe {
        sem_give(&mut *core::ptr::addr_of_mut!(S_PRIO_ACK));
    }
}

pub fn prio_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_PRIO_QUIT) = 1;
            for _ in 0..PRIO_N {
                sem_give(&mut *core::ptr::addr_of_mut!(S_PRIO_RELEASE));
            }
            for i in 0..PRIO_N {
                thread_join(&mut *core::ptr::addr_of_mut!(S_PRIO_THREADS[i]));
            }
            *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) = 0;
        }
    }
}
