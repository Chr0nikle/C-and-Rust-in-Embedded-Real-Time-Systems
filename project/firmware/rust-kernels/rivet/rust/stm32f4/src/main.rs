//! Rivet RTOS benchmark session on the STM32F407 Discovery.
//!
//! Protocol v1 (same wire format as every other path): boot -> `ready`,
//! host -> `hello`/`start`, firmware -> `started`/`measurement`/`complete`.
//! The session runs as a preemptive task at priority 5; timing uses the
//! DWT cycle counter (`rivet::port::arch::cycle_count`) at the 16 MHz HSI
//! clock this BSP leaves the chip at.

#![no_std]
#![no_main]

use rivet_rt as _;

mod arena;
mod bsp;
mod coop;

const HZ: u64 = 168_000_000;
const PROTO_BOARD: &str = "stm32f4discovery";
const PROTO_RTOS: &str = "rivet";
const PROTO_KERNEL: &str = "rivet-rtos-0.2.0";
const PROTO_LANGUAGE: &str = "rust";
const PROTO_APPLICATION: &str = "rivet-stm32f4";

// Start with the cases that have a faithful preemptive mapping; expand
// incrementally.
const CASES: [u32; 27] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 25, 26, 27,
    28,
];

const LINE_MAX: usize = 1024;
const RUN_ID_MAX: usize = 64;

fn now() -> u64 {
    rivet::port::arch::cycle_count()
}

/// Case 20: run the fixed-work low ISR once, with (`pend_h != 0`) or without
/// pending the higher-priority line, and return the elapsed cycle count.
fn nested_run(pend_h: u32) -> u64 {
    use core::sync::atomic::Ordering::SeqCst;
    NESTED_LOW_DONE.store(0, SeqCst);
    NESTED_H_COUNT.store(0, SeqCst);
    NESTED_PEND_H.store(pend_h, SeqCst);
    let t0 = now();
    unsafe {
        cortex_m::peripheral::NVIC::pend(stm32f4::stm32f407::Interrupt::EXTI1);
    }
    while NESTED_LOW_DONE.load(SeqCst) == 0 {
        core::hint::spin_loop();
    }
    now().wrapping_sub(t0)
}

// Case 10 (trigger_to_isr): a software-pended EXTI0 line runs a bare handler
// registered through `rivet::irq`; the handler records its DWT entry time.
const EXTI0_IRQ: u32 = 6;
static ISR_ENTRY: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

fn dummy_isr() {
    ISR_ENTRY.store(now() as u32, core::sync::atomic::Ordering::SeqCst);
}

// Case 20 (nested_isr): EXTI1 (low priority) runs a fixed-iteration loop and
// pends EXTI2 (higher priority) at intervals, which preempts it. The baseline
// run executes the same loop without pending, so the reported value is
// (nested elapsed) - (baseline elapsed), matching the shared C runner contract
// (min of three each). `BENCH_NESTED_CONTROL=1` must report ~0.
const EXTI1_IRQ: u32 = 7;
const EXTI2_IRQ: u32 = 8;
const NESTED_SPIN: u32 = 2000;
const NESTED_PENDS: u32 = 10;
const NESTED_CONTROL: bool = cfg!(nested_control);
static NESTED_LOW_DONE: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
static NESTED_H_COUNT: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
static NESTED_PEND_H: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

fn nested_hi_isr() {
    NESTED_H_COUNT.fetch_add(1, core::sync::atomic::Ordering::SeqCst);
}

fn nested_lo_isr() {
    let step = NESTED_SPIN / (NESTED_PENDS + 1);
    let mut next = step;
    let pend = !NESTED_CONTROL && NESTED_PEND_H.load(core::sync::atomic::Ordering::SeqCst) != 0;
    let mut i = 0u32;
    while i < NESTED_SPIN {
        if pend && i == next && next <= NESTED_PENDS * step {
            unsafe {
                cortex_m::peripheral::NVIC::pend(stm32f4::stm32f407::Interrupt::EXTI2);
            }
            next += step;
        }
        core::hint::black_box(i);
        i += 1;
    }
    NESTED_LOW_DONE.store(1, core::sync::atomic::Ordering::SeqCst);
}

// Case 25 (max_interrupt_disabled): a TIM4 one-shot probe at 50 us. Its
// entry timestamp is recorded by a bare ISR registered through `rivet::irq`
// (Rivet's critical section masks with PRIMASK, so any NVIC priority is
// masked). The value is the delay beyond the programmed period; the
// `RIVET_MAXDIS_CONTROL` build wraps an ~80 us busy loop in
// `rivet::critical::enter` so the probe is delayed by the masked interval.
const TIM4_IRQ: u32 = 30;
static PROBE_ENTRY: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
static PROBE_FIRED: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);

