//! Cooperative-tier (async executor) benchmark cases.
//!
//! Rivet's blocking `Signal`/`Semaphore`/`Channel` operations only exist as
//! `.await` futures, so the peer/waiting task in these cases is a
//! `#[rivet::task]` async fn. The preemptive session (priority 5) triggers a
//! measurement through a non-blocking primitive and then sleeps, letting the
//! executor (the priority-0 preemptive task) run the async peer to completion.
//! The value is therefore the executor's wake/poll path, not a PendSV context
//! switch — a documented deviation for these cases.

use core::sync::atomic::{AtomicU32, Ordering};

pub static SIG17: rivet::sync::Signal = rivet::sync::Signal::new();
static RES17: AtomicU32 = AtomicU32::new(0);

// Case 4 (isr_to_task): a TIM2 one-shot ISR records its entry and signals the
// blocked async target; the value is ISR-entry to target-resume.
pub static SIG4: rivet::sync::Signal = rivet::sync::Signal::new();
static ISR4_ENTRY: AtomicU32 = AtomicU32::new(0);
static RES4: AtomicU32 = AtomicU32::new(0);

pub const TIM2_IRQ: u32 = 28;

pub fn tim2_isr() {
    // Clear the update flag / disable the interrupt, or the level line
    // re-enters forever.
    unsafe {
        let tim = &*stm32f4::stm32f407::TIM2::ptr();
        tim.sr().modify(|_, w| w.uif().clear_bit());
        tim.dier().modify(|_, w| w.uie().clear_bit());
    }
    ISR4_ENTRY.store(rivet::port::arch::cycle_count() as u32, Ordering::SeqCst);
    SIG4.signal();
}

unsafe fn tim2_arm(us: u32) {
    let rcc = &*stm32f4::stm32f407::RCC::ptr();
    rcc.apb1enr().modify(|_, w| w.tim2en().set_bit());
    let tim = &*stm32f4::stm32f407::TIM2::ptr();
    // APB1 timer clock = 84 MHz; prescale to 1 MHz.
    tim.psc().write(|w| w.psc().bits(83));
    tim.arr().write(|w| w.arr().bits(us - 1));
    tim.egr().write(|w| w.ug().set_bit());
    tim.sr().modify(|_, w| w.uif().clear_bit());
    tim.dier().modify(|_, w| w.uie().set_bit());
    cortex_m::peripheral::NVIC::unpend(stm32f4::stm32f407::Interrupt::TIM2);
    tim.cr1().modify(|_, w| w.opm().set_bit().cen().set_bit());
}

#[rivet::task(priority = 1, stack = 1024)]
async fn isr_target() {
    loop {
        SIG4.reset();
        SIG4.wait().await;
        RES4.store(rivet::port::arch::cycle_count() as u32, Ordering::SeqCst);
    }
}

/// Case 4 `isr_to_task`: arm a 100 us TIM2 one-shot and return ISR-entry to
/// awakened-task-resume.
pub fn isr_measure() -> u64 {
    unsafe { tim2_arm(100) };
    rivet::preempt::sleep_ms(2);
    RES4.load(Ordering::SeqCst)
        .wrapping_sub(ISR4_ENTRY.load(Ordering::SeqCst)) as u64
}

// Case 2 (task_handoff): the session releases a binary semaphore the peer is
// blocked on; the peer records its unblock timestamp.
pub static SEM2: rivet::sync::Semaphore<1> = rivet::sync::Semaphore::new(0);
static RES2: AtomicU32 = AtomicU32::new(0);

#[rivet::task(priority = 1, stack = 1024)]
async fn handoff_peer() {
    loop {
        SEM2.acquire().await;
        RES2.store(rivet::port::arch::cycle_count() as u32, Ordering::SeqCst);
    }
}

/// Case 2 `task_handoff`: signal the peer and return signal-to-unblock.
pub fn handoff_measure() -> u64 {
    let t0 = rivet::port::arch::cycle_count() as u32;
    SEM2.release();
    rivet::preempt::sleep_ms(1);
    RES2.load(Ordering::SeqCst).wrapping_sub(t0) as u64
}

// Case 7 (ipc_pingpong): a requester and an echo task exchange a u32 over two
// capacity-one channels; the requester times the full round trip.
type U32Ch = rivet::sync::Channel<u32, 2>;
type U32Halves = (
    rivet::sync::Sender<'static, u32, 2>,
    rivet::sync::Receiver<'static, u32, 2>,
);

