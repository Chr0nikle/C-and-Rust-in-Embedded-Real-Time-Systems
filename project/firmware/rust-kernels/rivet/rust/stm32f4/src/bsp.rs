//! Board support for the STM32F407 Discovery (STM32F407VGTx) folded into
//! this benchmark binary. Console is USART2 (PA2 TX / PA3 RX) through the
//! external CP2102 on the Discovery. Runs at 168 MHz (HSE 8 MHz -> PLL:
//! M=8, N=336, P=2) so the cycle counts are comparable to the other F4
//! paths; APB1 = 42 MHz, APB2 = 84 MHz. Adapted from `rivet-bsp-stm32f401re`.

/// Board IRQ number map: which NVIC IRQ number is which peripheral.
pub mod irq {
    pub const USART2: u32 = 38;
}

// Case 21/22/23 instrumentation: bracket the SysTick body (the kernel's
// periodic tick handler) with DWT reads. `TICK_BODY_CYCLES` holds the last
// body duration (case 21); `TICK_ACCUM_CYCLES` accumulates it (case 22);
// `TICK_SEQ` counts ticks so a waiter can detect the next one.
pub static TICK_BODY_CYCLES: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
pub static TICK_ACCUM_CYCLES: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);
pub static TICK_SEQ: core::sync::atomic::AtomicU32 = core::sync::atomic::AtomicU32::new(0);

mod board {
    use core::sync::atomic::{AtomicU32, Ordering};

    const SYSCLK_HZ: u32 = 168_000_000;
    const PCLK1_HZ: u32 = 42_000_000;

    /// Switch from the reset-state 16 MHz HSI to 168 MHz from the 8 MHz HSE
    /// crystal (PLL M=8, N=336, P=2), with 5 flash wait states and APB1=/4,
    /// APB2=/2.
    fn clock_init() {
        unsafe {
            let rcc = &*stm32f4::stm32f407::RCC::ptr();
            let flash = &*stm32f4::stm32f407::FLASH::ptr();
            rcc.cr().modify(|_, w| w.hseon().set_bit());
            while rcc.cr().read().hserdy().bit_is_clear() {}
            flash.acr().modify(|_, w| {
                w.latency()
                    .bits(5)
                    .prften()
                    .set_bit()
                    .icen()
                    .set_bit()
                    .dcen()
                    .set_bit()
            });
            rcc.pllcfgr().write(|w| {
                w.pllm()
                    .bits(8)
                    .plln()
                    .bits(336)
                    .pllp()
                    .bits(0b00)
                    .pllsrc()
                    .set_bit()
            });
            rcc.cr().modify(|_, w| w.pllon().set_bit());
            while rcc.cr().read().pllrdy().bit_is_clear() {}
            rcc.cfgr().modify(|_, w| {
                w.hpre()
                    .bits(0b0000)
                    .ppre1()
                    .bits(0b101)
                    .ppre2()
                    .bits(0b100)
            });
            rcc.cfgr().modify(|_, w| w.sw().bits(0b10));
            while rcc.cfgr().read().sws().bits() != 0b10 {}
        }
    }