fn probe_isr() {
    // Clear the update flag and disable the interrupt, or the level-triggered
    // NVIC line re-enters this handler forever.
    unsafe {
        let tim = &*stm32f4::stm32f407::TIM4::ptr();
        tim.sr().modify(|_, w| w.uif().clear_bit());
        tim.dier().modify(|_, w| w.uie().clear_bit());
    }
    PROBE_ENTRY.store(now() as u32, core::sync::atomic::Ordering::SeqCst);
    PROBE_FIRED.store(true, core::sync::atomic::Ordering::SeqCst);
}

unsafe fn tim4_arm(us: u32) {
    let rcc = &*stm32f4::stm32f407::RCC::ptr();
    rcc.apb1enr().modify(|_, w| w.tim4en().set_bit());
    let tim = &*stm32f4::stm32f407::TIM4::ptr();
    // APB1 timer clock = 84 MHz (42 MHz APB1 x2); prescale to 1 MHz.
    tim.psc().write(|w| w.psc().bits(83));
    tim.arr().write(|w| w.arr().bits((us - 1) as u16));
    tim.egr().write(|w| w.ug().set_bit());
    tim.sr().modify(|_, w| w.uif().clear_bit());
    tim.dier().modify(|_, w| w.uie().set_bit());
    cortex_m::peripheral::NVIC::unpend(stm32f4::stm32f407::Interrupt::TIM4);
    // One-pulse mode: counter stops at ARR after the update event.
    tim.cr1().modify(|_, w| w.opm().set_bit().cen().set_bit());
}

// Case 1 (queue_roundtrip): Rivet's SPSC `Channel` with usable capacity
// N-1; N=2 gives the capacity-one single-task queue the spec asks for. The
// split halves are stored once at boot in a `Once` cell.
static QCHAN: rivet::sync::Channel<u32, 2> = rivet::sync::Channel::new();
static QHALVES: rivet::sync::Once<(
    rivet::sync::Sender<'static, u32, 2>,
    rivet::sync::Receiver<'static, u32, 2>,
)> = rivet::sync::Once::new();
const QPAYLOAD: u32 = 0xA55A_0001;

// Case 12 (semaphore_latency): one release then one nonblocking acquire of
// an initially empty binary semaphore.
static SEM: rivet::sync::Semaphore<1> = rivet::sync::Semaphore::new(0);

// Case 5 (yield_roundtrip): one same-priority peer that only yields.
static PEER_ARG: Unit = Unit;
static PEER: rivet::sync::Once<rivet::preempt::TaskHandle> = rivet::sync::Once::new();

fn peer_entry(_: &'static Unit) -> ! {
    loop {
        rivet::yield_now();
    }
}

// Cases 6 (preempt_latency) and 8 (mutex_priority_inheritance) use the
// preemptive tier's blocking `PriorityMutex`. Case 6: a low-priority holder L
// records the time immediately before it unlocks; the session H blocks on the
// mutex and measures release-to-resume. Case 8: L holds the mutex, H blocks,
// and a medium-priority spinner M occupies the CPU ~2 ms; with priority
// inheritance L is boosted and releases promptly, without it H waits out M.
static MUTEX_PREEMPT: rivet::preempt::PriorityMutex<u32> = rivet::preempt::PriorityMutex::new(0);
static MUTEX_INHERIT: rivet::preempt::PriorityMutex<u32> = rivet::preempt::PriorityMutex::new(0);

static REQ6: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
static HELD6: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
static GO6: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);

static REQ8: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
static HELD8: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
static GO8: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);
static M_REQ8: core::sync::atomic::AtomicBool = core::sync::atomic::AtomicBool::new(false);

static L6: rivet::sync::Once<rivet::preempt::TaskHandle> = rivet::sync::Once::new();
static L8: rivet::sync::Once<rivet::preempt::TaskHandle> = rivet::sync::Once::new();
static M8: rivet::sync::Once<rivet::preempt::TaskHandle> = rivet::sync::Once::new();
static SPAWN_ERR: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
// Per-sample variant emitted in the measurement frame (case 23 load level;
// 0 for all other cases).
static VARIANT: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

fn l6_holder(_: &'static Unit) -> ! {
    use core::sync::atomic::Ordering::SeqCst;
    loop {
        while !REQ6.load(SeqCst) {}
        REQ6.store(false, SeqCst);
        let mut g = MUTEX_PREEMPT.lock();
        *g = 0x6;
        HELD6.store(true, SeqCst);
        while !GO6.load(SeqCst) {}
        GO6.store(false, SeqCst);
        drop(g);
        HELD6.store(false, SeqCst);
    }
}

fn l8_holder(_: &'static Unit) -> ! {
    use core::sync::atomic::Ordering::SeqCst;
    loop {
        while !REQ8.load(SeqCst) {}
        REQ8.store(false, SeqCst);
        let mut g = MUTEX_INHERIT.lock();
        *g = 0x8;
        HELD8.store(true, SeqCst);
        while !GO8.load(SeqCst) {}
        GO8.store(false, SeqCst);
        drop(g);
        HELD8.store(false, SeqCst);
    }
}

