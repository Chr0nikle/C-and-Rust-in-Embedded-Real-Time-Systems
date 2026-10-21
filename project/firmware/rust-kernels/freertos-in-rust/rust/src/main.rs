#![no_std]
#![no_main]
#![allow(non_snake_case)]
#![allow(static_mut_refs)]

//! FreeRTOS-in-Rust benchmark firmware for the STM32F4 Discovery.
//!
//! This is the pure-Rust translation of the FreeRTOS kernel (`freertos-in-rust`
//! 0.3, Cortex-M4F port), not the C kernel. The benchmark harness drives the
//! safe `sync` veneer (queues, semaphores, mutex with priority inheritance,
//! event groups) through the shared UART protocol v1. Time is DWT CYCCNT at the
//! 168 MHz core clock.

extern crate panic_halt;

use core::ffi::c_void;
use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

#[allow(dead_code)]
mod arena;

use cortex_m_rt::entry;

// Link the port's exception handlers into the vector table.
use freertos_in_rust::port::{
    portDISABLE_INTERRUPTS, portENABLE_INTERRUPTS, vPortSVCHandler, xPortPendSVHandler,
    xPortSysTickHandler,
};

#[used]
static HANDLERS: [unsafe extern "C" fn(); 3] =
    [vPortSVCHandler, xPortPendSVHandler, xPortSysTickHandler];

use freertos_in_rust::memory::FreeRtosAllocator;

#[global_allocator]
static ALLOCATOR: FreeRtosAllocator = FreeRtosAllocator;

use freertos_in_rust::kernel::event_groups::EventBits_t;
use freertos_in_rust::kernel::tasks::{
    ulTaskGetIdleRunTimeCounter, uxTaskGetNumberOfTasks, uxTaskGetStackHighWaterMark,
    uxTaskGetSystemState, vTaskDelay, vTaskDelete, xTaskCreate, xTaskDelayUntil, xTaskGetTickCount,
    StaticTask_t,
};
use freertos_in_rust::memory::{
    pvPortMalloc, vPortFree, xPortGetFreeHeapSize, xPortGetMinimumEverFreeHeapSize,
};
use freertos_in_rust::port::portYIELD_FROM_ISR;
use freertos_in_rust::start_scheduler;
use freertos_in_rust::sync::{
    BinarySemaphore, CountingSemaphore, EventGroup, MessageBuffer, Mutex, Queue, TaskContext,
    TaskHandle, Timer,
};
use freertos_in_rust::types::{
    BaseType_t, StackType_t, TaskHandle_t, TaskStatus_t, TickType_t, TimerHandle_t, UBaseType_t,
};
use freertos_in_rust::{delay, delay_ms};

// F4 enables APB1 peripherals via RCC.APB1ENR; H7 uses RCC.APB1LENR.
#[cfg(feature = "stm32h723")]
macro_rules! apb1enr {
    ($rcc:expr) => {
        $rcc.apb1lenr
    };
}
#[cfg(not(feature = "stm32h723"))]
macro_rules! apb1enr {
    ($rcc:expr) => {
        $rcc.apb1enr()
    };
}

// Register access: the stm32h7 PAC exposes registers as struct fields, the
// stm32f4 PAC as accessor methods.
#[cfg(feature = "stm32h723")]
macro_rules! reg {
    ($r:expr, $m:ident) => {
        $r.$m
    };
}
#[cfg(not(feature = "stm32h723"))]
macro_rules! reg {
    ($r:expr, $m:ident) => {
        $r.$m()
    };
}

// Prescaler giving a 1 MHz timer clock (F4 APB1 timer 84 MHz; H7 260 MHz from
// the H7 HAL default HCLK 260 / APB1 130 -> 2x = 260 MHz).
#[cfg(feature = "stm32h723")]
const TIM_PSC_1MHZ: u16 = 260 - 1;
#[cfg(not(feature = "stm32h723"))]
const TIM_PSC_1MHZ: u16 = 84 - 1;

#[cfg(not(feature = "stm32h723"))]
use stm32f4xx_hal::{
    nb,
    pac::{self, interrupt, USART2},
    prelude::*,
    rcc,
    serial::{Config, Serial},
};
#[cfg(feature = "stm32h723")]
use stm32h7xx_hal::{
    nb,
    pac::{self, interrupt, USART1},
    prelude::*,
    rcc::PllConfigStrategy,
    serial::Serial,
};

// `user-config` feature: tell the port the actual core clock for the 1 kHz
// SysTick reload (168 MHz F4, 520 MHz H7).
#[cfg(feature = "stm32h723")]
#[no_mangle]
pub static FREERTOS_CONFIG_CPU_CLOCK_HZ: u32 = 520_000_000;
#[cfg(not(feature = "stm32h723"))]
#[no_mangle]
pub static FREERTOS_CONFIG_CPU_CLOCK_HZ: u32 = 168_000_000;

#[cfg(feature = "stm32h723")]
type SerialType = Serial<USART1>;
#[cfg(not(feature = "stm32h723"))]
type SerialType = Serial<USART2, u8>;

#[cfg(feature = "stm32h723")]
const HZ: u64 = 520_000_000;
#[cfg(not(feature = "stm32h723"))]
const HZ: u64 = 168_000_000;
#[cfg(feature = "stm32h723")]
const PROTO_BOARD: &str = "stm32h723";
#[cfg(not(feature = "stm32h723"))]
const PROTO_BOARD: &str = "stm32f4discovery";
const PROTO_RTOS: &str = "freertos";
const PROTO_KERNEL: &str = "freertos-in-rust-0.3";
const PROTO_LANGUAGE: &str = "rust";
#[cfg(feature = "stm32h723")]
const PROTO_APPLICATION: &str = "freertos-in-rust-stm32h723";
#[cfg(not(feature = "stm32h723"))]
const PROTO_APPLICATION: &str = "freertos-in-rust-stm32f4";

const CASES: [u32; 29] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28,
];

const LINE_MAX: usize = 1024;
const RUN_ID_MAX: usize = 64;

const EVG_NOTIFY_BIT: EventBits_t = 1;

// ------------------------------------------------------------ shared state
static mut SERIAL: Option<SerialType> = None;

// Benchmark primitives, created once before the scheduler starts.
static mut BENCH_QUEUE: Option<Queue<u32>> = None;
static mut HANDOFF_SEM: Option<BinarySemaphore> = None;
static mut HANDOFF_DONE: Option<BinarySemaphore> = None;
static mut IPC_REQ: Option<Queue<u32>> = None;
static mut IPC_RESP: Option<Queue<u32>> = None;
static mut SEM: Option<BinarySemaphore> = None;
static mut MUTEX: Option<Mutex<u32>> = None;
static mut EVG: Option<EventGroup> = None;
static mut NOTIFY_DONE: Option<BinarySemaphore> = None;
static mut T26_GATE_HI: Option<BinarySemaphore> = None;
static mut T26_GATE_MID: Option<BinarySemaphore> = None;
static mut T26_GATE_LO: Option<BinarySemaphore> = None;
static mut T26_DONE: Option<CountingSemaphore> = None;

// case 5 (yield round trip): same-priority peer, gate semaphore.
static mut YIELD_GATE: Option<BinarySemaphore> = None;

// case 9 (timer callback latency): one-shot software timer.
static mut TIMER: Option<Timer> = None;
static mut TIMER_DONE: Option<BinarySemaphore> = None;
static TIMER_FIRED: AtomicU32 = AtomicU32::new(0);

// case 8 (mutex priority inheritance): L holds, M spins, H blocks.
static mut PI_MUTEX: Option<Mutex<u32>> = None;
static mut PI_L_HOLDS: Option<BinarySemaphore> = None;
static mut PI_L_RELEASE: Option<BinarySemaphore> = None;
static mut PI_H_TRY: Option<BinarySemaphore> = None;
static mut PI_H_GOT: Option<BinarySemaphore> = None;
static mut PI_M_RUN: Option<BinarySemaphore> = None;
static PI_H_LATENCY: AtomicU32 = AtomicU32::new(0);

// case 24 (heap fragmentation): 256-byte blocks allocated until a margin of
// free heap remains, then alternate holes; mirrors the C FreeRTOS reference
// (which stops at a 16 KiB margin on its 100 KiB heap). The hard block cap is a
// safety bound only: a small cap would leave a dominant free tail that reads as
// the "largest block" and flatten the reported fragmentation.
const FRAG_MAX_BLOCKS: usize = 1024;
const FRAG_MARGIN: usize = 4096;
static mut FRAG_BLOCKS: [*mut c_void; FRAG_MAX_BLOCKS] = [core::ptr::null_mut(); FRAG_MAX_BLOCKS];

