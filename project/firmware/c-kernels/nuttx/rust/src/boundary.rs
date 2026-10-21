//! NuttX kernel-object FFI boundary.
//!
//! Native Rust calls into the NuttX POSIX API for the kernel objects the
//! benchmark cases exercise. The C port keeps only the timer/ISR register
//! shims and the record/flush bridges; the case control flow stays in
//! `lib.rs`. Keeping the boundary here makes the Rust-side unchecked work
//! explicit and enumerable, the same way the FreeRTOS adapter's
//! `boundary.rs` does for its handle-based API.
#![allow(clippy::needless_range_loop)]

use core::ffi::{c_char, c_int, c_uint, c_void};

/// NuttX POSIX message-queue descriptor (`mqd_t`).
pub type Mqd = c_int;

/// Sentinel the kernel returns for a failed `mq_open`.
pub const MQD_INVALID: Mqd = -1;

// `mq_open` flags: O_RDWR | O_CREAT | O_EXCL (values from NuttX `fcntl.h`).
const O_RDWR: c_int = 2;
const O_CREAT: c_int = 64;
const O_EXCL: c_int = 128;
const MQ_FLAGS: c_int = O_RDWR | O_CREAT | O_EXCL;
const MQ_MODE: c_int = 0o644;

extern "C" {
    // `mq_open` is variadic in C (mode, then an optional `struct mq_attr *`);
    // the fixed four-argument declaration below matches the AAPCS/RV32 layout
    // for integer/pointer arguments, which is all this boundary passes.
    fn mq_open(name: *const c_char, oflags: c_int, mode: c_int, attr: *mut c_void) -> Mqd;
    fn mq_send(mq: Mqd, msg: *const c_char, len: usize, prio: c_uint) -> c_int;
    fn mq_receive(mq: Mqd, msg: *mut c_char, len: usize, prio: *mut c_uint) -> isize;
    fn mq_close(mq: Mqd) -> c_int;
    fn mq_unlink(name: *const c_char) -> c_int;
}

/// Create the benchmark message queue, unlinked first so a stale name never
/// leaks a previous run's queue.
pub fn queue_create(name: &core::ffi::CStr) -> Result<Mqd, ()> {
    unsafe {
        mq_unlink(name.as_ptr());
        let mq = mq_open(name.as_ptr(), MQ_FLAGS, MQ_MODE, core::ptr::null_mut());
        if mq == MQD_INVALID {
            return Err(());
        }
        Ok(mq)
    }
}

pub fn queue_send(mq: Mqd, value: u32) -> Result<(), ()> {
    unsafe {
        let payload = value;
        if mq_send(
            mq,
            &payload as *const u32 as *const c_char,
            core::mem::size_of::<u32>(),
            0,
        ) == -1
        {
            return Err(());
        }
        Ok(())
    }
}

pub fn queue_receive(mq: Mqd) -> Result<u32, ()> {
    unsafe {
        let mut received_value = 0u32;
        let mut prio = 0u32;
        let received_bytes = mq_receive(
            mq,
            &mut received_value as *mut u32 as *mut c_char,
            core::mem::size_of::<u32>(),
            &mut prio as *mut c_uint,
        );
        if received_bytes != core::mem::size_of::<u32>() as isize {
            return Err(());
        }
        Ok(received_value)
    }
}

pub fn queue_destroy(mq: Mqd, name: &core::ffi::CStr) {
    unsafe {
        if mq != MQD_INVALID {
            mq_close(mq);
        }
        mq_unlink(name.as_ptr());
    }
}

/// NuttX `struct mq_attr` mirror: four 32-bit `long` fields in NuttX's order
/// (`mq_maxmsg`, `mq_msgsize`, `mq_flags`, `mq_curmsgs`), which differs from the
/// POSIX field order.
#[repr(C)]
pub struct MqAttr {
    pub mq_maxmsg: i32,
    pub mq_msgsize: i32,
    pub mq_flags: i32,
    pub mq_curmsgs: i32,
}

const _: () = {
    assert!(core::mem::size_of::<MqAttr>() == 16);
    assert!(core::mem::align_of::<MqAttr>() == 4);
};

/// Open a message queue with explicit capacity and message size.
pub fn queue_create_attr(name: &core::ffi::CStr, maxmsg: i32, msgsize: i32) -> Result<Mqd, ()> {
    unsafe {
        mq_unlink(name.as_ptr());
        let attr = MqAttr {
            mq_flags: 0,
            mq_maxmsg: maxmsg,
            mq_msgsize: msgsize,
            mq_curmsgs: 0,
        };
        let mq = mq_open(
            name.as_ptr(),
            MQ_FLAGS,
            MQ_MODE,
            &attr as *const MqAttr as *mut c_void,
        );
        if mq == MQD_INVALID {
            return Err(());
        }
        Ok(mq)
    }
}

/// Send `buf` as one message.
pub fn mq_send_bytes(mq: Mqd, buf: &[u8]) -> Result<(), ()> {
    unsafe {
        if mq_send(mq, buf.as_ptr() as *const c_char, buf.len(), 0) == -1 {
            return Err(());
        }
        Ok(())
    }
}

/// Receive one message into `buf`, returning the byte count.
pub fn mq_receive_bytes(mq: Mqd, buf: &mut [u8]) -> Result<usize, ()> {
    unsafe {
        let mut prio = 0u32;
        let received_bytes = mq_receive(
            mq,
            buf.as_mut_ptr() as *mut c_char,
            buf.len(),
            &mut prio as *mut c_uint,
        );
        if received_bytes < 0 {
            return Err(());
        }
        Ok(received_bytes as usize)
    }
}

/// NuttX `clockid_t` for the monotonic clock.
pub const CLOCK_MONOTONIC: c_int = 1;
const TIMER_ABSTIME: c_int = 1;

/// NuttX `struct timespec` mirror: `time_t tv_sec` (int64) + `long tv_nsec`
/// (32-bit). Size/alignment are asserted so a config change cannot silently
/// corrupt the clock calls.
#[repr(C)]
struct Timespec {
    tv_sec: i64,
    tv_nsec: i32,
}

const _: () = {
    assert!(core::mem::size_of::<Timespec>() == 16);
    assert!(core::mem::align_of::<Timespec>() == 8);
};

extern "C" {
    fn clock_gettime(clk_id: c_int, tp: *mut Timespec) -> c_int;
    fn clock_nanosleep(
        clk_id: c_int,
        flags: c_int,
        req: *const Timespec,
        rem: *mut Timespec,
    ) -> c_int;
    fn nanosleep(req: *const Timespec, rem: *mut Timespec) -> c_int;
}