// Case 23 (cpu_overhead_under_load): a worker at priority 1 spins for
// `pct`% of a 10 ms period, then sleeps the rest. It accumulates its own
// actual busy cycles (including tick-ISR preemptions), so the measured share
// is worker + kernel without needing idle-task accounting.
static W23_PCT: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
static W23_SPIN: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

fn w23_worker(_: &'static Unit) -> ! {
    use core::sync::atomic::Ordering::Relaxed;
    // Absolute 10 ms grid (not a relative period) so a late wake cannot
    // accumulate: the spin is a fixed fraction of the nominal period and the
    // RTOS sleep re-anchors to the next grid tick.
    let period_cyc = HZ / 100;
    let period_us = 10_000u64;
    let mut next_us = rivet::port::board::now_us();
    loop {
        let pct = W23_PCT.load(Relaxed) as u64;
        if pct == 0 {
            // Idle between cases: block briefly, so the worker does not
            // perturb other cases and reacts quickly when a level is set.
            rivet::preempt::sleep_ms(5);
            next_us = rivet::port::board::now_us();
            continue;
        }
        let on = period_cyc * pct / 100;
        let t0 = now();
        while now().wrapping_sub(t0) < on {
            core::hint::spin_loop();
        }
        W23_SPIN.fetch_add(now().wrapping_sub(t0) as u32, Relaxed);
        next_us = next_us.wrapping_add(period_us);
        let nowu = rivet::port::board::now_us();
        if next_us as i64 > nowu as i64 {
            rivet::preempt::sleep_until(next_us);
        } else {
            // Already past the grid point (e.g. pct == 100): do not sleep a
            // whole tick waiting for an expired deadline — re-anchor instead.
            next_us = nowu;
        }
    }
}

fn pause_task(h: Option<&rivet::preempt::TaskHandle>) {
    if let Some(h) = h {
        for _ in 0..100 {
            if h.pause() {
                return;
            }
            rivet::preempt::sleep_ms(1);
        }
    }
}

fn m8_spinner(_: &'static Unit) -> ! {
    use core::sync::atomic::Ordering::SeqCst;
    loop {
        while !M_REQ8.load(SeqCst) {}
        M_REQ8.store(false, SeqCst);
        let a = now();
        while now().wrapping_sub(a) < HZ / 500 {
            core::hint::spin_loop();
        }
    }
}

// Case 26 (priority_scheduling_correctness): four tasks at distinct
// priorities append their priority on first run, then park. Boot order is
// 9,8,7,6 (all above the session's priority 5), so a correct scheduler
// leaves PRIO_ORDER strictly descending.
static PRIO_ORDER: [core::sync::atomic::AtomicU8; 4] = [
    core::sync::atomic::AtomicU8::new(0xFF),
    core::sync::atomic::AtomicU8::new(0xFF),
    core::sync::atomic::AtomicU8::new(0xFF),
    core::sync::atomic::AtomicU8::new(0xFF),
];
static PRIO_N: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
static PRIO_A: u8 = 9;
static PRIO_B: u8 = 8;
static PRIO_C: u8 = 7;
static PRIO_D: u8 = 6;

fn prio_task(prio: &'static u8) -> ! {
    #[cfg(prio_invert)]
    rivet::preempt::sleep_ms(*prio as u64);
    let index = PRIO_N.fetch_add(1, core::sync::atomic::Ordering::SeqCst) as usize;
    if index < PRIO_ORDER.len() {
        PRIO_ORDER[index].store(*prio, core::sync::atomic::Ordering::SeqCst);
    }
    rivet::preempt::park_forever();
}

// The benchmark session needs a large stack. It is allocated as a dedicated
// aligned static rather than from the 16 KiB kernel task-stack pool: that
// pool packs stacks with power-of-two alignment and a 64-byte guard, so a
// single 8192-byte stack consumes the whole pool and leaves no room for the
// auxiliary preemptive tasks. The async-idle task uses the same pattern.
#[repr(align(8192))]
struct SessionStack([u8; 8192]);
static mut SESSION_STACK: SessionStack = SessionStack([0; 8192]);

fn prio_ok() -> bool {
    let mut previous_priority = 0xFFu8;
    for slot in PRIO_ORDER.iter() {
        let priority = slot.load(core::sync::atomic::Ordering::SeqCst);
        if priority == 0xFF || priority >= previous_priority {
            return false;
        }
        previous_priority = priority;
    }
    true
}

fn wr(b: &[u8]) {
    rivet::console::write_bytes(b);
    rivet::console::flush_sync();
}

fn rd() -> Option<u8> {
    rivet::console::try_read_byte()
}

