/* Minimal CMSIS-style startup and vector table for the nRF52840 (Cortex-M4F).
 *
 * The nRF52840 boots on the 64 MHz HFINT clock, so no clock setup is needed;
 * SystemInit is a no-op. The FreeRTOS Cortex-M4F port supplies the SVC, PendSV
 * and SysTick entry points. Case-specific IRQs (TIMER1 for case 4, SWI0/EGU0 for
 * case 10) are defined in bench.c. */
#include <stdint.h>

#define IRQ(n) (16 + (n))

extern uint32_t _sdata, _edata, _etext, _sbss, _ebss, _estack;

extern int main(void);
void Reset_Handler(void);
void Default_Handler(void) {
    for (;;) {
    }
}

/* FreeRTOS port entry points. */
extern void vPortSVCHandler(void);
extern void xPortPendSVHandler(void);
extern void xPortSysTickHandler(void);
/* Benchmark IRQs (bench.c). */
extern void TIMER1_IRQHandler(void);
extern void TIMER2_IRQHandler(void);
extern void TIMER3_IRQHandler(void);
extern void TIMER4_IRQHandler(void);
extern void SWI0_EGU0_IRQHandler(void);
extern void SWI1_EGU1_IRQHandler(void);
extern void SWI2_EGU2_IRQHandler(void);

/* CPACR at 0xE000ED88: enable CP10/CP11 so the hard-float port runs. */
#define CPACR (*(volatile uint32_t *)0xE000ED88UL)

uint32_t SystemCoreClock = 64000000UL;
void SystemInit(void) {
}

/* Vector table: word 0 is the initial stack pointer, not a function. Casting
 * handler addresses to uint32_t is the portable form on 32-bit ARM. */
__attribute__((section(".isr_vector"), used))
const uint32_t g_isr_vector[] = {
    (uint32_t)&_estack,
    (uint32_t)Reset_Handler,
    (uint32_t)Default_Handler, /* NMI */
    (uint32_t)Default_Handler, /* HardFault */
    (uint32_t)Default_Handler, /* MemManage */
    (uint32_t)Default_Handler, /* BusFault */
    (uint32_t)Default_Handler, /* UsageFault */
    0, 0, 0, 0,
    (uint32_t)vPortSVCHandler,
    (uint32_t)Default_Handler, /* DebugMon */
    0,
    (uint32_t)xPortPendSVHandler,
    (uint32_t)xPortSysTickHandler,
    /* IRQ 0..47 (nRF52840). Only TIMER1 (9) and SWI0/EGU0 (20) are used. */
    [IRQ(0)] = (uint32_t)Default_Handler,
    [IRQ(1)] = (uint32_t)Default_Handler,
    [IRQ(2)] = (uint32_t)Default_Handler,
    [IRQ(3)] = (uint32_t)Default_Handler,
    [IRQ(4)] = (uint32_t)Default_Handler,
    [IRQ(5)] = (uint32_t)Default_Handler,
    [IRQ(6)] = (uint32_t)Default_Handler,
    [IRQ(7)] = (uint32_t)Default_Handler,
    [IRQ(8)] = (uint32_t)Default_Handler,
    [IRQ(9)] = (uint32_t)TIMER1_IRQHandler,
    [IRQ(10)] = (uint32_t)TIMER2_IRQHandler, /* case 11 release */
    /* nRF52840: TIMER3 is IRQ 26 (IRQ 11 is RTC0). */
    [IRQ(11)] = (uint32_t)Default_Handler,
    [IRQ(26)] = (uint32_t)TIMER3_IRQHandler, /* case 11 injected load */
    /* nRF52840: TIMER4 is IRQ 27 (case 25 masked-interrupt probe). */
    [IRQ(27)] = (uint32_t)TIMER4_IRQHandler,
    [IRQ(12)] = (uint32_t)Default_Handler,
    [IRQ(13)] = (uint32_t)Default_Handler,
    [IRQ(14)] = (uint32_t)Default_Handler,
    [IRQ(15)] = (uint32_t)Default_Handler,
    [IRQ(16)] = (uint32_t)Default_Handler,
    [IRQ(17)] = (uint32_t)Default_Handler,
    [IRQ(18)] = (uint32_t)Default_Handler,
    [IRQ(19)] = (uint32_t)Default_Handler,
    [IRQ(20)] = (uint32_t)SWI0_EGU0_IRQHandler,
    [IRQ(21)] = (uint32_t)SWI1_EGU1_IRQHandler, /* case 20 low ISR */
    [IRQ(22)] = (uint32_t)SWI2_EGU2_IRQHandler, /* case 20 high ISR */
    [IRQ(23)] = (uint32_t)Default_Handler,
    [IRQ(24)] = (uint32_t)Default_Handler,
    [IRQ(25)] = (uint32_t)Default_Handler,
    [IRQ(28)] = (uint32_t)Default_Handler,
    [IRQ(29)] = (uint32_t)Default_Handler,
    [IRQ(30)] = (uint32_t)Default_Handler,
    [IRQ(31)] = (uint32_t)Default_Handler,
    [IRQ(32)] = (uint32_t)Default_Handler,
    [IRQ(33)] = (uint32_t)Default_Handler,
    [IRQ(34)] = (uint32_t)Default_Handler,
    [IRQ(35)] = (uint32_t)Default_Handler,
    [IRQ(36)] = (uint32_t)Default_Handler,
    [IRQ(37)] = (uint32_t)Default_Handler,
    [IRQ(38)] = (uint32_t)Default_Handler,
    [IRQ(39)] = (uint32_t)Default_Handler,
    [IRQ(40)] = (uint32_t)Default_Handler,
    [IRQ(41)] = (uint32_t)Default_Handler,
    [IRQ(42)] = (uint32_t)Default_Handler,
    [IRQ(43)] = (uint32_t)Default_Handler,
    [IRQ(44)] = (uint32_t)Default_Handler,
    [IRQ(45)] = (uint32_t)Default_Handler,
    [IRQ(46)] = (uint32_t)Default_Handler,
    [IRQ(47)] = (uint32_t)Default_Handler,
};

void Reset_Handler(void) {
    uint32_t *src = &_etext;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) {
        *dst++ = *src++;
    }
    for (dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0;
    }
    CPACR |= (0xFUL << 20);
    __asm volatile("dsb");
    __asm volatile("isb");
    SystemInit();
    (void)main();
    for (;;) {
    }
}