pub static Q1: U32Ch = rivet::sync::Channel::new();
pub static Q2: U32Ch = rivet::sync::Channel::new();
pub static TRIG7: U32Ch = rivet::sync::Channel::new();
static Q1H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static Q2H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static TRIG7H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static RES7: AtomicU32 = AtomicU32::new(0);

#[rivet::task(priority = 1, stack = 1024)]
async fn echo7() {
    let (_, q1_rx) = Q1H.get().expect("Q1 split");
    let (q2_tx, _) = Q2H.get().expect("Q2 split");
    loop {
        let value = q1_rx.recv().await;
        q2_tx.send(value).await;
    }
}

#[rivet::task(priority = 1, stack = 1024)]
async fn requester7() {
    let (_, trig_rx) = TRIG7H.get().expect("TRIG7 split");
    let (q1_tx, _) = Q1H.get().expect("Q1 split");
    let (_, q2_rx) = Q2H.get().expect("Q2 split");
    loop {
        let trigger_value = trig_rx.recv().await;
        let start_ticks = rivet::port::arch::cycle_count() as u32;
        q1_tx.send(trigger_value).await;
        let response = q2_rx.recv().await;
        let end_ticks = rivet::port::arch::cycle_count() as u32;
        let elapsed_ticks = if response == trigger_value {
            end_ticks.wrapping_sub(start_ticks)
        } else {
            u32::MAX
        };
        RES7.store(elapsed_ticks, Ordering::SeqCst);
    }
}

/// Case 7 `ipc_pingpong`: trigger one request/echo round trip.
pub fn ipc_measure(value: u32) -> u64 {
    let (trig_tx, _) = TRIG7H.get().expect("TRIG7 split");
    if trig_tx.try_send(value).is_err() {
        return u64::MAX;
    }
    rivet::preempt::sleep_ms(2);
    RES7.load(Ordering::SeqCst) as u64
}

// Case 18 (ipc_payload_sweep): one capacity-one request/reply pair carrying a
// fixed 256-byte message whose first `size` bytes are meaningful; the requester
// times each round trip. Variant = payload size.
pub const SIZES18: [u32; 5] = [1, 4, 16, 64, 256];
type MsgCh = rivet::sync::Channel<[u8; 256], 2>;
type MsgHalves = (
    rivet::sync::Sender<'static, [u8; 256], 2>,
    rivet::sync::Receiver<'static, [u8; 256], 2>,
);

pub static MSG18: MsgCh = rivet::sync::Channel::new();
pub static REP18: MsgCh = rivet::sync::Channel::new();
pub static TRIG18: U32Ch = rivet::sync::Channel::new();
static MSG18H: rivet::sync::Once<MsgHalves> = rivet::sync::Once::new();
static REP18H: rivet::sync::Once<MsgHalves> = rivet::sync::Once::new();
static TRIG18H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static RES18: AtomicU32 = AtomicU32::new(0);

fn fill(buf: &mut [u8; 256], size: usize) {
    for (i, b) in buf.iter_mut().enumerate().take(size) {
        *b = (i as u8) ^ 0x5A;
    }
}

#[rivet::task(priority = 1, stack = 1024)]
async fn echo18() {
    let (_, rx) = MSG18H.get().expect("MSG18 split");
    let (tx, _) = REP18H.get().expect("REP18 split");
    loop {
        let m = rx.recv().await;
        tx.send(m).await;
    }
}

#[rivet::task(priority = 1, stack = 1024)]
async fn requester18() {
    let (_, trig_rx) = TRIG18H.get().expect("TRIG18 split");
    let (msg_tx, _) = MSG18H.get().expect("MSG18 split");
    let (_, rep_rx) = REP18H.get().expect("REP18 split");
    loop {
        let size = trig_rx.recv().await as usize;
        let mut buf = [0u8; 256];
        fill(&mut buf, size);
        let start_ticks = rivet::port::arch::cycle_count() as u32;
        msg_tx.send(buf).await;
        let response = rep_rx.recv().await;
        let end_ticks = rivet::port::arch::cycle_count() as u32;
        let payload_matched = response[..size] == buf[..size];
        RES18.store(
            if payload_matched {
                end_ticks.wrapping_sub(start_ticks)
            } else {
                u32::MAX
            },
            Ordering::SeqCst,
        );
    }
}

/// Case 18 `ipc_payload_sweep`: time one `size`-byte request/echo round trip.
pub fn ipc_payload_measure(size: u32) -> u64 {
    let (trig_tx, _) = TRIG18H.get().expect("TRIG18 split");
    if trig_tx.try_send(size).is_err() {
        return u64::MAX;
    }
    rivet::preempt::sleep_ms(2);
    RES18.load(Ordering::SeqCst) as u64
}