fn fmt_u64(v: u64, out: &mut [u8], mut pos: usize) -> usize {
    let mut tmp = [0u8; 20];
    let mut n = 0;
    let mut x = v;
    if x == 0 {
        out[pos] = b'0';
        return pos + 1;
    }
    while x > 0 {
        tmp[n] = b'0' + (x % 10) as u8;
        x /= 10;
        n += 1;
    }
    while n > 0 {
        n -= 1;
        out[pos] = tmp[n];
        pos += 1;
    }
    pos
}

fn send_ready(challenge: Option<&str>) {
    let mut buf = [0u8; 512];
    let mut p = 0usize;
    for part in [
        "{\"protocol\":1,\"type\":\"ready\",\"board\":\"",
        PROTO_BOARD,
        "\",\"rtos\":\"",
        PROTO_RTOS,
        "\",\"kernel\":\"",
        PROTO_KERNEL,
        "\",\"language\":\"",
        PROTO_LANGUAGE,
        "\",\"build_id\":\"",
        env!("BENCH_BUILD_ID"),
        "\",\"application\":\"",
        PROTO_APPLICATION,
        "\",\"cases\":[",
    ] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    for (i, c) in CASES.iter().enumerate() {
        if i != 0 {
            buf[p] = b',';
            p += 1;
        }
        p = fmt_u64(*c as u64, &mut buf, p);
    }
    buf[p] = b']';
    p += 1;
    if let Some(ch) = challenge {
        for part in [",\"challenge\":\"", ch, "\""] {
            buf[p..p + part.len()].copy_from_slice(part.as_bytes());
            p += part.len();
        }
    }
    for part in [",\"spawn_err\":"] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    p = fmt_u64(
        SPAWN_ERR.load(core::sync::atomic::Ordering::SeqCst) as u64,
        &mut buf,
        p,
    );
    buf[p] = b'}';
    p += 1;
    buf[p] = b'\n';
    p += 1;
    wr(&buf[..p]);
}

fn send_started(run_id: &str) {
    let mut buf = [0u8; 256];
    let mut p = 0usize;
    for part in [
        "{\"protocol\":1,\"type\":\"started\",\"run_id\":\"",
        run_id,
        "\",\"build_id\":\"",
        env!("BENCH_BUILD_ID"),
        "\"}\n",
    ] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    wr(&buf[..p]);
}

fn send_measurement(run_id: &str, case: u32, index: u32, ticks: u64, variant: u32) {
    let mut buf = [0u8; 256];
    let mut p = 0usize;
    for part in [
        "{\"protocol\":1,\"type\":\"measurement\",\"run_id\":\"",
        run_id,
        "\",\"case\":",
    ] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    p = fmt_u64(case as u64, &mut buf, p);
    for part in [",\"index\":"] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    p = fmt_u64(index as u64, &mut buf, p);
    for part in [",\"ticks\":"] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    p = fmt_u64(ticks, &mut buf, p);
    for part in [",\"ticks_per_second\":"] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    p = fmt_u64(HZ, &mut buf, p);
    if variant != 0 {
        for part in [",\"variant\":"] {
            buf[p..p + part.len()].copy_from_slice(part.as_bytes());
            p += part.len();
        }
        p = fmt_u64(variant as u64, &mut buf, p);
    }
    buf[p] = b'}';
    p += 1;
    buf[p] = b'\n';
    p += 1;
    wr(&buf[..p]);
}

/* -------------------------------------------------- runtime memory fields
 *
 * Rivet is zero-allocation, so there is no heap figure. Per-task stack
 * watermarking comes from report::task_stack_usage (the kernel fills stacks
 * with 0xAA and reports (used, total)); the registry is preempt::tcb::TASKS. */

fn put(buf: &mut [u8], p: &mut usize, part: &str) {
    buf[*p..*p + part.len()].copy_from_slice(part.as_bytes());
    *p += part.len();
}

fn collect_memory() -> (u64, u64, u64, u64, u64) {
    use core::sync::atomic::Ordering;
    let cur = rivet::preempt::sched::current();
    let mut count = 0u64;
    let mut worst = 0u64;
    let mut best = 0u64;
    let mut self_free = 0u64;
    for id in 0..rivet::preempt::tcb::MAX_PTASKS {
        let t = match rivet::preempt::tcb::get(id) {
            Some(t) => t,
            None => continue,
        };
        if !t.used.load(Ordering::Acquire) {
            continue;
        }
        if let Some((used, total)) = rivet::report::task_stack_usage(id) {
            let freeb = total.saturating_sub(used) as u64;
            if count == 0 || freeb < worst {
                worst = freeb;
            }
            if count == 0 || freeb > best {
                best = freeb;
            }
            if cur == Some(id) {
                self_free = freeb;
            }
        }
        count += 1;
    }
    let per_task = core::mem::size_of::<rivet::preempt::tcb::Tcb>() as u64;
    (count, worst, best, self_free, per_task)
}