/// Current CLOCK_MONOTONIC time in nanoseconds.
pub fn monotonic_ns() -> Result<u64, ()> {
    unsafe {
        let mut ts = Timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        if clock_gettime(CLOCK_MONOTONIC, &mut ts as *mut Timespec) != 0 {
            return Err(());
        }
        Ok(ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64)
    }
}

/// Absolute CLOCK_MONOTONIC sleep until `ns` (nanoseconds since the monotonic
/// epoch).
pub fn abs_sleep_ns(ns: u64) -> Result<(), ()> {
    unsafe {
        let req = Timespec {
            tv_sec: (ns / 1_000_000_000) as i64,
            tv_nsec: (ns % 1_000_000_000) as i32,
        };
        if clock_nanosleep(
            CLOCK_MONOTONIC,
            TIMER_ABSTIME,
            &req as *const Timespec,
            core::ptr::null_mut(),
        ) != 0
        {
            return Err(());
        }
        Ok(())
    }
}

/// Relative sleep for `ms` milliseconds.
pub fn sleep_ms(ms: u32) -> Result<(), ()> {
    unsafe {
        let req = Timespec {
            tv_sec: 0,
            tv_nsec: (ms as i32) * 1_000_000,
        };
        if nanosleep(&req as *const Timespec, core::ptr::null_mut()) != 0 {
            return Err(());
        }
        Ok(())
    }
}

/// NuttX `sem_t` (`struct sem_s`) opaque mirror: 16 B, align 4 under the
/// benchmark configs (`PRIORITY_INHERITANCE`, `PRIORITY_PROTECT` and
/// `CUSTOM_SEMAPHORE_MAXVALUE` unset). The kernel writes the real struct into
/// this storage; this boundary only passes its address.
#[repr(C, align(4))]
pub struct SemT {
    _data: [u8; 16],
}

/// NuttX `pthread_mutex_t` (`struct pthread_mutex_s`) opaque mirror: 24 B,
/// align 4 (`PTHREAD_MUTEX_TYPES` unset).
#[repr(C, align(4))]
pub struct MutexT {
    _data: [u8; 24],
}

impl SemT {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 16] }
    }
}

impl MutexT {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 24] }
    }
}

const _: () = {
    assert!(core::mem::size_of::<SemT>() == 16);
    assert!(core::mem::align_of::<SemT>() == 4);
    assert!(core::mem::size_of::<MutexT>() == 24);
    assert!(core::mem::align_of::<MutexT>() == 4);
};

extern "C" {
    fn sem_init(sem: *mut SemT, pshared: c_int, value: c_uint) -> c_int;
    fn sem_post(sem: *mut SemT) -> c_int;
    fn sem_wait(sem: *mut SemT) -> c_int;
    fn sem_trywait(sem: *mut SemT) -> c_int;
    fn sem_destroy(sem: *mut SemT) -> c_int;
    fn pthread_mutex_init(mutex: *mut MutexT, attr: *mut c_void) -> c_int;
    fn pthread_mutex_lock(mutex: *mut MutexT) -> c_int;
    fn pthread_mutex_unlock(mutex: *mut MutexT) -> c_int;
    fn pthread_mutex_destroy(mutex: *mut MutexT) -> c_int;
}