// Case 19 (ipc_throughput): a producer pushes a fixed volume of 64-byte
// messages through a depth-8 channel drained by a consumer; the value is the
// total transfer time in cycles.
pub const MSG19: usize = 64;
pub const COUNT19: u32 = 16 * 1024; // 1 MiB / 64 B
type Ch19 = rivet::sync::Channel<[u8; 64], 9>;
type Ch19Halves = (
    rivet::sync::Sender<'static, [u8; 64], 9>,
    rivet::sync::Receiver<'static, [u8; 64], 9>,
);
pub static CH19: Ch19 = rivet::sync::Channel::new();
pub static TRIG19: U32Ch = rivet::sync::Channel::new();
static CH19H: rivet::sync::Once<Ch19Halves> = rivet::sync::Once::new();
static TRIG19H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static RES19: AtomicU32 = AtomicU32::new(0);
static DONE19: AtomicU32 = AtomicU32::new(0);

#[rivet::task(priority = 1, stack = 1024)]
async fn consumer19() {
    let (_, rx) = CH19H.get().expect("CH19 split");
    loop {
        let _ = rx.recv().await;
    }
}

#[rivet::task(priority = 1, stack = 1024)]
async fn producer19() {
    let (_, trig_rx) = TRIG19H.get().expect("TRIG19 split");
    let (tx, _) = CH19H.get().expect("CH19 split");
    loop {
        let count = trig_rx.recv().await as usize;
        let buf = [0xABu8; 64];
        let a = rivet::port::arch::cycle_count() as u32;
        for _ in 0..count {
            tx.send(buf).await;
        }
        let b = rivet::port::arch::cycle_count() as u32;
        RES19.store(b.wrapping_sub(a), Ordering::SeqCst);
        DONE19.fetch_add(1, Ordering::SeqCst);
    }
}

/// Case 19 `ipc_throughput`: transfer `count` 64-byte messages and return the
/// elapsed cycles.
pub fn tp_measure(count: u32) -> u64 {
    let (trig_tx, _) = TRIG19H.get().expect("TRIG19 split");
    let d0 = DONE19.load(Ordering::SeqCst);
    if trig_tx.try_send(count).is_err() {
        return u64::MAX;
    }
    let mut spins = 0u32;
    while DONE19.load(Ordering::SeqCst) == d0 && spins < 3000 {
        rivet::preempt::sleep_ms(1);
        spins += 1;
    }
    RES19.load(Ordering::SeqCst) as u64
}

// Case 27 (zero_copy_ipc): the copied leg reuses the case-18 256-byte path;
// the zero-copy leg sends only the address of a shared 256-byte buffer with no
// payload copy. Value = copied - zero-copy cycles (clamped to 0).
static BUF27: [u8; 256] = [0x5Au8; 256];
pub static ZQ1: rivet::sync::Channel<usize, 2> = rivet::sync::Channel::new();
pub static ZQ2: rivet::sync::Channel<usize, 2> = rivet::sync::Channel::new();
pub static TRIG27: U32Ch = rivet::sync::Channel::new();
type PtrHalves = (
    rivet::sync::Sender<'static, usize, 2>,
    rivet::sync::Receiver<'static, usize, 2>,
);
static ZQ1H: rivet::sync::Once<PtrHalves> = rivet::sync::Once::new();
static ZQ2H: rivet::sync::Once<PtrHalves> = rivet::sync::Once::new();
static TRIG27H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static RES27Z: AtomicU32 = AtomicU32::new(0);
static DONE27: AtomicU32 = AtomicU32::new(0);

#[rivet::task(priority = 1, stack = 1024)]
async fn echo27() {
    let (_, rx) = ZQ1H.get().expect("ZQ1 split");
    let (tx, _) = ZQ2H.get().expect("ZQ2 split");
    loop {
        let p = rx.recv().await;
        tx.send(p).await;
    }
}

#[rivet::task(priority = 1, stack = 1024)]
async fn requester27() {
    let (_, trig_rx) = TRIG27H.get().expect("TRIG27 split");
    let (q1_tx, _) = ZQ1H.get().expect("ZQ1 split");
    let (_, q2_rx) = ZQ2H.get().expect("ZQ2 split");
    loop {
        let _ = trig_rx.recv().await;
        let addr = &BUF27 as *const [u8; 256] as usize;
        let a = rivet::port::arch::cycle_count() as u32;
        q1_tx.send(addr).await;
        let p = q2_rx.recv().await;
        let b = rivet::port::arch::cycle_count() as u32;
        RES27Z.store(
            if p == addr {
                b.wrapping_sub(a)
            } else {
                u32::MAX
            },
            Ordering::SeqCst,
        );
        DONE27.fetch_add(1, Ordering::SeqCst);
    }
}