// case 27 (zero-copy IPC): a copied 256-byte queue pair vs a pointer queue.
static mut COPY_QUEUE: Option<Queue<[u8; 256]>> = None;
static mut ZERO_QUEUE: Option<Queue<usize>> = None;

// case 19 (IPC throughput): 1 MiB in 64-byte messages, drained by a consumer.
static mut TP_QUEUE: Option<Queue<[u8; 64]>> = None;

// case 18 (IPC payload sweep): message buffers carry the real byte length.
const IPC_PAYLOAD: [u32; 5] = [1, 4, 16, 64, 256];
static mut IPC_MB_REQ: Option<MessageBuffer> = None;
static mut IPC_MB_RESP: Option<MessageBuffer> = None;

// ISR cases: TIM2 one-shot (case 4), EXTI0 software pend (case 10).
static mut ISR_SEM: Option<BinarySemaphore> = None;
static mut ISR_DONE: Option<BinarySemaphore> = None;
static ISR_ENTRY: AtomicU32 = AtomicU32::new(0);
static ISR_GIVE: AtomicU32 = AtomicU32::new(0);

// case 6 (preempt): TIM3 one-shot releases a high task that preempts a low task.
static mut PREEMPT_SEM: Option<BinarySemaphore> = None;
static mut PREEMPT_DONE: Option<BinarySemaphore> = None;
static PREEMPT_ENTRY: AtomicU32 = AtomicU32::new(0);
static PREEMPT_GIVE: AtomicU32 = AtomicU32::new(0);
// The low task must spin only during a case-6 measurement; between samples it
// blocks on this gate so it does not starve the idle task (which would zero the
// case-23 idle run-time counter).
static mut PREEMPT_LOW_GATE: Option<BinarySemaphore> = None;
static PREEMPT_LOW_ARMED: AtomicU32 = AtomicU32::new(0);

// case 20 (nested IRQ): EXTI1 (low priority) runs a fixed-iteration loop and
// pends EXTI2 (higher priority) at intervals, which preempts it. The baseline
// run executes the same loop without pending, so the reported value is
// (nested elapsed) - (baseline elapsed), matching the shared C runner contract
// (min of three runs each). `BENCH_NESTED_CONTROL=1` disables the higher pend in
// both runs and must report ~0 (positive control).
const NESTED_SPIN: u32 = 2000; // fixed-iteration low-ISR loop (~8000 cycles)
const NESTED_PENDS: u32 = 10; // higher-priority pends per low-ISR run
const NESTED_CONTROL: bool = cfg!(nested_control);
static NESTED_LOW_DONE: AtomicU32 = AtomicU32::new(0);
static NESTED_H_COUNT: AtomicU32 = AtomicU32::new(0);
static NESTED_PEND_H: AtomicU32 = AtomicU32::new(0);

// case 23 (CPU overhead under load): a graded busy worker + the idle run time.
const CPU_LOAD: [u32; 4] = [25, 50, 75, 100];
static WORKER_LOAD: AtomicU32 = AtomicU32::new(0);

// case 25 (max interrupt disabled): TIM4 one-shot masked by a critical section.
static MAXDIS_FIRE: AtomicU32 = AtomicU32::new(0);

// case 11 (periodic_jitter_loaded): TIM5 is the 750 us auto-reload release
// grid (independent of the RTOS tick) and TIM7 injects a cycle-calibrated
// busy-loop ISR at a non-harmonic 137 us, at NVIC priority 1 (above the
// syscall threshold, so it makes no RTOS calls). measure() blocks on the
// release semaphore and returns wake - (R0 + n*P) in DWT ticks.
#[cfg(feature = "stm32h723")]
const PJIT_P_CYCLES: u32 = 390_000; // 750 us @ 520 MHz DWT
#[cfg(not(feature = "stm32h723"))]
const PJIT_P_CYCLES: u32 = 126_000; // 750 us @ 168 MHz DWT
#[cfg(feature = "stm32h723")]
const PJIT_TIMER_ARR: u32 = 194_999; // 195000 counts @ 260 MHz H7 timer
#[cfg(not(feature = "stm32h723"))]
const PJIT_TIMER_ARR: u32 = 62_999; // 63000 counts @ 84 MHz APB1 timer
#[cfg(feature = "stm32h723")]
const PJIT_LOAD_ARR: u32 = 35_619; // 35620 counts @ 260 MHz H7 timer
#[cfg(not(feature = "stm32h723"))]
const PJIT_LOAD_ARR: u32 = 11_507; // 11508 counts @ 84 MHz APB1 timer
#[cfg(feature = "stm32h723")]
const PJIT_LOAD_TARGET: u32 = 24_762; // H7: 8000 cycles scaled to 520 MHz (~35% duty)
#[cfg(not(feature = "stm32h723"))]
const PJIT_LOAD_TARGET: u32 = 8_000; // F4: busy cycles per load ISR (~35% duty)
static mut PJIT_SEM: Option<BinarySemaphore> = None;
static PJIT_R0: AtomicU32 = AtomicU32::new(0);
static PJIT_RELEASES: AtomicU32 = AtomicU32::new(0);
static PJIT_LAST_RELEASES: AtomicU32 = AtomicU32::new(0);
static PJIT_ITERS: AtomicU32 = AtomicU32::new(0);
static PJIT_ARMED: AtomicBool = AtomicBool::new(false);

static HANDOFF_WAKE: AtomicU32 = AtomicU32::new(0);
static NOTIFY_WAKE: AtomicU32 = AtomicU32::new(0);
static mut PREV_WAKE: TickType_t = 0;
static RUN_ORDER: [AtomicU32; 3] = [AtomicU32::new(0), AtomicU32::new(0), AtomicU32::new(0)];
static RUN_IDX: AtomicU32 = AtomicU32::new(0);

// Task stacks and TCBs (static, in words). The session task parses 1 KiB JSON
// lines and builds 512-byte frames on its stack, so it needs a large stack;
// the peers only take/give semaphores.
const STACK_WORDS: usize = 256;
const SESSION_STACK_WORDS: usize = 4096;
static mut SESSION_STACK: [StackType_t; SESSION_STACK_WORDS] = [0; SESSION_STACK_WORDS];
static mut SESSION_TCB: StaticTask_t = StaticTask_t::new();
static mut HANDOFF_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut HANDOFF_TCB: StaticTask_t = StaticTask_t::new();
static mut NOTIFY_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut NOTIFY_TCB: StaticTask_t = StaticTask_t::new();
static mut IPC_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut IPC_TCB: StaticTask_t = StaticTask_t::new();
static mut T26_HI_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut T26_HI_TCB: StaticTask_t = StaticTask_t::new();
static mut T26_MID_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut T26_MID_TCB: StaticTask_t = StaticTask_t::new();
static mut T26_LO_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut T26_LO_TCB: StaticTask_t = StaticTask_t::new();
static mut YIELD_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut YIELD_TCB: StaticTask_t = StaticTask_t::new();
static mut PI_LOW_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut PI_LOW_TCB: StaticTask_t = StaticTask_t::new();
static mut PI_MED_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut PI_MED_TCB: StaticTask_t = StaticTask_t::new();
static mut PI_HIGH_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut PI_HIGH_TCB: StaticTask_t = StaticTask_t::new();
static mut TP_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut TP_TCB: StaticTask_t = StaticTask_t::new();
static mut MB_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut MB_TCB: StaticTask_t = StaticTask_t::new();
static mut PREEMPT_LOW_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut PREEMPT_LOW_TCB: StaticTask_t = StaticTask_t::new();
static mut PREEMPT_HIGH_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut PREEMPT_HIGH_TCB: StaticTask_t = StaticTask_t::new();
static mut CPU_WORKER_STACK: [StackType_t; STACK_WORDS] = [0; STACK_WORDS];
static mut CPU_WORKER_TCB: StaticTask_t = StaticTask_t::new();

// --------------------------------------------------------------- helpers
fn now_ticks() -> u32 {
    cortex_m::peripheral::DWT::cycle_count()
}