pub fn sem_create(sem: &mut SemT) -> Result<(), ()> {
    unsafe {
        if sem_init(sem as *mut SemT, 0, 0) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn sem_post_op(sem: &mut SemT) -> Result<(), ()> {
    unsafe {
        if sem_post(sem as *mut SemT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn sem_wait_op(sem: &mut SemT) -> Result<(), ()> {
    unsafe {
        if sem_wait(sem as *mut SemT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn sem_destroy_op(sem: &mut SemT) {
    unsafe {
        sem_destroy(sem as *mut SemT);
    }
}

pub fn mutex_create(mutex: &mut MutexT) -> Result<(), ()> {
    unsafe {
        if pthread_mutex_init(mutex as *mut MutexT, core::ptr::null_mut()) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_lock_op(mutex: &mut MutexT) -> Result<(), ()> {
    unsafe {
        if pthread_mutex_lock(mutex as *mut MutexT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_unlock_op(mutex: &mut MutexT) -> Result<(), ()> {
    unsafe {
        if pthread_mutex_unlock(mutex as *mut MutexT) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_destroy_op(mutex: &mut MutexT) {
    unsafe {
        pthread_mutex_destroy(mutex as *mut MutexT);
    }
}

/// NuttX `pthread_t` (`pid_t`, a task id).
pub type PthreadT = c_int;

/// NuttX `struct sched_param` mirror.
#[repr(C)]
pub struct SchedParam {
    pub sched_priority: c_int,
}

/// NuttX `pthread_attr_t` (`struct pthread_attr_s`) opaque mirror: 16 B,
/// align 4.
#[repr(C, align(4))]
pub struct PthreadAttrT {
    _data: [u8; 16],
}

const SCHED_FIFO: c_int = 1;
const PTHREAD_EXPLICIT_SCHED: c_int = 1;

const _: () = {
    assert!(core::mem::size_of::<SchedParam>() == 4);
    assert!(core::mem::align_of::<SchedParam>() == 4);
    assert!(core::mem::size_of::<PthreadAttrT>() == 16);
    assert!(core::mem::align_of::<PthreadAttrT>() == 4);
};

extern "C" {
    fn pthread_attr_init(attr: *mut PthreadAttrT) -> c_int;
    fn pthread_attr_setschedpolicy(attr: *mut PthreadAttrT, policy: c_int) -> c_int;
    fn pthread_attr_setschedparam(attr: *mut PthreadAttrT, param: *const SchedParam) -> c_int;
    fn pthread_attr_setinheritsched(attr: *mut PthreadAttrT, inherit: c_int) -> c_int;
    fn pthread_attr_setstacksize(attr: *mut PthreadAttrT, stacksize: usize) -> c_int;
    fn pthread_attr_destroy(attr: *mut PthreadAttrT) -> c_int;
    fn sched_get_priority_max(policy: c_int) -> c_int;
    fn sched_get_priority_min(policy: c_int) -> c_int;
    fn pthread_create(
        thread: *mut PthreadT,
        attr: *const PthreadAttrT,
        start: extern "C" fn(*mut c_void) -> *mut c_void,
        arg: *mut c_void,
    ) -> c_int;
    fn pthread_join(thread: PthreadT, retval: *mut *mut c_void) -> c_int;
    fn pthread_self() -> PthreadT;
    fn pthread_getschedparam(thread: PthreadT, policy: *mut c_int, param: *mut SchedParam)
        -> c_int;
    fn sched_yield() -> c_int;
    fn bench_rust_now_ticks() -> u64;
}

/// Create a peer pthread at `priority` under SCHED_FIFO with explicit sched.
pub fn thread_create(
    entry: extern "C" fn(*mut c_void) -> *mut c_void,
    priority: c_int,
    arg: *mut c_void,
) -> Result<PthreadT, ()> {
    unsafe {
        let mut attr = PthreadAttrT { _data: [0u8; 16] };
        if pthread_attr_init(&mut attr as *mut PthreadAttrT) != 0 {
            return Err(());
        }
        let sp = SchedParam {
            sched_priority: priority,
        };
        let _ = pthread_attr_setschedpolicy(&mut attr as *mut PthreadAttrT, SCHED_FIFO);
        if pthread_attr_setschedparam(&mut attr as *mut PthreadAttrT, &sp as *const SchedParam) != 0
        {
            pthread_attr_destroy(&mut attr as *mut PthreadAttrT);
            return Err(());
        }
        let _ =
            pthread_attr_setinheritsched(&mut attr as *mut PthreadAttrT, PTHREAD_EXPLICIT_SCHED);
        let mut thread: PthreadT = 0;
        let rc = pthread_create(
            &mut thread as *mut PthreadT,
            &attr as *const PthreadAttrT,
            entry,
            arg,
        );
        pthread_attr_destroy(&mut attr as *mut PthreadAttrT);
        if rc != 0 {
            return Err(());
        }
        Ok(thread)
    }
}

/// Create a peer pthread at the maximum SCHED_FIFO priority (so a post wakes it
/// immediately).
pub fn thread_create_maxfifo(
    entry: extern "C" fn(*mut c_void) -> *mut c_void,
) -> Result<PthreadT, ()> {
    unsafe {
        thread_create(
            entry,
            sched_get_priority_max(SCHED_FIFO),
            core::ptr::null_mut(),
        )
    }
}

/// Create a peer pthread at `priority` with an explicit stack size.
pub fn thread_create_stack(
    entry: extern "C" fn(*mut c_void) -> *mut c_void,
    priority: c_int,
    stacksize: usize,
    arg: *mut c_void,
) -> Result<PthreadT, ()> {
    unsafe {
        let mut attr = PthreadAttrT { _data: [0u8; 16] };
        if pthread_attr_init(&mut attr as *mut PthreadAttrT) != 0 {
            return Err(());
        }
        let sp = SchedParam {
            sched_priority: priority,
        };
        let _ = pthread_attr_setschedpolicy(&mut attr as *mut PthreadAttrT, SCHED_FIFO);
        if pthread_attr_setschedparam(&mut attr as *mut PthreadAttrT, &sp as *const SchedParam) != 0
        {
            pthread_attr_destroy(&mut attr as *mut PthreadAttrT);
            return Err(());
        }
        let _ =
            pthread_attr_setinheritsched(&mut attr as *mut PthreadAttrT, PTHREAD_EXPLICIT_SCHED);
        let _ = pthread_attr_setstacksize(&mut attr as *mut PthreadAttrT, stacksize);
        let mut thread: PthreadT = 0;
        let rc = pthread_create(
            &mut thread as *mut PthreadT,
            &attr as *const PthreadAttrT,
            entry,
            arg,
        );
        pthread_attr_destroy(&mut attr as *mut PthreadAttrT);
        if rc != 0 {
            return Err(());
        }
        Ok(thread)
    }
}

pub fn thread_self_priority() -> Result<c_int, ()> {
    unsafe {
        let mut policy: c_int = 0;
        let mut sp = SchedParam { sched_priority: 0 };
        if pthread_getschedparam(
            pthread_self(),
            &mut policy as *mut c_int,
            &mut sp as *mut SchedParam,
        ) != 0
        {
            return Err(());
        }
        Ok(sp.sched_priority)
    }
}

pub fn thread_join(thread: PthreadT) {
    unsafe {
        pthread_join(thread, core::ptr::null_mut());
    }
}

// Task-handoff peer state. Accessed only from the benchmark task and its peer,
// serialized by the semaphores; the C session never touches these.
static mut S_SIGNAL: SemT = SemT::zeroed();
static mut S_DONE: SemT = SemT::zeroed();
static mut S_PEER: PthreadT = 0;
static mut S_WAKE_TICKS: u64 = 0;
static mut S_QUIT: c_int = 0;

extern "C" fn handoff_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_SIGNAL)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_WAKE_TICKS) = bench_rust_now_ticks();
            let _ = sem_post(core::ptr::addr_of_mut!(S_DONE));
        }
    }
    core::ptr::null_mut()
}

pub fn handoff_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_QUIT) = 0;
        if sem_init(core::ptr::addr_of_mut!(S_SIGNAL), 0, 0) != 0 {
            return Err(());
        }
        if sem_init(core::ptr::addr_of_mut!(S_DONE), 0, 0) != 0 {
            return Err(());
        }
        let peer = thread_create_maxfifo(handoff_peer_entry)?;
        *core::ptr::addr_of_mut!(S_PEER) = peer;
        Ok(())
    }
}

pub fn handoff_measure() -> Result<u64, ()> {
    unsafe {
        if sem_post(core::ptr::addr_of_mut!(S_SIGNAL)) != 0 {
            return Err(());
        }
        if sem_wait(core::ptr::addr_of_mut!(S_DONE)) != 0 {
            return Err(());
        }
        Ok(*core::ptr::addr_of_mut!(S_WAKE_TICKS))
    }
}

pub fn handoff_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_QUIT) = 1;
        let _ = sem_post(core::ptr::addr_of_mut!(S_SIGNAL));
        thread_join(*core::ptr::addr_of_mut!(S_PEER));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_SIGNAL));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_DONE));
    }
}

// IPC ping-pong peer state: a request/reply message-queue pair and one echo
// peer. Serialized by the queues; the C session never touches these.
static mut S_IPC_REQ: Mqd = MQD_INVALID;
static mut S_IPC_REP: Mqd = MQD_INVALID;
static mut S_IPC_PEER: PthreadT = 0;
static mut S_IPC_QUIT: c_int = 0;

extern "C" fn ipc_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        loop {
            let received_value = match queue_receive(*core::ptr::addr_of_mut!(S_IPC_REQ)) {
                Ok(value) => value,
                Err(_) => continue,
            };
            if *core::ptr::addr_of_mut!(S_IPC_QUIT) != 0 {
                break;
            }
            let _ = queue_send(*core::ptr::addr_of_mut!(S_IPC_REP), received_value);
        }
    }
    core::ptr::null_mut()
}