/// Case 27 `zero_copy_ipc`: copied (256 B) minus zero-copy (pointer) cycles.
pub fn zcopy_measure() -> u64 {
    let copied = ipc_payload_measure(256);
    let (trig_tx, _) = TRIG27H.get().expect("TRIG27 split");
    let d0 = DONE27.load(Ordering::SeqCst);
    if trig_tx.try_send(1).is_err() {
        return u64::MAX;
    }
    let mut spins = 0u32;
    while DONE27.load(Ordering::SeqCst) == d0 && spins < 2000 {
        rivet::preempt::sleep_ms(1);
        spins += 1;
    }
    copied.saturating_sub(RES27Z.load(Ordering::SeqCst) as u64)
}

// Case 9 (timer_callback_latency): an async one-shot `Sleep<10_000>` (the
// kernel timer queue); the value is the absolute deviation from the 10 ms
// nominal period.
const CPU_HZ: u64 = 168_000_000;
pub static TRIG9: U32Ch = rivet::sync::Channel::new();
static TRIG9H: rivet::sync::Once<U32Halves> = rivet::sync::Once::new();
static RES9: AtomicU32 = AtomicU32::new(0);
static DONE9: AtomicU32 = AtomicU32::new(0);

#[rivet::task(priority = 1, stack = 1024)]
async fn timer9() {
    let (_, trig_rx) = TRIG9H.get().expect("TRIG9 split");
    let nom = 10_000u64 * (CPU_HZ / 1_000_000);
    loop {
        let _ = trig_rx.recv().await;
        let a = rivet::port::arch::cycle_count();
        rivet::time::Sleep::<10_000>::new().await;
        let b = rivet::port::arch::cycle_count();
        RES9.store(b.saturating_sub(a).abs_diff(nom) as u32, Ordering::SeqCst);
        DONE9.fetch_add(1, Ordering::SeqCst);
    }
}

/// Case 9 `timer_callback_latency`: one 10 ms one-shot timer, deviation.
pub fn timer_measure() -> u64 {
    let (trig_tx, _) = TRIG9H.get().expect("TRIG9 split");
    let d0 = DONE9.load(Ordering::SeqCst);
    if trig_tx.try_send(1).is_err() {
        return u64::MAX;
    }
    let mut spins = 0u32;
    while DONE9.load(Ordering::SeqCst) == d0 && spins < 3000 {
        rivet::preempt::sleep_ms(1);
        spins += 1;
    }
    RES9.load(Ordering::SeqCst) as u64
}

// Case 11 (periodic_jitter_loaded): a TIM5 750 us auto-reload release grid
// signals the blocked async target (release instant recorded by the ISR); a
// TIM7 137 us high-priority busy-loop injector provides the load. The value is
// the signed activation error (wake - release instant).
pub static SEM11: rivet::sync::Semaphore<1> = rivet::sync::Semaphore::new(0);
static R5: AtomicU32 = AtomicU32::new(0);
pub static ERRQ: rivet::sync::Channel<i32, 33> = rivet::sync::Channel::new();
type I32Halves = (
    rivet::sync::Sender<'static, i32, 33>,
    rivet::sync::Receiver<'static, i32, 33>,
);
static ERRQH: rivet::sync::Once<I32Halves> = rivet::sync::Once::new();
static T5_STARTED: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);

pub const TIM5_IRQ: u32 = 50;
pub const TIM7_IRQ: u32 = 55;

pub fn tim5_isr() {
    unsafe {
        let t = &*stm32f4::stm32f407::TIM5::ptr();
        t.sr().modify(|_, w| w.uif().clear_bit());
    }
    R5.store(rivet::port::arch::cycle_count() as u32, Ordering::Relaxed);
    SEM11.release();
}

pub fn tim7_isr() {
    unsafe {
        let t = &*stm32f4::stm32f407::TIM7::ptr();
        t.sr().modify(|_, w| w.uif().clear_bit());
    }
    // Calibrated ~50 us injected load at 168 MHz.
    let t0 = rivet::port::arch::cycle_count();
    while rivet::port::arch::cycle_count().wrapping_sub(t0) < 8_400 {
        core::hint::spin_loop();
    }
}