fn send_complete(run_id: &str, cases: &[u32], reps: u32) {
    let mut buf = [0u8; 768];
    let mut p = 0usize;
    for part in [
        "{\"protocol\":1,\"type\":\"complete\",\"run_id\":\"",
        run_id,
        "\",\"per_case_counts\":[",
    ] {
        put(&mut buf, &mut p, part);
    }
    for (i, _c) in cases.iter().enumerate() {
        if i != 0 {
            buf[p] = b',';
            p += 1;
        }
        p = fmt_u64(reps as u64, &mut buf, p);
    }
    let (task_count, worst, best, self_free, per_task) = collect_memory();
    let stack_self = if self_free != 0 { self_free } else { worst };
    put(
        &mut buf,
        &mut p,
        "],\"status\":\"ok\",\"stack_high_water_bytes\":",
    );
    p = fmt_u64(stack_self, &mut buf, p);
    put(&mut buf, &mut p, ",\"per_task_ram_overhead_bytes\":");
    p = fmt_u64(per_task, &mut buf, p);
    put(&mut buf, &mut p, ",\"task_count\":");
    p = fmt_u64(task_count, &mut buf, p);
    put(&mut buf, &mut p, ",\"worst_stack_headroom_bytes\":");
    p = fmt_u64(worst, &mut buf, p);
    put(&mut buf, &mut p, ",\"best_stack_headroom_bytes\":");
    p = fmt_u64(best, &mut buf, p);
    put(&mut buf, &mut p, "}\n");
    wr(&buf[..p]);
}

fn send_error(code: &str, msg: &str) {
    let mut buf = [0u8; 320];
    let mut p = 0usize;
    for part in [
        "{\"protocol\":1,\"type\":\"error\",\"code\":\"",
        code,
        "\",\"message\":\"",
        msg,
        "\"}\n",
    ] {
        buf[p..p + part.len()].copy_from_slice(part.as_bytes());
        p += part.len();
    }
    wr(&buf[..p]);
}

fn find_str<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let idx = line.find(key)?;
    let rest = &line[idx + key.len()..];
    let colon = rest.find(':')?;
    let after = &rest[colon + 1..];
    let q = after.find('"')?;
    let tail = &after[q + 1..];
    let end = tail.find('"')?;
    Some(&tail[..end])
}

fn find_u32(line: &str, key: &str) -> Option<u32> {
    let idx = line.find(key)?;
    let rest = &line[idx + key.len()..];
    let colon = rest.find(':')?;
    let after = rest[colon + 1..].trim_start();
    let mut n: u32 = 0;
    let mut any = false;
    for b in after.bytes() {
        if b.is_ascii_digit() {
            n = n.saturating_mul(10).saturating_add((b - b'0') as u32);
            any = true;
        } else {
            break;
        }
    }
    if any {
        Some(n)
    } else {
        None
    }
}

fn parse_cases(line: &str) -> ([u32; 32], usize) {
    let mut out = [0u32; 32];
    let mut n = 0usize;
    if let Some(idx) = line.find("\"cases\"") {
        let rest = &line[idx..];
        if let Some(lb) = rest.find('[') {
            let inside = &rest[lb + 1..];
            if let Some(rb) = inside.find(']') {
                for tok in inside[..rb].split(',') {
                    if n >= out.len() {
                        break;
                    }
                    if let Ok(v) = tok.trim().parse::<u32>() {
                        out[n] = v;
                        n += 1;
                    }
                }
            }
        }
    }
    (out, n)
}

fn advertised(id: u32) -> bool {
    CASES.contains(&id)
}

static MUTEX: rivet::preempt::PriorityMutex<u32> = rivet::preempt::PriorityMutex::new(0);