pub fn ipc_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_IPC_QUIT) = 0;
        // Handles must be published before the peer thread starts, otherwise
        // the peer blocks on an invalid descriptor at its (maximum) priority.
        let req = queue_create(c"/rtos_bench_ipc_req")?;
        let rep = queue_create(c"/rtos_bench_ipc_rep")?;
        *core::ptr::addr_of_mut!(S_IPC_REQ) = req;
        *core::ptr::addr_of_mut!(S_IPC_REP) = rep;
        let peer = thread_create_maxfifo(ipc_peer_entry)?;
        *core::ptr::addr_of_mut!(S_IPC_PEER) = peer;
        Ok(())
    }
}

pub fn ipc_roundtrip(value: u32) -> Result<u32, ()> {
    unsafe {
        queue_send(*core::ptr::addr_of_mut!(S_IPC_REQ), value)?;
        queue_receive(*core::ptr::addr_of_mut!(S_IPC_REP))
    }
}

pub fn ipc_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_IPC_QUIT) = 1;
        let _ = queue_send(*core::ptr::addr_of_mut!(S_IPC_REQ), 0);
        thread_join(*core::ptr::addr_of_mut!(S_IPC_PEER));
        queue_destroy(*core::ptr::addr_of_mut!(S_IPC_REQ), c"/rtos_bench_ipc_req");
        queue_destroy(*core::ptr::addr_of_mut!(S_IPC_REP), c"/rtos_bench_ipc_rep");
        *core::ptr::addr_of_mut!(S_IPC_REQ) = MQD_INVALID;
        *core::ptr::addr_of_mut!(S_IPC_REP) = MQD_INVALID;
    }
}

// Preempt latency peer: one priority above the benchmark thread, blocked on a
// semaphore; the post preempts it, it timestamps and re-blocks.
static mut S_PREEMPT_SEM: SemT = SemT::zeroed();
static mut S_PREEMPT_TASK: PthreadT = 0;
static mut S_PREEMPT_WAKE: u64 = 0;
static mut S_PREEMPT_SEQ: u32 = 0;
static mut S_PREEMPT_READY: u32 = 0;
static mut S_PREEMPT_QUIT: c_int = 0;

extern "C" fn preempt_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        *core::ptr::addr_of_mut!(S_PREEMPT_READY) = 1;
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_PREEMPT_SEM)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) = bench_rust_now_ticks();
            *core::ptr::addr_of_mut!(S_PREEMPT_SEQ) += 1;
        }
    }
    core::ptr::null_mut()
}

pub fn preempt_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_SEQ) = 0;
        *core::ptr::addr_of_mut!(S_PREEMPT_READY) = 0;
        if sem_init(core::ptr::addr_of_mut!(S_PREEMPT_SEM), 0, 0) != 0 {
            return Err(());
        }
        let prio = thread_self_priority()?.saturating_add(1);
        if prio > sched_get_priority_max(SCHED_FIFO) {
            return Err(());
        }
        let task = thread_create(preempt_peer_entry, prio, core::ptr::null_mut())?;
        *core::ptr::addr_of_mut!(S_PREEMPT_TASK) = task;
        for _ in 0..1000 {
            if *core::ptr::addr_of_mut!(S_PREEMPT_READY) != 0 {
                break;
            }
            let _ = sched_yield();
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
        if sem_post(core::ptr::addr_of_mut!(S_PREEMPT_SEM)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn preempt_wake() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_PREEMPT_WAKE) }
}

pub fn preempt_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_PREEMPT_QUIT) = 1;
        let _ = sem_post(core::ptr::addr_of_mut!(S_PREEMPT_SEM));
        thread_join(*core::ptr::addr_of_mut!(S_PREEMPT_TASK));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_PREEMPT_SEM));
    }
}

// Notification latency peer: blocked on a go semaphore, it timestamps its wake
// and posts a done token; the benchmark drains stale done tokens first.
static mut S_NOTIFY_GO: SemT = SemT::zeroed();
static mut S_NOTIFY_DONE: SemT = SemT::zeroed();
static mut S_NOTIFY_PEER: PthreadT = 0;
static mut S_NOTIFY_SIGNAL: u64 = 0;
static mut S_NOTIFY_QUIT: c_int = 0;

extern "C" fn notify_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_NOTIFY_GO)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_NOTIFY_SIGNAL) = bench_rust_now_ticks();
            let _ = sem_post(core::ptr::addr_of_mut!(S_NOTIFY_DONE));
        }
    }
    core::ptr::null_mut()
}

pub fn notify_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) = 0;
        if sem_init(core::ptr::addr_of_mut!(S_NOTIFY_GO), 0, 0) != 0 {
            return Err(());
        }
        if sem_init(core::ptr::addr_of_mut!(S_NOTIFY_DONE), 0, 0) != 0 {
            return Err(());
        }
        let self_prio = thread_self_priority()?;
        let minp = sched_get_priority_min(SCHED_FIFO);
        let prio = if self_prio - 1 < minp {
            minp
        } else {
            self_prio - 1
        };
        let peer = thread_create(notify_peer_entry, prio, core::ptr::null_mut())?;
        *core::ptr::addr_of_mut!(S_NOTIFY_PEER) = peer;
        Ok(())
    }
}

pub fn notify_drain() -> Result<(), ()> {
    unsafe {
        while sem_trywait(core::ptr::addr_of_mut!(S_NOTIFY_DONE)) == 0 {}
        Ok(())
    }
}

pub fn notify_go() -> Result<(), ()> {
    unsafe {
        if sem_post(core::ptr::addr_of_mut!(S_NOTIFY_GO)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn notify_done() -> Result<(), ()> {
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_NOTIFY_DONE)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn notify_signal() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_NOTIFY_SIGNAL) }
}

pub fn notify_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_NOTIFY_QUIT) = 1;
        let _ = sem_post(core::ptr::addr_of_mut!(S_NOTIFY_GO));
        thread_join(*core::ptr::addr_of_mut!(S_NOTIFY_PEER));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_NOTIFY_GO));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_NOTIFY_DONE));
    }
}

// IPC payload-sweep peer: a request/reply pair with 256-byte messages and one
// echo peer. The peer's receive buffer is on its stack, so it gets a larger
// stack than the default.
static mut S_IPC_PAYLOAD_REQ: Mqd = MQD_INVALID;
static mut S_IPC_PAYLOAD_REP: Mqd = MQD_INVALID;
static mut S_IPC_PAYLOAD_PEER: PthreadT = 0;
static mut S_IPC_PAYLOAD_QUIT: c_int = 0;