/// Case 20: run the fixed-work low ISR once, with (`pend_h != 0`) or without
/// pending the higher-priority line, and return the elapsed cycle count.
fn nested_run(pend_h: u32) -> u32 {
    NESTED_LOW_DONE.store(0, Ordering::SeqCst);
    NESTED_H_COUNT.store(0, Ordering::SeqCst);
    NESTED_PEND_H.store(pend_h, Ordering::SeqCst);
    let t0 = now_ticks();
    cortex_m::peripheral::NVIC::pend(pac::Interrupt::EXTI1);
    while NESTED_LOW_DONE.load(Ordering::SeqCst) == 0 {
        core::hint::spin_loop();
    }
    now_ticks().wrapping_sub(t0)
}

#[inline(always)]
fn serial() -> &'static mut SerialType {
    unsafe { SERIAL.as_mut().expect("UART not initialized") }
}

// ---------------------------------------------------------------- entry
#[entry]
fn main() -> ! {
    // F4: 168 MHz SYSCLK + USART2 (PA2/PA3). H7: 520 MHz SYSCLK + USART1
    // (PA9/PA10) from a 25 MHz HSE crystal on the FK723M1 core board.
    let dp = pac::Peripherals::take().unwrap();
    #[cfg(not(feature = "stm32h723"))]
    let serial = {
        let mut rcc = dp.RCC.freeze(
            rcc::Config::hse(8.MHz())
                .sysclk(168_000_000u32.Hz())
                .pclk1(42.MHz()),
        );
        let gpioa = dp.GPIOA.split(&mut rcc);
        Serial::new(
            dp.USART2,
            (gpioa.pa2, gpioa.pa3),
            Config::default().baudrate(115_200.bps()).wordlength_8(),
            &mut rcc,
        )
        .unwrap()
        .with_u8_data()
    };
    #[cfg(feature = "stm32h723")]
    let serial = {
        let pwr = dp.PWR.constrain();
        let pwrcfg = pwr.ldo().vos0(&dp.SYSCFG).freeze();
        let rcc = dp.RCC.constrain();
        let ccdr = rcc
            .sys_ck(520.MHz())
            .pll1_strategy(PllConfigStrategy::Iterative)
            .hclk(260.MHz())
            .freeze(pwrcfg, &dp.SYSCFG);
        let gpioa = dp.GPIOA.split(ccdr.peripheral.GPIOA);
        let tx = gpioa.pa9.into_alternate();
        let rx = gpioa.pa10.into_alternate();
        dp.USART1
            .serial(
                (tx, rx),
                115_200.bps(),
                ccdr.peripheral.USART1,
                &ccdr.clocks,
            )
            .unwrap()
    };

    let mut cp = cortex_m::Peripherals::take().unwrap();
    cp.DCB.enable_trace();
    cp.DWT.enable_cycle_counter();

    unsafe {
        SERIAL = Some(serial);
    }

    // ISR infrastructure: TIM2 one-shot (1 MHz, update IRQ) for case 4, and
    // EXTI0 software-pend for case 10. Both at NVIC priority 15 so the FromISR
    // calls are syscall-safe.
    {
        let rcc = unsafe { &*pac::RCC::ptr() };
        apb1enr!(rcc).modify(|_, w| w.tim2en().set_bit());
        let tim2 = unsafe { &*pac::TIM2::ptr() };
        reg!(tim2, psc).write(|w| unsafe { w.psc().bits(TIM_PSC_1MHZ) }); // APB1 timer clock 84 MHz -> 1 MHz
        reg!(tim2, arr).write(|w| unsafe { w.arr().bits(1000 - 1) }); // 1 ms
        reg!(tim2, dier).write(|w| w.uie().set_bit());
        reg!(tim2, cr1).write(|w| w.opm().set_bit()); // one-pulse: stops after firing
        apb1enr!(rcc).modify(|_, w| w.tim3en().set_bit());
        let tim3 = unsafe { &*pac::TIM3::ptr() };
        reg!(tim3, psc).write(|w| unsafe { w.psc().bits(TIM_PSC_1MHZ) });
        reg!(tim3, arr).write(|w| unsafe { w.arr().bits(1000 - 1) });
        reg!(tim3, dier).write(|w| w.uie().set_bit());
        reg!(tim3, cr1).write(|w| w.opm().set_bit());
        apb1enr!(rcc).modify(|_, w| w.tim4en().set_bit());
        let tim4 = unsafe { &*pac::TIM4::ptr() };
        reg!(tim4, psc).write(|w| unsafe { w.psc().bits(TIM_PSC_1MHZ) });
        reg!(tim4, arr).write(|w| unsafe { w.arr().bits(50) }); // 50 us
        reg!(tim4, dier).write(|w| w.uie().set_bit());
        reg!(tim4, cr1).write(|w| w.opm().set_bit());
        // case 11: TIM5 (release, 750 us auto-reload) + TIM7 (load injector,
        // 137 us auto-reload). TIM5 gives a semaphore from ISR so it must sit
        // at the syscall-safe priority 15; TIM7 is a bare busy loop at priority
        // 1 so it can preempt even critical sections (no RTOS calls).
        apb1enr!(rcc).modify(|_, w| w.tim5en().set_bit());
        let tim5 = unsafe { &*pac::TIM5::ptr() };
        reg!(tim5, psc).write(|w| unsafe { w.psc().bits(0) });
        reg!(tim5, arr).write(|w| unsafe { w.arr().bits(PJIT_TIMER_ARR) });
        reg!(tim5, dier).write(|w| w.uie().set_bit());
        apb1enr!(rcc).modify(|_, w| w.tim7en().set_bit());
        let tim7 = unsafe { &*pac::TIM7::ptr() };
        reg!(tim7, psc).write(|w| unsafe { w.psc().bits(0) });
        reg!(tim7, arr).write(|w| unsafe { w.arr().bits(PJIT_LOAD_ARR as u16) });
        reg!(tim7, dier).write(|w| w.uie().set_bit());
        let mut cp = unsafe { cortex_m::Peripherals::steal() };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::TIM2, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::TIM3, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::TIM4, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::TIM5, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::TIM7, 0x10) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::EXTI0, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::EXTI1, 0xF0) };
        unsafe { cp.NVIC.set_priority(pac::Interrupt::EXTI2, 0x50) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::TIM2) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::TIM3) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::TIM4) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::TIM5) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::TIM7) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::EXTI0) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::EXTI1) };
        unsafe { cortex_m::peripheral::NVIC::unmask(pac::Interrupt::EXTI2) };
    }

    // Setup context: single-threaded, interrupts still masked.
    let context = unsafe { TaskContext::assume() };

    // Primitives.
    unsafe {
        BENCH_QUEUE = Some(Queue::<u32>::new(&context, 1).expect("bench queue"));
        HANDOFF_SEM = Some(BinarySemaphore::new(&context).expect("handoff sem"));
        HANDOFF_DONE = Some(BinarySemaphore::new(&context).expect("handoff done"));
        IPC_REQ = Some(Queue::<u32>::new(&context, 1).expect("ipc req"));
        IPC_RESP = Some(Queue::<u32>::new(&context, 1).expect("ipc resp"));
        SEM = Some(BinarySemaphore::new(&context).expect("sem"));
        MUTEX = Some(Mutex::new(&context, 0u32).expect("mutex"));
        EVG = Some(EventGroup::new(&context).expect("evg"));
        NOTIFY_DONE = Some(BinarySemaphore::new(&context).expect("notify done"));
        T26_GATE_HI = Some(BinarySemaphore::new(&context).expect("t26 gate hi"));
        T26_GATE_MID = Some(BinarySemaphore::new(&context).expect("t26 gate mid"));
        T26_GATE_LO = Some(BinarySemaphore::new(&context).expect("t26 gate lo"));
        T26_DONE = Some(CountingSemaphore::new(&context, 3, 0).expect("t26 done"));
        YIELD_GATE = Some(BinarySemaphore::new(&context).expect("yield gate"));
        TIMER_DONE = Some(BinarySemaphore::new(&context).expect("timer done"));
        TIMER = Some(Timer::new_oneshot(&context, c"Timer", 1, timer_callback).expect("timer"));
        PI_MUTEX = Some(Mutex::new(&context, 0u32).expect("pi mutex"));
        PI_L_HOLDS = Some(BinarySemaphore::new(&context).expect("pi l holds"));
        PI_L_RELEASE = Some(BinarySemaphore::new(&context).expect("pi l release"));
        PI_H_TRY = Some(BinarySemaphore::new(&context).expect("pi h try"));
        PI_H_GOT = Some(BinarySemaphore::new(&context).expect("pi h got"));
        PI_M_RUN = Some(BinarySemaphore::new(&context).expect("pi m run"));
        COPY_QUEUE = Some(Queue::<[u8; 256]>::new(&context, 1).expect("copy queue"));
        ZERO_QUEUE = Some(Queue::<usize>::new(&context, 1).expect("zero queue"));
        TP_QUEUE = Some(Queue::<[u8; 64]>::new(&context, 4).expect("tp queue"));
        IPC_MB_REQ = Some(MessageBuffer::new(&context, 264).expect("mb req"));
        IPC_MB_RESP = Some(MessageBuffer::new(&context, 264).expect("mb resp"));
        ISR_SEM = Some(BinarySemaphore::new(&context).expect("isr sem"));
        ISR_DONE = Some(BinarySemaphore::new(&context).expect("isr done"));
        PREEMPT_SEM = Some(BinarySemaphore::new(&context).expect("preempt sem"));
        PREEMPT_DONE = Some(BinarySemaphore::new(&context).expect("preempt done"));
        PREEMPT_LOW_GATE = Some(BinarySemaphore::new(&context).expect("preempt low gate"));
        PJIT_SEM = Some(BinarySemaphore::new(&context).expect("pjit sem"));
    }

    // Tasks (configMAX_PRIORITIES = 5, so priorities 0..4).
    unsafe {
        TaskHandle::spawn_static(
            &context,
            b"session\0",
            &mut SESSION_STACK,
            &mut SESSION_TCB,
            4 as UBaseType_t,
            session_task,
        )
        .expect("session task");
        TaskHandle::spawn_static(
            &context,
            b"handoff\0",
            &mut HANDOFF_STACK,
            &mut HANDOFF_TCB,
            2 as UBaseType_t,
            peer_handoff_task,
        )
        .expect("handoff task");
        TaskHandle::spawn_static(
            &context,
            b"notify\0",
            &mut NOTIFY_STACK,
            &mut NOTIFY_TCB,
            2 as UBaseType_t,
            peer_notify_task,
        )
        .expect("notify task");
        TaskHandle::spawn_static(
            &context,
            b"ipc\0",
            &mut IPC_STACK,
            &mut IPC_TCB,
            2 as UBaseType_t,
            peer_ipc_task,
        )
        .expect("ipc task");
        TaskHandle::spawn_static(
            &context,
            b"t26hi\0",
            &mut T26_HI_STACK,
            &mut T26_HI_TCB,
            3 as UBaseType_t,
            t26_hi,
        )
        .expect("t26 hi");
        TaskHandle::spawn_static(
            &context,
            b"t26mid\0",
            &mut T26_MID_STACK,
            &mut T26_MID_TCB,
            2 as UBaseType_t,
            t26_mid,
        )
        .expect("t26 mid");
        TaskHandle::spawn_static(
            &context,
            b"t26lo\0",
            &mut T26_LO_STACK,
            &mut T26_LO_TCB,
            1 as UBaseType_t,
            t26_lo,
        )
        .expect("t26 lo");
        TaskHandle::spawn_static(
            &context,
            b"yield\0",
            &mut YIELD_STACK,
            &mut YIELD_TCB,
            4 as UBaseType_t,
            yield_peer,
        )
        .expect("yield peer");
        TaskHandle::spawn_static(
            &context,
            b"pilow\0",
            &mut PI_LOW_STACK,
            &mut PI_LOW_TCB,
            1 as UBaseType_t,
            pi_low,
        )
        .expect("pi low");
        TaskHandle::spawn_static(
            &context,
            b"pimed\0",
            &mut PI_MED_STACK,
            &mut PI_MED_TCB,
            2 as UBaseType_t,
            pi_medium,
        )
        .expect("pi medium");
        TaskHandle::spawn_static(
            &context,
            b"pihigh\0",
            &mut PI_HIGH_STACK,
            &mut PI_HIGH_TCB,
            3 as UBaseType_t,
            pi_high,
        )
        .expect("pi high");
        TaskHandle::spawn_static(
            &context,
            b"tpcons\0",
            &mut TP_STACK,
            &mut TP_TCB,
            3 as UBaseType_t,
            tp_consumer,
        )
        .expect("tp consumer");
        TaskHandle::spawn_static(
            &context,
            b"mbpeer\0",
            &mut MB_STACK,
            &mut MB_TCB,
            2 as UBaseType_t,
            mb_peer,
        )
        .expect("mb peer");
        TaskHandle::spawn_static(
            &context,
            b"prelo\0",
            &mut PREEMPT_LOW_STACK,
            &mut PREEMPT_LOW_TCB,
            1 as UBaseType_t,
            preempt_low,
        )
        .expect("preempt low");
        TaskHandle::spawn_static(
            &context,
            b"prehi\0",
            &mut PREEMPT_HIGH_STACK,
            &mut PREEMPT_HIGH_TCB,
            3 as UBaseType_t,
            preempt_high,
        )
        .expect("preempt high");
        TaskHandle::spawn_static(
            &context,
            b"cpuwrk\0",
            &mut CPU_WORKER_STACK,
            &mut CPU_WORKER_TCB,
            2 as UBaseType_t,
            cpu_worker,
        )
        .expect("cpu worker");
    }

    start_scheduler(context)
}