fn measure(case: u32, index: u32) -> u64 {
    VARIANT.store(0, core::sync::atomic::Ordering::SeqCst);
    match case {
        0 => {
            let a = now();
            core::hint::black_box(now());
            now() - a
        }
        1 => {
            let (tx, rx) = QHALVES.get().expect("queue halves not initialised");
            let start_ticks = now();
            if tx.try_send(QPAYLOAD).is_err() {
                return u64::MAX;
            }
            let received = rx.try_recv();
            let end_ticks = now();
            if end_ticks < start_ticks || received != Some(QPAYLOAD) {
                u64::MAX
            } else {
                end_ticks - start_ticks
            }
        }
        4 => crate::coop::isr_measure(),
        5 => {
            if let Some(h) = PEER.get() {
                h.resume();
            }
            let a = now();
            rivet::yield_now();
            let b = now();
            if let Some(h) = PEER.get() {
                h.pause();
            }
            b.wrapping_sub(a)
        }
        6 => {
            use core::sync::atomic::Ordering::SeqCst;
            if let Some(h) = L6.get() {
                h.resume();
            }
            REQ6.store(true, SeqCst);
            let mut spins = 0u32;
            while !HELD6.load(SeqCst) && spins < 1000 {
                rivet::preempt::sleep_ms(1);
                spins += 1;
            }
            if !HELD6.load(SeqCst) {
                return u64::MAX;
            }
            // The session timestamps just before releasing L; L unlocks as
            // soon as it sees GO6 (it cannot run until the session blocks),
            // so this is release-to-resume from H's side.
            let a = now();
            GO6.store(true, SeqCst);
            let mut g = MUTEX_PREEMPT.lock();
            let b = now();
            core::hint::black_box(&*g);
            drop(g);
            pause_task(L6.get());
            b.wrapping_sub(a)
        }
        7 => crate::coop::ipc_measure(1),
        8 => {
            use core::sync::atomic::Ordering::SeqCst;
            if let Some(h) = L8.get() {
                h.resume();
            }
            REQ8.store(true, SeqCst);
            let mut spins = 0u32;
            while !HELD8.load(SeqCst) && spins < 1000 {
                rivet::preempt::sleep_ms(1);
                spins += 1;
            }
            if !HELD8.load(SeqCst) {
                return u64::MAX;
            }
            // Start M spinning only after L holds the mutex, so the prio-4
            // spinner cannot starve the prio-3 holder before it acquires.
            M_REQ8.store(true, SeqCst);
            if let Some(h) = M8.get() {
                h.resume();
            }
            let a = now();
            GO8.store(true, SeqCst);
            let mut g = MUTEX_INHERIT.lock();
            let b = now();
            core::hint::black_box(&*g);
            drop(g);
            pause_task(M8.get());
            pause_task(L8.get());
            b.wrapping_sub(a)
        }
        9 => crate::coop::timer_measure(),
        10 => {
            use core::sync::atomic::Ordering::SeqCst;
            ISR_ENTRY.store(0, SeqCst);
            let a = now() as u32;
            unsafe {
                cortex_m::peripheral::NVIC::pend(stm32f4::stm32f407::Interrupt::EXTI0);
            }
            let mut s = 0u32;
            while ISR_ENTRY.load(SeqCst) == 0 && s < 1_000_000 {
                core::hint::spin_loop();
                s += 1;
            }
            ISR_ENTRY.load(SeqCst).wrapping_sub(a) as u64
        }
        17 => crate::coop::notify_measure(),
        18 => {
            let size = crate::coop::SIZES18[(index as usize) % crate::coop::SIZES18.len()];
            VARIANT.store(size, core::sync::atomic::Ordering::SeqCst);
            crate::coop::ipc_payload_measure(size)
        }
        19 => crate::coop::tp_measure(crate::coop::COUNT19),
        20 => {
            // Nested-minus-baseline: the low ISR loop runs with and without
            // pending the higher line; the difference isolates the nesting cost.
            let mut base = u64::MAX;
            let mut nested = u64::MAX;
            for _ in 0..3 {
                base = base.min(nested_run(0));
                nested = nested.min(nested_run(1));
            }
            nested.saturating_sub(base)
        }
        2 => crate::coop::handoff_measure(),
        3 => {
            // Tick-anchored periodic grid: measure the deviation of one
            // 1 ms deadline-to-deadline interval from nominal.
            let nominal = HZ / 1000;
            let mut d = rivet::port::board::now_us() + 1000;
            rivet::preempt::sleep_until(d);
            let a = now();
            d += 1000;
            rivet::preempt::sleep_until(d);
            let b = now();
            b.wrapping_sub(a).abs_diff(nominal)
        }
        11 => crate::coop::jitter_measure(),
        12 => {
            let a = now();
            SEM.release();
            let ok = SEM.try_acquire();
            let b = now();
            if !ok {
                u64::MAX
            } else {
                b.wrapping_sub(a)
            }
        }
        13 => {
            let a = now();
            let mut g = MUTEX.lock();
            *g = 1;
            core::hint::black_box(&*g);
            drop(g);
            now() - a
        }
        15 => {
            // One RTOS tick (1 ms): deviation of the actual elapsed time from
            // the nominal period.
            let nominal = HZ / 1000;
            let a = now();
            rivet::preempt::sleep_ms(1);
            let b = now();
            b.wrapping_sub(a).abs_diff(nominal)
        }
        16 => {
            let a = now();
            rivet::yield_now();
            now() - a
        }
        21 => {
            use core::sync::atomic::Ordering::SeqCst;
            let s0 = bsp::TICK_SEQ.load(SeqCst);
            let mut spins = 0u32;
            while bsp::TICK_SEQ.load(SeqCst) == s0 && spins < 20_000_000 {
                core::hint::spin_loop();
                spins += 1;
            }
            bsp::TICK_BODY_CYCLES.load(SeqCst) as u64
        }
        22 => {
            use core::sync::atomic::Ordering::SeqCst;
            let a0 = bsp::TICK_ACCUM_CYCLES.load(SeqCst) as u64;
            let t0 = now() as u32;
            rivet::preempt::sleep_ms(500);
            let t1 = now() as u32;
            let a1 = bsp::TICK_ACCUM_CYCLES.load(SeqCst) as u64;
            let win = t1.wrapping_sub(t0) as u64;
            if win == 0 {
                return 0;
            }
            1_000_000u64.saturating_mul(a1.wrapping_sub(a0)) / win
        }
        23 => {
            use core::sync::atomic::Ordering::SeqCst;
            const LEVELS: [u32; 4] = [25, 50, 75, 100];
            let level = LEVELS[(index as usize) % LEVELS.len()];
            VARIANT.store(level, SeqCst);
            W23_PCT.store(level, SeqCst);
            // Let the worker's period feedback settle before the window.
            rivet::preempt::sleep_ms(20);
            let s0 = W23_SPIN.load(SeqCst);
            let t0 = now() as u32;
            rivet::preempt::sleep_ms(500);
            let t1 = now() as u32;
            let s1 = W23_SPIN.load(SeqCst);
            let win = t1.wrapping_sub(t0) as u64;
            if win == 0 {
                return 0;
            }
            let busy = s1.wrapping_sub(s0) as u64;
            1_000_000u64.saturating_mul(busy) / win
        }
        25 => {
            use core::sync::atomic::Ordering::SeqCst;
            let period_us = 50u32;
            let period = (period_us as u64) * (HZ / 1_000_000);
            PROBE_FIRED.store(false, SeqCst);
            PROBE_ENTRY.store(0, SeqCst);
            let a = now() as u32;
            unsafe { tim4_arm(period_us) };
            #[cfg(maxdis_control)]
            rivet::critical::enter(|| {
                let t = now();
                while now().wrapping_sub(t) < 80 * (HZ / 1_000_000) {
                    core::hint::spin_loop();
                }
            });
            let mut spins = 0u32;
            while !PROBE_FIRED.load(SeqCst) && spins < 50_000_000 {
                core::hint::spin_loop();
                spins += 1;
            }
            if !PROBE_FIRED.load(SeqCst) {
                return u64::MAX;
            }
            let entry = PROBE_ENTRY.load(SeqCst);
            entry.wrapping_sub(a.wrapping_add(period as u32)) as u64
        }
        26 => {
            while PRIO_N.load(core::sync::atomic::Ordering::SeqCst) < 4 {
                rivet::preempt::sleep_ms(1);
            }
            if prio_ok() {
                1
            } else {
                0
            }
        }
        27 => crate::coop::zcopy_measure(),
        28 => crate::arena::measure_static(false) as u64,
        _ => 0,
    }
}