extern "C" fn ipc_payload_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        let mut buf = [0u8; 256];
        loop {
            let received_bytes =
                match mq_receive_bytes(*core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ), &mut buf) {
                    Ok(bytes) => bytes,
                    Err(_) => continue,
                };
            if *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) != 0 {
                break;
            }
            let _ = mq_send_bytes(
                *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP),
                &buf[..received_bytes],
            );
        }
    }
    core::ptr::null_mut()
}

pub fn ipc_payload_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) = 0;
        let req = queue_create_attr(c"/rtos_bench_ipc_payload_req", 1, 256)?;
        let rep = queue_create_attr(c"/rtos_bench_ipc_payload_rep", 1, 256)?;
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ) = req;
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP) = rep;
        let peer = thread_create_stack(
            ipc_payload_peer_entry,
            sched_get_priority_max(SCHED_FIFO),
            4096,
            core::ptr::null_mut(),
        )?;
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_PEER) = peer;
        Ok(())
    }
}

pub fn ipc_payload_roundtrip(tx: &[u8], rx: &mut [u8]) -> Result<usize, ()> {
    unsafe {
        mq_send_bytes(*core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ), tx)?;
        mq_receive_bytes(*core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP), rx)
    }
}

pub fn ipc_payload_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_QUIT) = 1;
        let _ = mq_send_bytes(*core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ), &[0u8]);
        thread_join(*core::ptr::addr_of_mut!(S_IPC_PAYLOAD_PEER));
        queue_destroy(
            *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ),
            c"/rtos_bench_ipc_payload_req",
        );
        queue_destroy(
            *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP),
            c"/rtos_bench_ipc_payload_rep",
        );
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REQ) = MQD_INVALID;
        *core::ptr::addr_of_mut!(S_IPC_PAYLOAD_REP) = MQD_INVALID;
    }
}

// IPC throughput peer: drains a bounded 64-byte message queue and posts a done
// semaphore once it has consumed the target count.
static mut S_TP_MQ: Mqd = MQD_INVALID;
static mut S_TP_PEER: PthreadT = 0;
static mut S_TP_DONE: SemT = SemT::zeroed();
static mut S_TP_SEEN: u32 = 0;
static mut S_TP_TARGET: u32 = 0;
static mut S_TP_QUIT: c_int = 0;
static mut S_TP_TX: [u8; 64] = [0u8; 64];

const TP_MSG_BYTES: usize = 64;

extern "C" fn tp_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        let mut buf = [0u8; TP_MSG_BYTES];
        loop {
            if mq_receive_bytes(*core::ptr::addr_of_mut!(S_TP_MQ), &mut buf).is_err() {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_TP_QUIT) != 0 {
                break;
            }
            *core::ptr::addr_of_mut!(S_TP_SEEN) += 1;
            if *core::ptr::addr_of_mut!(S_TP_SEEN) >= *core::ptr::addr_of_mut!(S_TP_TARGET) {
                let _ = sem_post(core::ptr::addr_of_mut!(S_TP_DONE));
            }
        }
    }
    core::ptr::null_mut()
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
        let mq = queue_create_attr(c"/rtos_bench_tp", 8, TP_MSG_BYTES as i32)?;
        *core::ptr::addr_of_mut!(S_TP_MQ) = mq;
        if sem_init(core::ptr::addr_of_mut!(S_TP_DONE), 0, 0) != 0 {
            return Err(());
        }
        let peer = thread_create_maxfifo(tp_peer_entry)?;
        *core::ptr::addr_of_mut!(S_TP_PEER) = peer;
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
        mq_send_bytes(
            *core::ptr::addr_of_mut!(S_TP_MQ),
            &*core::ptr::addr_of_mut!(S_TP_TX),
        )
    }
}

pub fn tp_done() -> Result<(), ()> {
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_TP_DONE)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn tp_destroy() {
    unsafe {
        *core::ptr::addr_of_mut!(S_TP_QUIT) = 1;
        let _ = mq_send_bytes(*core::ptr::addr_of_mut!(S_TP_MQ), &[0u8; 64]);
        thread_join(*core::ptr::addr_of_mut!(S_TP_PEER));
        queue_destroy(*core::ptr::addr_of_mut!(S_TP_MQ), c"/rtos_bench_tp");
        *core::ptr::addr_of_mut!(S_TP_MQ) = MQD_INVALID;
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_TP_DONE));
    }
}

// Zero-copy IPC: the copied leg round-trips a 256-byte payload through a
// message queue; the zero-copy leg round-trips only a pointer to a shared
// buffer. `zcopy_measure` returns copied minus zero-copy.
static mut S_ZCOPY_CP_REQ: Mqd = MQD_INVALID;
static mut S_ZCOPY_CP_REP: Mqd = MQD_INVALID;
static mut S_ZCOPY_ZC_REQ: Mqd = MQD_INVALID;
static mut S_ZCOPY_ZC_REP: Mqd = MQD_INVALID;
static mut S_ZCOPY_CP_PEER: PthreadT = 0;
static mut S_ZCOPY_ZC_PEER: PthreadT = 0;
static mut S_ZCOPY_QUIT: c_int = 0;
static mut S_ZCOPY_TX: [u8; 256] = [0u8; 256];
static mut S_ZCOPY_RX: [u8; 256] = [0u8; 256];
static mut S_ZCOPY_SHARED: [u8; 256] = [0u8; 256];

const ZCOPY_BYTES: usize = 256;

extern "C" fn zcopy_copy_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        let mut buf = [0u8; ZCOPY_BYTES];
        loop {
            let received_bytes =
                match mq_receive_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ), &mut buf) {
                    Ok(bytes) => bytes,
                    Err(_) => continue,
                };
            if *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) != 0 {
                break;
            }
            let _ = mq_send_bytes(
                *core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
                &buf[..received_bytes],
            );
        }
    }
    core::ptr::null_mut()
}

extern "C" fn zcopy_ref_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        let mut refb = [0u8; 4];
        loop {
            if mq_receive_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ), &mut refb).is_err() {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) != 0 {
                break;
            }
            let _ = mq_send_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP), &refb);
        }
    }
    core::ptr::null_mut()
}