// ---------------------------------------------------------------- peer tasks
extern "C" fn peer_handoff_task(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { HANDOFF_SEM.as_ref().unwrap() }.take(&ctx);
        HANDOFF_WAKE.store(now_ticks(), Ordering::SeqCst);
        unsafe { HANDOFF_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn peer_notify_task(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { EVG.as_ref().unwrap() }
            .wait_any_clear_timeout(&ctx, EVG_NOTIFY_BIT, u32::MAX)
            .unwrap();
        NOTIFY_WAKE.store(now_ticks(), Ordering::SeqCst);
        unsafe { NOTIFY_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn peer_ipc_task(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        let received_value = unsafe { IPC_REQ.as_ref().unwrap() }.receive(&ctx).unwrap();
        unsafe { IPC_RESP.as_ref().unwrap() }.send(&ctx, &received_value);
    }
}

extern "C" fn t26_hi(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { T26_GATE_HI.as_ref().unwrap() }.take(&ctx);
        let index = RUN_IDX.fetch_add(1, Ordering::SeqCst) as usize;
        RUN_ORDER[index].store(3, Ordering::SeqCst);
        unsafe { T26_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn t26_mid(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { T26_GATE_MID.as_ref().unwrap() }.take(&ctx);
        let index = RUN_IDX.fetch_add(1, Ordering::SeqCst) as usize;
        RUN_ORDER[index].store(2, Ordering::SeqCst);
        unsafe { T26_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn t26_lo(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { T26_GATE_LO.as_ref().unwrap() }.take(&ctx);
        let index = RUN_IDX.fetch_add(1, Ordering::SeqCst) as usize;
        RUN_ORDER[index].store(1, Ordering::SeqCst);
        unsafe { T26_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn yield_peer(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { YIELD_GATE.as_ref().unwrap() }.take(&ctx);
        delay(&ctx, 0);
    }
}

extern "C" fn timer_callback(_t: TimerHandle_t) {
    TIMER_FIRED.store(now_ticks(), Ordering::SeqCst);
    let ctx = unsafe { TaskContext::assume() };
    unsafe { TIMER_DONE.as_ref().unwrap() }.give(&ctx);
}

extern "C" fn pi_low(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        let _guard = unsafe { PI_MUTEX.as_ref().unwrap() }.lock(&ctx).unwrap();
        unsafe { PI_L_HOLDS.as_ref().unwrap() }.give(&ctx);
        unsafe { PI_L_RELEASE.as_ref().unwrap() }.take(&ctx);
    }
}

extern "C" fn pi_medium(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        // Block until the session lets the spin start, so L can acquire the
        // mutex first. Then spin CPU-bound for ~2 ms (ready the whole time).
        unsafe { PI_M_RUN.as_ref().unwrap() }.take(&ctx);
        let a = now_ticks();
        while now_ticks().wrapping_sub(a) < (HZ as u32 / 500) {}
    }
}

extern "C" fn pi_high(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { PI_H_TRY.as_ref().unwrap() }.take(&ctx);
        let a = now_ticks();
        let _guard = unsafe { PI_MUTEX.as_ref().unwrap() }.lock(&ctx).unwrap();
        PI_H_LATENCY.store(now_ticks().wrapping_sub(a), Ordering::SeqCst);
        unsafe { PI_H_GOT.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn tp_consumer(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { TP_QUEUE.as_ref().unwrap() }.receive(&ctx);
    }
}

extern "C" fn mb_peer(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    let mut buf = [0u8; 256];
    loop {
        let received_len = unsafe { IPC_MB_REQ.as_mut().unwrap() }.receive(&ctx, &mut buf);
        unsafe { IPC_MB_RESP.as_mut().unwrap() }.send(&ctx, &buf[..received_len]);
    }
}

#[interrupt]
fn EXTI0() {
    ISR_ENTRY.store(now_ticks(), Ordering::SeqCst);
    let mut woken: BaseType_t = 0;
    unsafe {
        ISR_DONE.as_ref().unwrap().give_from_isr(&mut woken);
    }
    portYIELD_FROM_ISR(woken);
}

#[interrupt]
fn TIM2() {
    let tim2 = unsafe { &*pac::TIM2::ptr() };
    reg!(tim2, sr).modify(|_, w| w.uif().clear_bit());
    ISR_GIVE.store(now_ticks(), Ordering::SeqCst);
    let mut woken: BaseType_t = 0;
    unsafe {
        ISR_SEM.as_ref().unwrap().give_from_isr(&mut woken);
    }
    portYIELD_FROM_ISR(woken);
}

#[interrupt]
fn TIM3() {
    let tim3 = unsafe { &*pac::TIM3::ptr() };
    reg!(tim3, sr).modify(|_, w| w.uif().clear_bit());
    PREEMPT_GIVE.store(now_ticks(), Ordering::SeqCst);
    let mut woken: BaseType_t = 0;
    unsafe {
        PREEMPT_SEM.as_ref().unwrap().give_from_isr(&mut woken);
    }
    portYIELD_FROM_ISR(woken);
}

#[interrupt]
fn EXTI1() {
    // Fixed-iteration loop (not wall-clock bounded), so a higher-priority
    // preemption adds its full duration to the measured elapsed time.
    let step = NESTED_SPIN / (NESTED_PENDS + 1);
    let mut next = step;
    let pend = !NESTED_CONTROL && NESTED_PEND_H.load(Ordering::SeqCst) != 0;
    let mut i = 0u32;
    while i < NESTED_SPIN {
        if pend && i == next && next <= NESTED_PENDS * step {
            cortex_m::peripheral::NVIC::pend(pac::Interrupt::EXTI2);
            next += step;
        }
        core::hint::black_box(i);
        i += 1;
    }
    NESTED_LOW_DONE.store(1, Ordering::SeqCst);
}

#[interrupt]
fn EXTI2() {
    NESTED_H_COUNT.fetch_add(1, Ordering::SeqCst);
}

#[interrupt]
fn TIM4() {
    let tim4 = unsafe { &*pac::TIM4::ptr() };
    reg!(tim4, sr).modify(|_, w| w.uif().clear_bit());
    MAXDIS_FIRE.store(now_ticks(), Ordering::SeqCst);
}

#[interrupt]
fn TIM5() {
    let tim5 = unsafe { &*pac::TIM5::ptr() };
    reg!(tim5, sr).modify(|_, w| w.uif().clear_bit());
    let release_ticks = now_ticks();
    let previous_releases = PJIT_RELEASES.fetch_add(1, Ordering::SeqCst);
    if previous_releases == 0 {
        PJIT_R0.store(release_ticks, Ordering::SeqCst);
    }
    let mut woken: BaseType_t = 0;
    unsafe {
        PJIT_SEM.as_ref().unwrap().give_from_isr(&mut woken);
    }
    portYIELD_FROM_ISR(woken);
}

#[interrupt]
fn TIM7() {
    let tim7 = unsafe { &*pac::TIM7::ptr() };
    reg!(tim7, sr).modify(|_, w| w.uif().clear_bit());
    pjit_load_body(PJIT_ITERS.load(Ordering::SeqCst));
}

extern "C" fn preempt_low(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { PREEMPT_LOW_GATE.as_ref().unwrap() }.take(&ctx);
        while PREEMPT_LOW_ARMED.load(Ordering::SeqCst) != 0 {}
    }
}

extern "C" fn preempt_high(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        unsafe { PREEMPT_SEM.as_ref().unwrap() }.take(&ctx);
        PREEMPT_ENTRY.store(now_ticks(), Ordering::SeqCst);
        unsafe { PREEMPT_DONE.as_ref().unwrap() }.give(&ctx);
    }
}

extern "C" fn cpu_worker(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    loop {
        let load = WORKER_LOAD.load(Ordering::SeqCst);
        if load == 0 {
            delay_ms(&ctx, 10);
            continue;
        }
        // Match the C reference: spin for pct% of a 10 ms period, then delay
        // for the remainder. The off time is `period - (period * pct) / 100`
        // (integer ms, rounding the on fraction down), not `(100 - pct)%`,
        // so the duty cycle matches the C worker exactly.
        let pct = load.min(100);
        let spin_cyc = (HZ / 100) * (pct as u64) / 100;
        let a = now_ticks();
        while (now_ticks().wrapping_sub(a) as u64) < spin_cyc {}
        let off_ms = 10 - (10 * pct) / 100;
        if off_ms > 0 {
            delay_ms(&ctx, off_ms);
        } else {
            delay(&ctx, 0);
        }
    }
}

// --------------------------------------------------------------- measure
#[inline(never)]
fn pjit_load_body(mut n: u32) {
    let mut a: u32 = 0;
    while n > 0 {
        n -= 1;
        // black_box the counter each iteration so LLVM cannot collapse the
        // loop into a closed-form sum (the C reference uses a `volatile`
        // accumulator for the same reason).
        a = a.wrapping_add(core::hint::black_box(n));
    }
    core::hint::black_box(a);
}

fn calibrate_pjit_load() -> u32 {
    let mut iters: u32 = 100;
    for _ in 0..64 {
        let load_ticks = cortex_m::interrupt::free(|_| {
            let start_ticks = now_ticks();
            pjit_load_body(iters);
            now_ticks().wrapping_sub(start_ticks)
        });
        if load_ticks >= PJIT_LOAD_TARGET && load_ticks < PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8 {
            break;
        }
        iters = if load_ticks == 0 {
            iters + 1
        } else {
            let next = ((iters as u64) * (PJIT_LOAD_TARGET as u64) / (load_ticks as u64)) as u32;
            if next == 0 {
                1
            } else {
                next
            }
        };
    }
    if iters == 0 {
        1
    } else {
        iters
    }
}

fn arm_pjit() {
    PJIT_ITERS.store(calibrate_pjit_load(), Ordering::SeqCst);
    PJIT_R0.store(0, Ordering::SeqCst);
    PJIT_RELEASES.store(0, Ordering::SeqCst);
    PJIT_LAST_RELEASES.store(0, Ordering::SeqCst);
    // Reset CNT by writing it directly (EGR.UG would set UIF and, since DIER.UIE
    // is already enabled, trigger a spurious release/load ISR that would record
    // a wrong R0 and give a stale semaphore).
    let tim5 = unsafe { &*pac::TIM5::ptr() };
    reg!(tim5, cr1).write(|w| w.cen().clear_bit());
    reg!(tim5, cnt).write(|w| unsafe { w.cnt().bits(0) });
    reg!(tim5, sr).write(|w| w.uif().clear_bit());
    reg!(tim5, cr1).write(|w| w.cen().set_bit());
    let tim7 = unsafe { &*pac::TIM7::ptr() };
    reg!(tim7, cr1).write(|w| w.cen().clear_bit());
    reg!(tim7, cnt).write(|w| unsafe { w.cnt().bits(0) });
    reg!(tim7, sr).write(|w| w.uif().clear_bit());
    reg!(tim7, cr1).write(|w| w.cen().set_bit());
    PJIT_ARMED.store(true, Ordering::SeqCst);
}

fn disarm_pjit() {
    let tim5 = unsafe { &*pac::TIM5::ptr() };
    reg!(tim5, cr1).write(|w| w.cen().clear_bit());
    let tim7 = unsafe { &*pac::TIM7::ptr() };
    reg!(tim7, cr1).write(|w| w.cen().clear_bit());
    PJIT_ARMED.store(false, Ordering::SeqCst);
}

fn measure(ctx: &TaskContext, id: u32, variant: u32) -> u64 {
    if id == 11 {
        return measure_case11(ctx);
    }
    measure_u32(ctx, id, variant) as u64
}

fn measure_case11(ctx: &TaskContext) -> u64 {
    if !PJIT_ARMED.load(Ordering::SeqCst) {
        arm_pjit();
    }
    let sem = unsafe { PJIT_SEM.as_ref().unwrap() };
    // Drain a stale give (accumulated during the batch flush) so the take below
    // blocks for a fresh release instead of returning a caught-up delta=0 sample.
    sem.try_take(ctx);
    // Re-anchor the release counter so the per-sample flush gap is not charged
    // to this sample; measure the activation error of the next release against
    // the ideal grid R0 + (rel-1)*P.
    PJIT_LAST_RELEASES.store(PJIT_RELEASES.load(Ordering::SeqCst), Ordering::SeqCst);
    sem.take(ctx);
    let wake = now_ticks() as u64;
    let rel = PJIT_RELEASES.load(Ordering::SeqCst);
    let delta = rel.wrapping_sub(PJIT_LAST_RELEASES.load(Ordering::SeqCst));
    if delta > 1 {
        return u64::MAX; // coalesced release (deadline miss) sentinel
    }
    let r0 = PJIT_R0.load(Ordering::SeqCst) as u64;
    let ideal = r0 + (rel as u64 - 1) * PJIT_P_CYCLES as u64;
    if wake < ideal {
        return u64::MAX;
    }
    wake - ideal
}

fn measure_u32(ctx: &TaskContext, id: u32, variant: u32) -> u32 {
    match id {
        0 => {
            let a = now_ticks();
            let b = now_ticks();
            b.wrapping_sub(a)
        }
        1 => {
            let q = unsafe { BENCH_QUEUE.as_ref().unwrap() };
            let a = now_ticks();
            q.try_send(ctx, &42);
            q.try_receive(ctx);
            now_ticks().wrapping_sub(a)
        }
        2 => {
            let t0 = now_ticks();
            unsafe { HANDOFF_SEM.as_ref().unwrap() }.give(ctx);
            unsafe { HANDOFF_DONE.as_ref().unwrap() }.take(ctx);
            let wake = HANDOFF_WAKE.load(Ordering::SeqCst);
            wake.wrapping_sub(t0)
        }
        3 => {
            let nominal = (HZ / 1000) as u32;
            // Anchor the grid and wake to a tick boundary first, then measure
            // the next aligned interval (the per-sample UART send otherwise
            // forces the grid to fall behind). A quiet tick-based path ~0.
            unsafe { PREV_WAKE = xTaskGetTickCount() };
            unsafe { xTaskDelayUntil(&mut PREV_WAKE, 1) };
            let a = now_ticks();
            // SAFETY: PREV_WAKE is a valid, non-aliased TickType_t; task context.
            unsafe { xTaskDelayUntil(&mut PREV_WAKE, 1) };
            let actual = now_ticks().wrapping_sub(a);
            if actual >= nominal {
                actual - nominal
            } else {
                nominal - actual
            }
        }
        5 => {
            let a = now_ticks();
            unsafe { YIELD_GATE.as_ref().unwrap() }.give(ctx);
            delay(ctx, 0);
            now_ticks().wrapping_sub(a)
        }
        7 => {
            let req = unsafe { IPC_REQ.as_ref().unwrap() };
            let resp = unsafe { IPC_RESP.as_ref().unwrap() };
            let a = now_ticks();
            req.send(ctx, &7);
            resp.receive(ctx);
            now_ticks().wrapping_sub(a)
        }
        8 => {
            // L holds the mutex; start M spinning (ready the whole time), then
            // release H to block on it (boosting L above M), then let L release.
            // H's lock latency should be a few context switches, far below M's
            // ~2 ms spin.
            unsafe { PI_L_HOLDS.as_ref().unwrap() }.take(ctx);
            unsafe { PI_M_RUN.as_ref().unwrap() }.give(ctx);
            unsafe { PI_H_TRY.as_ref().unwrap() }.give(ctx);
            unsafe { PI_L_RELEASE.as_ref().unwrap() }.give(ctx);
            unsafe { PI_H_GOT.as_ref().unwrap() }.take(ctx);
            PI_H_LATENCY.load(Ordering::SeqCst)
        }
        9 => {
            delay_ms(ctx, 1);
            let a = now_ticks();
            unsafe { TIMER.as_ref().unwrap() }.start(ctx);
            unsafe { TIMER_DONE.as_ref().unwrap() }.take(ctx);
            let fired = TIMER_FIRED.load(Ordering::SeqCst);
            let nominal = (HZ / 1000) as u32;
            let elapsed = fired.wrapping_sub(a);
            if elapsed >= nominal {
                elapsed - nominal
            } else {
                0
            }
        }
        12 => {
            let semaphore = unsafe { SEM.as_ref().unwrap() };
            let start_ticks = now_ticks();
            semaphore.give(ctx);
            semaphore.take(ctx);
            now_ticks().wrapping_sub(start_ticks)
        }
        13 => {
            let m = unsafe { MUTEX.as_ref().unwrap() };
            let a = now_ticks();
            {
                let _g = m.lock(ctx).unwrap();
            }
            now_ticks().wrapping_sub(a)
        }
        14 => {
            let a = now_ticks();
            // SAFETY: 64 bytes is a valid small allocation; the pointer is freed
            // exactly once and not used after.
            let p = unsafe { pvPortMalloc(64) };
            if !p.is_null() {
                unsafe { vPortFree(p) };
            }
            now_ticks().wrapping_sub(a)
        }
        15 => {
            // Align to a tick boundary first (the per-sample UART send would
            // otherwise desynchronize the phase and report the full 1 ms
            // quantization instead of the aligned wake residual).
            delay_ms(ctx, 1);
            let nominal = (HZ / 1000) as u32;
            let a = now_ticks();
            delay_ms(ctx, 1);
            let actual = now_ticks().wrapping_sub(a);
            if actual >= nominal {
                actual - nominal
            } else {
                nominal - actual
            }
        }
        16 => {
            let a = now_ticks();
            delay(ctx, 0);
            now_ticks().wrapping_sub(a)
        }
        17 => {
            let e = unsafe { EVG.as_ref().unwrap() };
            let t0 = now_ticks();
            e.set(ctx, EVG_NOTIFY_BIT);
            unsafe { NOTIFY_DONE.as_ref().unwrap() }.take(ctx);
            let wake = NOTIFY_WAKE.load(Ordering::SeqCst);
            wake.wrapping_sub(t0)
        }
        18 => {
            let size = (variant as usize).min(256);
            let mut data = [0u8; 256];
            for (i, b) in data.iter_mut().enumerate().take(size) {
                *b = (i & 0xFF) as u8;
            }
            let a = now_ticks();
            unsafe { IPC_MB_REQ.as_mut().unwrap() }.send(ctx, &data[..size]);
            let mut out = [0u8; 256];
            unsafe { IPC_MB_RESP.as_mut().unwrap() }.receive(ctx, &mut out);
            now_ticks().wrapping_sub(a)
        }
        19 => {
            // Push 1 MiB (16384 x 64-byte messages) through the queue, drained
            // by the consumer. Return total ticks; the analysis computes MiB/s.
            const N: usize = 16384;
            let q = unsafe { TP_QUEUE.as_ref().unwrap() };
            let a = now_ticks();
            for _ in 0..N {
                q.send(ctx, &[0u8; 64]);
            }
            now_ticks().wrapping_sub(a)
        }
        4 => {
            // Arm TIM2 one-shot (1 ms), block on ISR_SEM, measure the ISR->task
            // wake latency (task wake timestamp minus the ISR give timestamp).
            let tim2 = unsafe { &*pac::TIM2::ptr() };
            reg!(tim2, sr).modify(|_, w| w.uif().clear_bit());
            reg!(tim2, cr1).modify(|_, w| w.cen().set_bit());
            unsafe { ISR_SEM.as_ref().unwrap() }.take(ctx);
            let wake = now_ticks();
            let give = ISR_GIVE.load(Ordering::SeqCst);
            wake.wrapping_sub(give)
        }
        10 => {
            let a = now_ticks();
            cortex_m::peripheral::NVIC::pend(pac::Interrupt::EXTI0);
            unsafe { ISR_DONE.as_ref().unwrap() }.take(ctx);
            let entry = ISR_ENTRY.load(Ordering::SeqCst);
            entry.wrapping_sub(a)
        }
        6 => {
            // TIM3 one-shot releases the high task, which preempts the spinning
            // low task. Measure ISR give -> high-task entry. The low task is
            // released to spin only for this sample, then blocks again so the
            // idle task can run between samples.
            PREEMPT_LOW_ARMED.store(1, Ordering::SeqCst);
            unsafe { PREEMPT_LOW_GATE.as_ref().unwrap() }.give(ctx);
            let tim3 = unsafe { &*pac::TIM3::ptr() };
            reg!(tim3, sr).modify(|_, w| w.uif().clear_bit());
            reg!(tim3, cr1).modify(|_, w| w.cen().set_bit());
            unsafe { PREEMPT_DONE.as_ref().unwrap() }.take(ctx);
            let entry = PREEMPT_ENTRY.load(Ordering::SeqCst);
            let give = PREEMPT_GIVE.load(Ordering::SeqCst);
            PREEMPT_LOW_ARMED.store(0, Ordering::SeqCst);
            entry.wrapping_sub(give)
        }
        20 => {
            // Nested-minus-baseline: the low ISR loop runs with and without
            // pending the higher line; the difference isolates the nesting cost.
            let mut base = u32::MAX;
            let mut nested = u32::MAX;
            for _ in 0..3 {
                base = base.min(nested_run(0));
                nested = nested.min(nested_run(1));
            }
            nested.saturating_sub(base)
        }
        21 => {
            freertos_in_rust::trace::TICK_BODY_CYC.store(0, Ordering::SeqCst);
            delay_ms(ctx, 1);
            freertos_in_rust::trace::TICK_BODY_CYC.load(Ordering::SeqCst)
        }
        22 => {
            freertos_in_rust::trace::TICK_BODY_CYC.store(0, Ordering::SeqCst);
            let window_cyc = (HZ / 2) as u64;
            delay_ms(ctx, 500);
            let isr = freertos_in_rust::trace::TICK_BODY_CYC.load(Ordering::SeqCst) as u64;
            ((isr * 1_000_000) / window_cyc) as u32
        }
        23 => {
            let load = variant.max(25).min(100);
            WORKER_LOAD.store(load, Ordering::SeqCst);
            freertos_in_rust::trace::TICK_BODY_CYC.store(0, Ordering::SeqCst);
            let idle_before = ulTaskGetIdleRunTimeCounter();
            let t0 = now_ticks();
            delay_ms(ctx, 500); // 500 ms window (matches the C CPU_WINDOW_MS)
            let total = now_ticks().wrapping_sub(t0) as u64;
            let idle_after = ulTaskGetIdleRunTimeCounter();
            WORKER_LOAD.store(0, Ordering::SeqCst);
            let isr = freertos_in_rust::trace::TICK_BODY_CYC.load(Ordering::SeqCst) as u64;
            let idle_delta = idle_after.wrapping_sub(idle_before) as u64;
            // The idle counter includes the tick-ISR time; subtract it so only
            // genuine idle is credited (matching the C `true_idle`).
            let true_idle = idle_delta.saturating_sub(isr);
            let busy = total.saturating_sub(true_idle);
            ((busy * 1_000_000) / total) as u32
        }
        25 => {
            // Arm TIM4 (50 us one-shot), then mask interrupts and spin for a
            // bounded interval. TIM4 fires during the mask and its ISR runs only
            // after the mask is lifted; the extra delay is the disable time.
            let tim4 = unsafe { &*pac::TIM4::ptr() };
            reg!(tim4, sr).modify(|_, w| w.uif().clear_bit());
            MAXDIS_FIRE.store(0, Ordering::SeqCst);
            reg!(tim4, cr1).modify(|_, w| w.cen().set_bit());
            let a = now_ticks();
            let nominal = HZ as u32 / 20000; // 50 us
            portDISABLE_INTERRUPTS();
            let start = now_ticks();
            while now_ticks().wrapping_sub(start) < nominal {}
            portENABLE_INTERRUPTS();
            while MAXDIS_FIRE.load(Ordering::SeqCst) == 0 {}
            let fire = MAXDIS_FIRE.load(Ordering::SeqCst);
            fire.wrapping_sub(a).wrapping_sub(nominal)
        }
        24 => {
            // Fill the heap with 256-byte blocks until a margin of free heap
            // remains, free alternating ones to create non-adjacent holes, then
            // probe the largest single allocation with a malloc binary search.
            // The metric is 1e6*(1 - largest/total_free), as in the C reference.
            const BLOCK: usize = 256;
            let mut n = 0usize;
            while n < FRAG_MAX_BLOCKS && xPortGetFreeHeapSize() > FRAG_MARGIN {
                let p = unsafe { pvPortMalloc(BLOCK) };
                if p.is_null() {
                    break;
                }
                unsafe { FRAG_BLOCKS[n] = p };
                n += 1;
            }
            for i in (0..n).step_by(2) {
                unsafe {
                    vPortFree(FRAG_BLOCKS[i]);
                    FRAG_BLOCKS[i] = core::ptr::null_mut();
                }
            }
            let total_free = xPortGetFreeHeapSize();
            let mut lo = 0usize;
            let mut hi = total_free;
            while lo < hi {
                let mid = lo + (hi - lo + 1) / 2;
                let p = unsafe { pvPortMalloc(mid) };
                if p.is_null() {
                    hi = mid - 1;
                } else {
                    unsafe { vPortFree(p) };
                    lo = mid;
                }
            }
            for i in (1..n).step_by(2) {
                unsafe {
                    vPortFree(FRAG_BLOCKS[i]);
                    FRAG_BLOCKS[i] = core::ptr::null_mut();
                }
            }
            if total_free == 0 {
                0
            } else {
                let largest = lo.min(total_free);
                let diff = (total_free - largest) as u64;
                (diff * 1_000_000 / total_free as u64) as u32
            }
        }
        26 => {
            RUN_IDX.store(0, Ordering::SeqCst);
            unsafe { T26_GATE_LO.as_ref().unwrap() }.give(ctx);
            unsafe { T26_GATE_MID.as_ref().unwrap() }.give(ctx);
            unsafe { T26_GATE_HI.as_ref().unwrap() }.give(ctx);
            let done = unsafe { T26_DONE.as_ref().unwrap() };
            done.take(ctx);
            done.take(ctx);
            done.take(ctx);
            let ok = RUN_ORDER[0].load(Ordering::SeqCst) == 3
                && RUN_ORDER[1].load(Ordering::SeqCst) == 2
                && RUN_ORDER[2].load(Ordering::SeqCst) == 1;
            if ok {
                1
            } else {
                0
            }
        }
        27 => {
            // Copied leg: round-trip a 256-byte buffer through a queue.
            let a = now_ticks();
            unsafe { COPY_QUEUE.as_ref().unwrap() }.send(ctx, &[0u8; 256]);
            unsafe { COPY_QUEUE.as_ref().unwrap() }.receive(ctx);
            let copied = now_ticks().wrapping_sub(a);
            // Zero-copy leg: round-trip a pointer through a queue.
            let a = now_ticks();
            let p: usize = 0x2000_0000;
            unsafe { ZERO_QUEUE.as_ref().unwrap() }.send(ctx, &p);
            unsafe { ZERO_QUEUE.as_ref().unwrap() }.receive(ctx);
            let zero = now_ticks().wrapping_sub(a);
            copied.wrapping_sub(zero)
        }
        28 => crate::arena::measure_static(false),
        _ => 0,
    }
}

// ------------------------------------------------------------ UART I/O
fn wr(s: &mut SerialType, b: &[u8]) {
    for &x in b {
        let _ = nb::block!(s.write(x));
    }
}

fn rd(s: &mut SerialType) -> Option<u8> {
    nb::block!(s.read()).ok()
}

// ------------------------------------------------------------- protocol
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

fn send_ready(s: &mut SerialType, challenge: Option<&str>) {
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
    buf[p] = b'}';
    p += 1;
    buf[p] = b'\n';
    p += 1;
    wr(s, &buf[..p]);
}

fn send_started(s: &mut SerialType, run_id: &str) {
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
    wr(s, &buf[..p]);
}

fn send_measurement(
    s: &mut SerialType,
    run_id: &str,
    case: u32,
    index: u32,
    ticks: u64,
    variant: u32,
) {
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
    wr(s, &buf[..p]);
}

/* -------------------------------------------------- runtime memory fields */

fn put(buf: &mut [u8], p: &mut usize, part: &str) {
    buf[*p..*p + part.len()].copy_from_slice(part.as_bytes());
    *p += part.len();
}

extern "C" fn mem_probe_task(_arg: *mut core::ffi::c_void) {
    unsafe {
        loop {
            freertos_in_rust::kernel::tasks::vTaskDelay(TickType_t::MAX);
        }
    }
}

unsafe fn per_task_ram_overhead() -> u64 {
    let stack_bytes = 192usize * core::mem::size_of::<StackType_t>();
    let before = xPortGetFreeHeapSize();
    let mut handle: TaskHandle_t = core::ptr::null_mut();
    let create_status = xTaskCreate(
        mem_probe_task,
        b"memprobe\0".as_ptr(),
        192,
        core::ptr::null_mut(),
        1,
        &mut handle,
    );
    let after = xPortGetFreeHeapSize();
    if create_status == 1 && before > after {
        vTaskDelete(handle);
        vTaskDelay(2);
        let delta = before - after;
        if delta > stack_bytes {
            return (delta - stack_bytes) as u64;
        }
    }
    0
}

unsafe fn collect_memory() -> (u64, u64, u64, u64, u64, u64, u64, u64) {
    let heap_min = xPortGetMinimumEverFreeHeapSize() as u64;
    let heap_free = xPortGetFreeHeapSize() as u64;
    let before = xPortGetFreeHeapSize();
    let p = pvPortMalloc(1);
    let heap_over = if !p.is_null() {
        let after = xPortGetFreeHeapSize();
        vPortFree(p);
        if before > after {
            (before - after - 1) as u64
        } else {
            0
        }
    } else {
        0
    };
    let task_count = uxTaskGetNumberOfTasks() as u64;
    let stack_self = uxTaskGetStackHighWaterMark(core::ptr::null_mut()) as u64
        * core::mem::size_of::<StackType_t>() as u64;
    let mut arr: [TaskStatus_t; 16] = core::array::from_fn(|_| TaskStatus_t::new());
    let cap = if (task_count as usize) < 16 {
        task_count as usize
    } else {
        16
    };
    let enumerated_tasks =
        uxTaskGetSystemState(arr.as_mut_ptr(), cap as UBaseType_t, core::ptr::null_mut());
    let mut worst = 0u64;
    let mut best = 0u64;
    for i in 0..enumerated_tasks as usize {
        let f = uxTaskGetStackHighWaterMark(arr[i].xHandle) as u64
            * core::mem::size_of::<StackType_t>() as u64;
        if i == 0 || f < worst {
            worst = f;
        }
        if i == 0 || f > best {
            best = f;
        }
    }
    let per_task = per_task_ram_overhead();
    if worst == 0 && best == 0 {
        /* Per-task enumeration returned no usable marks; report the benchmark
         * task's own headroom rather than a misleading zero. */
        worst = stack_self;
        best = stack_self;
    }
    (
        heap_over, heap_min, heap_free, task_count, stack_self, worst, best, per_task,
    )
}

fn send_complete(s: &mut SerialType, run_id: &str, cases: &[u32], reps: u32) {
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
    let (heap_over, heap_min, heap_free, task_count, stack_self, worst, best, per_task) =
        unsafe { collect_memory() };
    put(
        &mut buf,
        &mut p,
        "],\"status\":\"ok\",\"stack_high_water_bytes\":",
    );
    p = fmt_u64(stack_self, &mut buf, p);
    put(&mut buf, &mut p, ",\"heap_min_free_bytes\":");
    p = fmt_u64(heap_min, &mut buf, p);
    put(&mut buf, &mut p, ",\"heap_free_bytes\":");
    p = fmt_u64(heap_free, &mut buf, p);
    put(&mut buf, &mut p, ",\"heap_overhead_bytes\":");
    p = fmt_u64(heap_over, &mut buf, p);
    put(&mut buf, &mut p, ",\"per_task_ram_overhead_bytes\":");
    p = fmt_u64(per_task, &mut buf, p);
    put(&mut buf, &mut p, ",\"task_count\":");
    p = fmt_u64(task_count, &mut buf, p);
    put(&mut buf, &mut p, ",\"worst_stack_headroom_bytes\":");
    p = fmt_u64(worst, &mut buf, p);
    put(&mut buf, &mut p, ",\"best_stack_headroom_bytes\":");
    p = fmt_u64(best, &mut buf, p);
    put(&mut buf, &mut p, "}\n");
    wr(s, &buf[..p]);
}

fn send_error(s: &mut SerialType, code: &str, msg: &str) {
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
    wr(s, &buf[..p]);
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

fn run_session(
    ctx: &TaskContext,
    s: &mut SerialType,
    run_id: &str,
    cases: &[u32],
    reps: u32,
    warmup: u32,
) {
    send_started(s, run_id);
    for &c in cases {
        let variant_of = |i: u32| -> u32 {
            if c == 18 {
                IPC_PAYLOAD[(i as usize) % IPC_PAYLOAD.len()]
            } else if c == 23 {
                CPU_LOAD[(i as usize) % CPU_LOAD.len()]
            } else {
                0
            }
        };
        for i in 0..warmup {
            let variant = variant_of(i);
            let _ = measure(ctx, c, variant);
        }
        if c == 11 {
            // Case 11 measures the free-running 750 us release grid, so the task
            // must stay locked to it. A per-sample UART send would force the grid
            // behind and flatten the activation error, so buffer 100 samples and
            // flush them in one burst (matching the C runner's flush cadence).
            let mut buf = [0u64; 100];
            let mut n = 0usize;
            for i in 0..reps {
                buf[n] = measure(ctx, c, 0);
                n += 1;
                if n == buf.len() {
                    for j in 0..n {
                        send_measurement(s, run_id, c, i + 1 - n as u32 + j as u32, buf[j], 0);
                    }
                    n = 0;
                }
            }
            for j in 0..n {
                send_measurement(s, run_id, c, reps - n as u32 + j as u32, buf[j], 0);
            }
            disarm_pjit();
        } else {
            for i in 0..reps {
                let variant = variant_of(i);
                let t = measure(ctx, c, variant);
                send_measurement(s, run_id, c, i, t, variant);
            }
        }
    }
    send_complete(s, run_id, cases, reps);
}

// ---------------------------------------------------------------- session
extern "C" fn session_task(_p: *mut c_void) {
    let ctx = unsafe { TaskContext::assume() };
    let uart = serial();

    send_ready(uart, None);

    let mut line = [0u8; LINE_MAX];
    let mut len = 0usize;
    loop {
        let byte = match rd(uart) {
            Some(byte) => byte,
            None => continue,
        };
        if byte == b'\r' {
            continue;
        }
        if byte == b'\n' {
            if len > 0 {
                if let Ok(line_str) = core::str::from_utf8(&line[..len]) {
                    if line_str.contains("\"type\":\"hello\"") {
                        let ch = find_str(line_str, "\"challenge\"");
                        send_ready(uart, ch);
                    } else if line_str.contains("\"type\":\"start\"") {
                        let run_id = match find_str(line_str, "\"run_id\"") {
                            Some(r) if r.len() < RUN_ID_MAX => r,
                            _ => {
                                send_error(uart, "run_id", "missing run_id");
                                len = 0;
                                continue;
                            }
                        };
                        let (cases, case_count) = parse_cases(line_str);
                        let repetitions = find_u32(line_str, "\"repetitions\"").unwrap_or(0);
                        let warmup = find_u32(line_str, "\"warmup\"").unwrap_or(0);
                        if case_count == 0 || repetitions == 0 {
                            send_error(uart, "start", "no cases or zero repetitions");
                        } else if cases[..case_count].iter().any(|byte| !advertised(*byte)) {
                            send_error(uart, "unsupported", "case not advertised");
                        } else {
                            run_session(
                                &ctx,
                                uart,
                                run_id,
                                &cases[..case_count],
                                repetitions,
                                warmup,
                            );
                        }
                    }
                }
            }
            len = 0;
            continue;
        }
        if len + 1 < LINE_MAX {
            line[len] = byte;
            len += 1;
        } else {
            len = 0;
        }
    }
}