pub unsafe fn jitter_start() {
    if T5_STARTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let rcc = &*stm32f4::stm32f407::RCC::ptr();
    rcc.apb1enr().modify(|_, w| w.tim5en().set_bit());
    let t5 = &*stm32f4::stm32f407::TIM5::ptr();
    t5.psc().write(|w| w.psc().bits(83)); // 1 MHz
    t5.arr().write(|w| w.arr().bits(749)); // 750 us
    t5.egr().write(|w| w.ug().set_bit());
    t5.sr().modify(|_, w| w.uif().clear_bit());
    t5.dier().modify(|_, w| w.uie().set_bit());
    t5.cr1().modify(|_, w| w.cen().set_bit());

    if !cfg!(pjit_load_off) {
        rcc.apb1enr().modify(|_, w| w.tim7en().set_bit());
        let t7 = &*stm32f4::stm32f407::TIM7::ptr();
        t7.psc().write(|w| w.psc().bits(83)); // 1 MHz
        t7.arr().write(|w| w.arr().bits(136)); // 137 us
        t7.egr().write(|w| w.ug().set_bit());
        t7.sr().modify(|_, w| w.uif().clear_bit());
        t7.dier().modify(|_, w| w.uie().set_bit());
        t7.cr1().modify(|_, w| w.cen().set_bit());
    }
}

pub fn jitter_stop() {
    unsafe {
        let t5 = &*stm32f4::stm32f407::TIM5::ptr();
        t5.cr1().modify(|_, w| w.cen().clear_bit());
        t5.dier().modify(|_, w| w.uie().clear_bit());
        let t7 = &*stm32f4::stm32f407::TIM7::ptr();
        t7.cr1().modify(|_, w| w.cen().clear_bit());
        t7.dier().modify(|_, w| w.uie().clear_bit());
    }
    T5_STARTED.store(false, Ordering::SeqCst);
}

#[rivet::task(priority = 1, stack = 1024)]
async fn jitter11() {
    let (tx, _) = ERRQH.get().expect("ERRQ split");
    loop {
        SEM11.acquire().await;
        let wake_ticks = rivet::port::arch::cycle_count() as u32;
        let release_ticks = R5.load(Ordering::Relaxed);
        let error_ticks = (wake_ticks as i32).wrapping_sub(release_ticks as i32);
        let _ = tx.try_send(error_ticks);
    }
}

/// Case 11 `periodic_jitter_loaded`: one release's signed activation error.
pub fn jitter_measure() -> u64 {
    unsafe { jitter_start() };
    let (_, rx) = ERRQH.get().expect("ERRQ split");
    let mut spins = 0u32;
    loop {
        if let Some(e) = rx.try_recv() {
            return (e as i64) as u64;
        }
        rivet::preempt::sleep_ms(1);
        spins += 1;
        if spins > 20 {
            return u64::MAX;
        }
    }
}

/// Split the static channels once, at boot.
pub fn setup() {
    if let Some(h) = Q1.split() {
        let _ = Q1H.set(h);
    }
    if let Some(h) = Q2.split() {
        let _ = Q2H.set(h);
    }
    if let Some(h) = TRIG7.split() {
        let _ = TRIG7H.set(h);
    }
    if let Some(h) = MSG18.split() {
        let _ = MSG18H.set(h);
    }
    if let Some(h) = REP18.split() {
        let _ = REP18H.set(h);
    }
    if let Some(h) = TRIG18.split() {
        let _ = TRIG18H.set(h);
    }
    if let Some(h) = CH19.split() {
        let _ = CH19H.set(h);
    }
    if let Some(h) = TRIG19.split() {
        let _ = TRIG19H.set(h);
    }
    if let Some(h) = ZQ1.split() {
        let _ = ZQ1H.set(h);
    }
    if let Some(h) = ZQ2.split() {
        let _ = ZQ2H.set(h);
    }
    if let Some(h) = TRIG27.split() {
        let _ = TRIG27H.set(h);
    }
    if let Some(h) = TRIG9.split() {
        let _ = TRIG9H.set(h);
    }
    if let Some(h) = ERRQ.split() {
        let _ = ERRQH.set(h);
    }
}

/// Case 17 target: blocks on a `Signal`, then records its resume timestamp.
#[rivet::task(priority = 1, stack = 1024)]
async fn notify_target() {
    loop {
        SIG17.wait().await;
        RES17.store(rivet::port::arch::cycle_count() as u32, Ordering::SeqCst);
    }
}

/// Case 17 `notification_latency`: the session (the peer) signals the blocked
/// task and returns signal-to-resume in cycles.
pub fn notify_measure() -> u64 {
    SIG17.reset();
    let t0 = rivet::port::arch::cycle_count() as u32;
    SIG17.signal();
    rivet::preempt::sleep_ms(1);
    RES17.load(Ordering::SeqCst).wrapping_sub(t0) as u64
}