pub fn zcopy_create() -> Result<(), ()> {
    unsafe {
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        for i in 0..ZCOPY_BYTES {
            (*tx)[i] = 0x3Cu8 ^ (i as u8);
        }
        *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) = 0;
        let cp_req = queue_create_attr(c"/rtos_bench_zcopy_cp_req", 1, ZCOPY_BYTES as i32)?;
        let cp_rep = queue_create_attr(c"/rtos_bench_zcopy_cp_rep", 1, ZCOPY_BYTES as i32)?;
        let zc_req = queue_create_attr(c"/rtos_bench_zcopy_zc_req", 1, 4)?;
        let zc_rep = queue_create_attr(c"/rtos_bench_zcopy_zc_rep", 1, 4)?;
        *core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ) = cp_req;
        *core::ptr::addr_of_mut!(S_ZCOPY_CP_REP) = cp_rep;
        *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ) = zc_req;
        *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP) = zc_rep;
        let prio = sched_get_priority_max(SCHED_FIFO);
        let cp_peer =
            thread_create_stack(zcopy_copy_peer_entry, prio, 4096, core::ptr::null_mut())?;
        let zc_peer = thread_create_stack(zcopy_ref_peer_entry, prio, 4096, core::ptr::null_mut())?;
        *core::ptr::addr_of_mut!(S_ZCOPY_CP_PEER) = cp_peer;
        *core::ptr::addr_of_mut!(S_ZCOPY_ZC_PEER) = zc_peer;
        Ok(())
    }
}

pub fn zcopy_reset() -> Result<(), ()> {
    unsafe {
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
        for i in 0..ZCOPY_BYTES {
            (*shared)[i] = (*tx)[i];
        }
        Ok(())
    }
}

pub fn zcopy_copied() -> Result<u64, ()> {
    unsafe {
        let start = bench_rust_now_ticks();
        mq_send_bytes(
            *core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
            &*core::ptr::addr_of_mut!(S_ZCOPY_TX),
        )?;
        let received_bytes = mq_receive_bytes(
            *core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
            &mut *core::ptr::addr_of_mut!(S_ZCOPY_RX),
        )?;
        let end = bench_rust_now_ticks();
        if received_bytes != ZCOPY_BYTES {
            return Err(());
        }
        let rx = core::ptr::addr_of_mut!(S_ZCOPY_RX);
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
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
        let shared_ptr = core::ptr::addr_of_mut!(S_ZCOPY_SHARED) as u32;
        let ref_bytes = shared_ptr.to_ne_bytes();
        let start = bench_rust_now_ticks();
        mq_send_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ), &ref_bytes)?;
        let mut returned_bytes = [0u8; 4];
        let received_bytes = mq_receive_bytes(
            *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP),
            &mut returned_bytes,
        )?;
        let end = bench_rust_now_ticks();
        if received_bytes != 4 || u32::from_ne_bytes(returned_bytes) != shared_ptr {
            return Err(());
        }
        Ok(end - start)
    }
}

pub fn zcopy_verify() -> Result<(), ()> {
    unsafe {
        let shared = core::ptr::addr_of_mut!(S_ZCOPY_SHARED);
        let tx = core::ptr::addr_of_mut!(S_ZCOPY_TX);
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
        *core::ptr::addr_of_mut!(S_ZCOPY_QUIT) = 1;
        let _ = mq_send_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ), &[0u8]);
        let _ = mq_send_bytes(*core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ), &[0u8; 4]);
        thread_join(*core::ptr::addr_of_mut!(S_ZCOPY_CP_PEER));
        thread_join(*core::ptr::addr_of_mut!(S_ZCOPY_ZC_PEER));
        queue_destroy(
            *core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ),
            c"/rtos_bench_zcopy_cp_req",
        );
        queue_destroy(
            *core::ptr::addr_of_mut!(S_ZCOPY_CP_REP),
            c"/rtos_bench_zcopy_cp_rep",
        );
        queue_destroy(
            *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ),
            c"/rtos_bench_zcopy_zc_req",
        );
        queue_destroy(
            *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP),
            c"/rtos_bench_zcopy_zc_rep",
        );
        *core::ptr::addr_of_mut!(S_ZCOPY_CP_REQ) = MQD_INVALID;
        *core::ptr::addr_of_mut!(S_ZCOPY_CP_REP) = MQD_INVALID;
        *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REQ) = MQD_INVALID;
        *core::ptr::addr_of_mut!(S_ZCOPY_ZC_REP) = MQD_INVALID;
    }
}

// Yield round trip: a same-priority peer spins sched_yield and counts its runs;
// the benchmark yields until the peer's count advances. The task-count sweep
// adds up to 32 extra cooperative peers.
static mut S_YIELD_PEER: PthreadT = 0;
static mut S_YIELD_PEER_RUNS: u32 = 0;
static mut S_YIELD_PEER_STARTED: u32 = 0;
static mut S_YIELD_QUIT: c_int = 0;

const PEERS_MAX_EXTRA: usize = 32;
static mut S_PEERS_EXTRA: [PthreadT; PEERS_MAX_EXTRA] = [0; PEERS_MAX_EXTRA];
static mut S_PEERS_QUIT: [c_int; PEERS_MAX_EXTRA] = [0; PEERS_MAX_EXTRA];
static mut S_PEERS_COUNT: u32 = 0;

extern "C" fn yield_peer_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) = 1;
        while *core::ptr::addr_of_mut!(S_YIELD_QUIT) == 0 {
            *core::ptr::addr_of_mut!(S_YIELD_PEER_RUNS) += 1;
            let _ = sched_yield();
        }
    }
    core::ptr::null_mut()
}

extern "C" fn peers_extra_entry(arg: *mut c_void) -> *mut c_void {
    unsafe {
        let idx = arg as usize;
        while *core::ptr::addr_of_mut!(S_PEERS_QUIT[idx]) == 0 {
            let _ = sched_yield();
        }
    }
    core::ptr::null_mut()
}

pub fn peers_set(count: u32) -> Result<(), ()> {
    unsafe {
        let want = if count > 0 { count - 1 } else { 0 };
        if want > PEERS_MAX_EXTRA as u32 {
            return Err(());
        }
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) > want {
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) -= 1;
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            *core::ptr::addr_of_mut!(S_PEERS_QUIT[i]) = 1;
            thread_join(*core::ptr::addr_of_mut!(S_PEERS_EXTRA[i]));
        }
        if *core::ptr::addr_of_mut!(S_PEERS_COUNT) == want {
            return Ok(());
        }
        let prio = thread_self_priority()?;
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) < want {
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            *core::ptr::addr_of_mut!(S_PEERS_QUIT[i]) = 0;
            let t = thread_create_stack(peers_extra_entry, prio, 1024, i as *mut c_void)?;
            *core::ptr::addr_of_mut!(S_PEERS_EXTRA[i]) = t;
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) += 1;
        }
        Ok(())
    }
}