fn run_session(run_id: &str, cases: &[u32], repetitions: u32, warmup: u32) {
    send_started(run_id);
    for &case_id in cases {
        for warmup_index in 0..warmup {
            let _ = measure(case_id, warmup_index);
        }
        for iteration in 0..repetitions {
            let elapsed_ticks = measure(case_id, iteration);
            let variant = VARIANT.swap(0, core::sync::atomic::Ordering::SeqCst);
            send_measurement(run_id, case_id, iteration, elapsed_ticks, variant);
        }
        case_teardown(case_id);
    }
    send_complete(run_id, cases, repetitions);
}

/// Release per-case auxiliary activity once a case's samples are done.
fn case_teardown(c: u32) {
    if c == 23 {
        // Stop the graded-load worker so it does not perturb later cases.
        W23_PCT.store(0, core::sync::atomic::Ordering::SeqCst);
    }
    if c == 11 {
        // Stop the release grid and the injected-load timer.
        crate::coop::jitter_stop();
    }
}

struct Unit;
static SESSION_ARG: Unit = Unit;

fn session(_: &'static Unit) -> ! {
    send_ready(None);

    let mut line = [0u8; LINE_MAX];
    let mut len = 0usize;
    loop {
        let c = match rd() {
            Some(c) => c,
            None => continue,
        };
        if c == b'\r' {
            continue;
        }
        if c == b'\n' {
            if len > 0 {
                if let Ok(sl) = core::str::from_utf8(&line[..len]) {
                    if sl.contains("\"type\":\"hello\"") {
                        let ch = find_str(sl, "\"challenge\"");
                        send_ready(ch);
                    } else if sl.contains("\"type\":\"start\"") {
                        let rid = match find_str(sl, "\"run_id\"") {
                            Some(r) if r.len() < RUN_ID_MAX => r,
                            _ => {
                                send_error("run_id", "missing run_id");
                                len = 0;
                                continue;
                            }
                        };
                        let (cases, n) = parse_cases(sl);
                        let reps = find_u32(sl, "\"repetitions\"").unwrap_or(0);
                        let warmup = find_u32(sl, "\"warmup\"").unwrap_or(0);
                        if n == 0 || reps == 0 {
                            send_error("start", "no cases or zero repetitions");
                        } else if cases[..n].iter().any(|c| !advertised(*c)) {
                            send_error("unsupported", "case not advertised");
                        } else {
                            run_session(rid, &cases[..n], reps, warmup);
                        }
                    }
                }
            }
            len = 0;
            continue;
        }
        if len + 1 < LINE_MAX {
            line[len] = c;
            len += 1;
        } else {
            len = 0;
        }
    }
}