    fn uart_irq_handler() {
        let usart2 = unsafe { &*stm32f4::stm32f407::USART2::ptr() };
        loop {
            let sr = usart2.sr().read();
            let cr1 = usart2.cr1().read();
            if sr.txe().bit_is_set() && cr1.txeie().bit_is_set() {
                match rivet::console::tx_irq_next_byte() {
                    Some(b) => {
                        usart2.dr().write(|w| unsafe { w.dr().bits(b as u16) });
                    }
                    None => {
                        usart2.cr1().modify(|_, w| w.txeie().clear_bit());
                    }
                }
            } else if sr.rxne().bit_is_set() && cr1.rxneie().bit_is_set() {
                let b = usart2.dr().read().dr().bits() as u8;
                rivet::console::on_rx_byte(b);
            } else {
                break;
            }
        }
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_console_kick_tx() {
        unsafe {
            (&*stm32f4::stm32f407::USART2::ptr())
                .cr1()
                .modify(|_, w| w.txeie().set_bit());
        }
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_init() {
        clock_init();
        unsafe {
            let rcc = &*stm32f4::stm32f407::RCC::ptr();
            rcc.ahb1enr().modify(|_, w| w.gpioaen().set_bit());
            rcc.apb1enr().modify(|_, w| w.usart2en().set_bit());

            let gpioa = &*stm32f4::stm32f407::GPIOA::ptr();
            gpioa
                .moder()
                .modify(|_, w| w.moder2().bits(0b10).moder3().bits(0b10));
            gpioa
                .afrl()
                .modify(|_, w| w.afrl2().bits(7).afrl3().bits(7));

            let usart2 = &*stm32f4::stm32f407::USART2::ptr();
            let usartdiv = (PCLK1_HZ * 100) / (16 * 115_200);
            let mantissa = usartdiv / 100;
            let fraction = ((usartdiv % 100) * 16 + 50) / 100;
            usart2.brr().write(|w| {
                w.div_mantissa()
                    .bits(mantissa as u16)
                    .div_fraction()
                    .bits(fraction as u8)
            });
            usart2
                .cr1()
                .write(|w| w.ue().set_bit().te().set_bit().re().set_bit());

            rivet::irq::register_untraced(super::irq::USART2, uart_irq_handler).unwrap();
            rivet::irq::set_priority(super::irq::USART2, 0xFF);
            rivet::irq::enable(super::irq::USART2);
            usart2.cr1().modify(|_, w| w.rxneie().set_bit());
            rivet::console::enable_irq_tx();
        }
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_now_us() -> u64 {
        rivet_arch_cortex_m::systick::now_micros_precise()
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_tick_start(hz: u32) {
        let reload_ticks = SYSCLK_HZ / hz;
        let period_us = 1_000_000 / hz;
        rivet_arch_cortex_m::systick::init(reload_ticks, period_us);
    }

    #[no_mangle]
    unsafe extern "Rust" fn __rivet_board_console_write(ptr: *const u8, len: usize) {
        let bytes = unsafe { core::slice::from_raw_parts(ptr, len) };
        let usart2 = unsafe { &*stm32f4::stm32f407::USART2::ptr() };
        for &b in bytes {
            while usart2.sr().read().txe().bit_is_clear() {
                core::hint::spin_loop();
            }
            usart2.dr().write(|w| unsafe { w.dr().bits(b as u16) });
        }
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_reset() -> ! {
        rivet_arch_cortex_m::system_reset()
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_exit(code: u32) -> ! {
        if code == 0 {
            rivet::console::write_str("\nRIVET_EXIT_OK\n");
        } else {
            rivet::console::write_str("\nRIVET_FAILURE code=");
            print_dec(code);
            rivet::console::write_str("\n");
        }
        rivet::console::flush_sync();
        loop {
            core::hint::spin_loop();
        }
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_wdt_init(_period_us: u32) {
        // IWDG left unused for the benchmark (no watchdog on this path).
    }

    #[no_mangle]
    extern "Rust" fn __rivet_board_wdt_feed() {}

    #[no_mangle]
    extern "Rust" fn __rivet_board_wdt_check() {}

    static WDT_PERIOD_US: AtomicU32 = AtomicU32::new(0);
    #[allow(dead_code)]
    fn _wdt() {
        let _ = WDT_PERIOD_US.load(Ordering::Relaxed);
    }

    #[no_mangle]
    pub unsafe extern "C" fn SysTick() {
        let enter = rivet::port::arch::cycle_count() as u32;
        rivet_arch_cortex_m::systick::handler();
        let body = (rivet::port::arch::cycle_count() as u32).wrapping_sub(enter);
        super::TICK_BODY_CYCLES.store(body, Ordering::Relaxed);
        super::TICK_ACCUM_CYCLES.fetch_add(body, Ordering::Relaxed);
        super::TICK_SEQ.fetch_add(1, Ordering::Relaxed);
    }

    fn print_dec(mut n: u32) {
        if n == 0 {
            rivet::console::write_str("0");
            return;
        }
        let mut digits = [0u8; 10];
        let mut i = 0;
        while n > 0 {
            digits[i] = b'0' + (n % 10) as u8;
            n /= 10;
            i += 1;
        }
        let mut buf = [0u8; 10];
        for j in 0..i {
            buf[j] = digits[i - 1 - j];
        }
        if let Ok(s) = core::str::from_utf8(&buf[..i]) {
            rivet::console::write_str(s);
        }
    }
}