pub fn yield_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_YIELD_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_YIELD_PEER_RUNS) = 0;
        *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) = 0;
        let prio = thread_self_priority()?;
        let peer = thread_create(yield_peer_entry, prio, core::ptr::null_mut())?;
        *core::ptr::addr_of_mut!(S_YIELD_PEER) = peer;
        for _ in 0..1000 {
            if *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) != 0 {
                break;
            }
            let _ = sched_yield();
        }
        if *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) == 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn yield_started() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) }
}

pub fn yield_runs() -> u32 {
    unsafe { *core::ptr::addr_of_mut!(S_YIELD_PEER_RUNS) }
}

pub fn yield_destroy() {
    unsafe {
        while *core::ptr::addr_of_mut!(S_PEERS_COUNT) > 0 {
            *core::ptr::addr_of_mut!(S_PEERS_COUNT) -= 1;
            let i = *core::ptr::addr_of_mut!(S_PEERS_COUNT) as usize;
            *core::ptr::addr_of_mut!(S_PEERS_QUIT[i]) = 1;
            thread_join(*core::ptr::addr_of_mut!(S_PEERS_EXTRA[i]));
        }
        if *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) != 0 {
            *core::ptr::addr_of_mut!(S_YIELD_QUIT) = 1;
            thread_join(*core::ptr::addr_of_mut!(S_YIELD_PEER));
            *core::ptr::addr_of_mut!(S_YIELD_PEER_STARTED) = 0;
        }
    }
}

// Priority conformance: four peers one..four priorities above the benchmark
// block on a release semaphore; posting it wakes them in priority order, and
// each records its id into the trace before waiting for an ack.
const PRIO_N: usize = 4;
static mut S_PRIO_RELEASE: SemT = SemT::zeroed();
static mut S_PRIO_ALL: SemT = SemT::zeroed();
static mut S_PRIO_ACK: SemT = SemT::zeroed();
static mut S_PRIO_TASK: [PthreadT; PRIO_N] = [0; PRIO_N];
static mut S_PRIO_N: u32 = 0;
static mut S_PRIO_TRACE: [u8; PRIO_N] = [0; PRIO_N];
static mut S_PRIO_QUIT: c_int = 0;
static mut S_PRIO_ACTIVE: u32 = 0;

extern "C" fn prio_peer_entry(arg: *mut c_void) -> *mut c_void {
    unsafe {
        let id = arg as usize as u8;
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_PRIO_RELEASE)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_PRIO_QUIT) != 0 {
                break;
            }
            let recorded_count = *core::ptr::addr_of_mut!(S_PRIO_N);
            if recorded_count < PRIO_N as u32 {
                *core::ptr::addr_of_mut!(S_PRIO_TRACE[recorded_count as usize]) = id;
                *core::ptr::addr_of_mut!(S_PRIO_N) = recorded_count + 1;
            }
            let _ = sem_post(core::ptr::addr_of_mut!(S_PRIO_ALL));
            let _ = sem_wait(core::ptr::addr_of_mut!(S_PRIO_ACK));
        }
    }
    core::ptr::null_mut()
}

pub fn prio_create() -> Result<(), ()> {
    unsafe {
        *core::ptr::addr_of_mut!(S_PRIO_QUIT) = 0;
        *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) = 0;
        if sem_init(core::ptr::addr_of_mut!(S_PRIO_RELEASE), 0, 0) != 0 {
            return Err(());
        }
        if sem_init(core::ptr::addr_of_mut!(S_PRIO_ALL), 0, 0) != 0 {
            return Err(());
        }
        if sem_init(core::ptr::addr_of_mut!(S_PRIO_ACK), 0, 0) != 0 {
            return Err(());
        }
        let self_prio = thread_self_priority()?;
        let maxp = sched_get_priority_max(SCHED_FIFO);
        for i in 0..PRIO_N {
            let prio = self_prio + 1 + i as c_int;
            if prio > maxp {
                return Err(());
            }
            let t = thread_create(prio_peer_entry, prio, i as *mut c_void)?;
            *core::ptr::addr_of_mut!(S_PRIO_TASK[i]) = t;
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
        if sem_post(core::ptr::addr_of_mut!(S_PRIO_RELEASE)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn prio_all() -> Result<(), ()> {
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_PRIO_ALL)) != 0 {
            return Err(());
        }
        Ok(())
    }
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
        let _ = sem_post(core::ptr::addr_of_mut!(S_PRIO_ACK));
    }
}

pub fn prio_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) != 0 {
            *core::ptr::addr_of_mut!(S_PRIO_QUIT) = 1;
            for _ in 0..PRIO_N {
                let _ = sem_post(core::ptr::addr_of_mut!(S_PRIO_RELEASE));
            }
            for i in 0..PRIO_N {
                thread_join(*core::ptr::addr_of_mut!(S_PRIO_TASK[i]));
            }
            *core::ptr::addr_of_mut!(S_PRIO_ACTIVE) = 0;
        }
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_PRIO_RELEASE));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_PRIO_ALL));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_PRIO_ACK));
    }
}

// Priority-inheritance mutex: H (the benchmark) blocks on a mutex held by L
// (H+2) while M (H+1) spins a bounded ~2 ms. The measure is the mutex-acquire
// latency while M spins; NuttX mutexes raise the holder to the highest waiter.
const M_SPIN_NS: i64 = 2_000_000;

static mut S_M_MUTEX: MutexT = MutexT::zeroed();
static mut S_M_REQ: SemT = SemT::zeroed();
static mut S_M_HELD: SemT = SemT::zeroed();
static mut S_M_REL: SemT = SemT::zeroed();
static mut S_M_GO: SemT = SemT::zeroed();
static mut S_M_DONE: SemT = SemT::zeroed();
static mut S_M_TASK: PthreadT = 0;
static mut S_L_TASK: PthreadT = 0;
static mut S_M_QUIT: c_int = 0;
static mut S_M_STOP: c_int = 0;
static mut S_M_MADE: u32 = 0;

extern "C" fn mutex_low_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_M_REQ)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
            let _ = pthread_mutex_lock(core::ptr::addr_of_mut!(S_M_MUTEX));
            let _ = sem_post(core::ptr::addr_of_mut!(S_M_HELD));
            let _ = sem_wait(core::ptr::addr_of_mut!(S_M_REL));
            let _ = pthread_mutex_unlock(core::ptr::addr_of_mut!(S_M_MUTEX));
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
        }
    }
    core::ptr::null_mut()
}