#[rivet::main]
fn main() -> ! {
    let _ = rivet::irq::register(EXTI0_IRQ, dummy_isr);
    rivet::irq::enable(EXTI0_IRQ);
    let _ = rivet::irq::register(EXTI1_IRQ, nested_lo_isr);
    let _ = rivet::irq::register(EXTI2_IRQ, nested_hi_isr);
    rivet::irq::set_priority(EXTI1_IRQ, 0xC0);
    rivet::irq::set_priority(EXTI2_IRQ, 0x40);
    rivet::irq::enable(EXTI1_IRQ);
    rivet::irq::enable(EXTI2_IRQ);
    let _ = rivet::irq::register(TIM4_IRQ, probe_isr);
    rivet::irq::set_priority(TIM4_IRQ, 0x40);
    rivet::irq::enable(TIM4_IRQ);
    let _ = rivet::irq::register(crate::coop::TIM2_IRQ, crate::coop::tim2_isr);
    rivet::irq::set_priority(crate::coop::TIM2_IRQ, 0x40);
    rivet::irq::enable(crate::coop::TIM2_IRQ);
    let _ = rivet::irq::register(crate::coop::TIM5_IRQ, crate::coop::tim5_isr);
    rivet::irq::set_priority(crate::coop::TIM5_IRQ, 0x40);
    rivet::irq::enable(crate::coop::TIM5_IRQ);
    let _ = rivet::irq::register(crate::coop::TIM7_IRQ, crate::coop::tim7_isr);
    rivet::irq::set_priority(crate::coop::TIM7_IRQ, 0x20);
    rivet::irq::enable(crate::coop::TIM7_IRQ);

    // Case 1: split the static channel into its single producer/consumer pair.
    if let Some(halves) = QCHAN.split() {
        let _ = QHALVES.set(halves);
    }
    // Cooperative-tier cases: split their static channels once.
    crate::coop::setup();

    // Case 26: four tasks at distinct priorities (all above session priority
    // 5, so they run at boot in descending order), each appending then parking.
    let _ = rivet::spawn_ptask!(stack = 512, priority = 9, entry = prio_task, arg = PRIO_A);
    let _ = rivet::spawn_ptask!(stack = 512, priority = 8, entry = prio_task, arg = PRIO_B);
    let _ = rivet::spawn_ptask!(stack = 512, priority = 7, entry = prio_task, arg = PRIO_C);
    let _ = rivet::spawn_ptask!(stack = 512, priority = 6, entry = prio_task, arg = PRIO_D);

    // Case 5: a same-priority peer, parked until the case measures.
    if let Ok(h) = rivet::spawn_ptask!(
        stack = 512,
        priority = 5,
        entry = peer_entry,
        arg = PEER_ARG
    ) {
        h.pause();
        let _ = PEER.set(h);
    }

    // Cases 6/8: low-priority holder(s) and a medium-priority spinner,
    // spawned suspended and resumed only while their case runs.
    match rivet::spawn_ptask!(stack = 512, priority = 3, entry = l6_holder, arg = PEER_ARG) {
        Ok(h) => {
            h.pause();
            let _ = L6.set(h);
        }
        Err(_) => {
            SPAWN_ERR.fetch_add(1, core::sync::atomic::Ordering::SeqCst);
        }
    }
    match rivet::spawn_ptask!(stack = 512, priority = 3, entry = l8_holder, arg = PEER_ARG) {
        Ok(h) => {
            h.pause();
            let _ = L8.set(h);
        }
        Err(_) => {
            SPAWN_ERR.fetch_add(1, core::sync::atomic::Ordering::SeqCst);
        }
    }
    match rivet::spawn_ptask!(
        stack = 512,
        priority = 4,
        entry = m8_spinner,
        arg = PEER_ARG
    ) {
        Ok(h) => {
            h.pause();
            let _ = M8.set(h);
        }
        Err(_) => {
            SPAWN_ERR.fetch_add(1, core::sync::atomic::Ordering::SeqCst);
        }
    }
    // Case 23 worker: always resident; idles on a long sleep when its load
    // level is 0 (see `w23_worker`).
    if rivet::spawn_ptask!(
        stack = 512,
        priority = 1,
        entry = w23_worker,
        arg = PEER_ARG
    )
    .is_err()
    {
        SPAWN_ERR.fetch_add(1, core::sync::atomic::Ordering::SeqCst);
    }

    // SAFETY: SESSION_STACK is a dedicated static, handed to exactly one task
    // for the program's lifetime.
    #[allow(static_mut_refs)]
    unsafe {
        let _ = rivet::preempt::spawn(&mut SESSION_STACK.0, 5, session, &SESSION_ARG);
    }
    rivet::run();
}