extern "C" fn mutex_med_entry(_arg: *mut c_void) -> *mut c_void {
    unsafe {
        loop {
            if sem_wait(core::ptr::addr_of_mut!(S_M_GO)) != 0 {
                continue;
            }
            if *core::ptr::addr_of_mut!(S_M_QUIT) != 0 {
                break;
            }
            let dl = monotonic_ns().unwrap_or(0) as i64 + M_SPIN_NS;
            loop {
                if *core::ptr::addr_of_mut!(S_M_STOP) != 0 {
                    break;
                }
                if monotonic_ns().unwrap_or(0) as i64 >= dl {
                    break;
                }
            }
            let _ = sem_post(core::ptr::addr_of_mut!(S_M_DONE));
        }
    }
    core::ptr::null_mut()
}

pub fn mutex_pi_create() -> Result<(), ()> {
    unsafe {
        if *core::ptr::addr_of_mut!(S_M_MADE) != 0 {
            return Ok(());
        }
        if sem_init(core::ptr::addr_of_mut!(S_M_REQ), 0, 0) != 0
            || sem_init(core::ptr::addr_of_mut!(S_M_HELD), 0, 0) != 0
            || sem_init(core::ptr::addr_of_mut!(S_M_REL), 0, 0) != 0
            || sem_init(core::ptr::addr_of_mut!(S_M_GO), 0, 0) != 0
            || sem_init(core::ptr::addr_of_mut!(S_M_DONE), 0, 0) != 0
        {
            return Err(());
        }
        if pthread_mutex_init(core::ptr::addr_of_mut!(S_M_MUTEX), core::ptr::null_mut()) != 0 {
            return Err(());
        }
        *core::ptr::addr_of_mut!(S_M_QUIT) = 0;
        let self_prio = thread_self_priority()?;
        let maxp = sched_get_priority_max(SCHED_FIFO);
        if self_prio + 2 > maxp {
            return Err(());
        }
        let med = thread_create(mutex_med_entry, self_prio + 1, core::ptr::null_mut())?;
        let low = thread_create(mutex_low_entry, self_prio + 2, core::ptr::null_mut())?;
        *core::ptr::addr_of_mut!(S_M_TASK) = med;
        *core::ptr::addr_of_mut!(S_L_TASK) = low;
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
        if sem_post(core::ptr::addr_of_mut!(S_M_REQ)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_held() -> Result<(), ()> {
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_M_HELD)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_go() {
    unsafe {
        let _ = sem_post(core::ptr::addr_of_mut!(S_M_GO));
    }
}

pub fn mutex_pi_rel() {
    unsafe {
        let _ = sem_post(core::ptr::addr_of_mut!(S_M_REL));
    }
}

pub fn mutex_pi_lock() -> Result<(), ()> {
    unsafe {
        if pthread_mutex_lock(core::ptr::addr_of_mut!(S_M_MUTEX)) != 0 {
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
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_M_DONE)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_unlock() -> Result<(), ()> {
    unsafe {
        if pthread_mutex_unlock(core::ptr::addr_of_mut!(S_M_MUTEX)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn mutex_pi_destroy() {
    unsafe {
        if *core::ptr::addr_of_mut!(S_M_MADE) != 0 {
            *core::ptr::addr_of_mut!(S_M_QUIT) = 1;
            let _ = sem_post(core::ptr::addr_of_mut!(S_M_REQ));
            let _ = sem_post(core::ptr::addr_of_mut!(S_M_REL));
            let _ = sem_post(core::ptr::addr_of_mut!(S_M_GO));
            thread_join(*core::ptr::addr_of_mut!(S_M_TASK));
            thread_join(*core::ptr::addr_of_mut!(S_L_TASK));
            *core::ptr::addr_of_mut!(S_M_MADE) = 0;
        }
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_M_REQ));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_M_HELD));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_M_REL));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_M_GO));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_M_DONE));
        let _ = pthread_mutex_destroy(core::ptr::addr_of_mut!(S_M_MUTEX));
    }
}

/// NuttX `struct wdog_s` opaque mirror: 24 B, align 8 (list node + wdparm_t +
/// function pointer + 64-bit `clock_t`).
#[repr(C, align(8))]
pub struct WdogT {
    _data: [u8; 24],
}

impl WdogT {
    pub const fn zeroed() -> Self {
        Self { _data: [0u8; 24] }
    }
}

const _: () = {
    assert!(core::mem::size_of::<WdogT>() == 24);
    assert!(core::mem::align_of::<WdogT>() == 8);
};

extern "C" {
    // `wd_start` is a static inline over `wd_start_abstick` with
    // `clock_delay2abstick(delay) = clock_systime_ticks() + delay + 1`; only the
    // latter two are exported symbols.
    fn wd_start_abstick(
        wdog: *mut WdogT,
        ticks: i64,
        entry: extern "C" fn(usize),
        arg: usize,
    ) -> c_int;
    fn wd_cancel(wdog: *mut WdogT) -> c_int;
    fn clock_systime_ticks() -> i64;
}

// Timer-callback case: a one-shot watchdog armed for 10 ms; the callback
// (timer interrupt context) timestamps itself and posts a semaphore.
static mut S_TIMER_SEM: SemT = SemT::zeroed();
static mut S_TIMER_FIRE: u64 = 0;
static mut S_TIMER_WDOG: WdogT = WdogT::zeroed();

const TIMER_PERIOD_SYS_TICKS: i64 = 10;

extern "C" fn timer_wd(_arg: usize) {
    unsafe {
        *core::ptr::addr_of_mut!(S_TIMER_FIRE) = bench_rust_now_ticks();
        let _ = sem_post(core::ptr::addr_of_mut!(S_TIMER_SEM));
    }
}

pub fn timer_create() -> Result<(), ()> {
    unsafe {
        if sem_init(core::ptr::addr_of_mut!(S_TIMER_SEM), 0, 0) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn timer_drain() -> Result<(), ()> {
    unsafe {
        while sem_trywait(core::ptr::addr_of_mut!(S_TIMER_SEM)) == 0 {}
        Ok(())
    }
}

pub fn timer_arm() -> Result<(), ()> {
    unsafe {
        let abstick = clock_systime_ticks() + TIMER_PERIOD_SYS_TICKS + 1;
        if wd_start_abstick(core::ptr::addr_of_mut!(S_TIMER_WDOG), abstick, timer_wd, 0) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn timer_wait() -> Result<(), ()> {
    unsafe {
        if sem_wait(core::ptr::addr_of_mut!(S_TIMER_SEM)) != 0 {
            return Err(());
        }
        Ok(())
    }
}

pub fn timer_fire() -> u64 {
    unsafe { *core::ptr::addr_of_mut!(S_TIMER_FIRE) }
}

pub fn timer_destroy() {
    unsafe {
        let _ = wd_cancel(core::ptr::addr_of_mut!(S_TIMER_WDOG));
        let _ = sem_destroy(core::ptr::addr_of_mut!(S_TIMER_SEM));
    }
}
