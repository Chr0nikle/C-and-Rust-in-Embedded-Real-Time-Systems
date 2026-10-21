/* NuttX interrupt-to-task harness for benchmark case 4.
 *
 * STM32F4: TIM2 is armed in one-pulse mode. Its update interrupt records the
 * DWT cycle count on ISR entry and posts a semaphore; the benchmark task blocks
 * on the semaphore and reports the ISR-entry to awakened-task delta. TIM2 is not
 * used by the NuttX system timer (SysTick), so it is free here.
 *
 * ESP32-C6: the system tick uses SYSTIMER alarm 0; this harness uses the free
 * alarm 1 (target 1) the same way and timestamps with the C6 performance
 * counter (CSR 0x7e2) so the delta shares the port's tick base.
 *
 * This file is deliberately separate from port.c: port.c is host syntax-checked
 * without NuttX headers, while this harness needs the NuttX IRQ/semaphore API
 * and direct peripheral access. */
#include "rtos_bench.h"

#if defined(CONFIG_ARCH_CHIP_ESP32C6)

#include <nuttx/irq.h>
#include <nuttx/arch.h>
#include <nuttx/clock.h>
#include <nuttx/semaphore.h>

/* The C6 system tick drives SYSTIMER alarm 0 (target 0). On this single-core
 * part alarm 1 (target 1, IRQ source 58) is free, so case 4 uses it as an
 * independent one-shot interrupt source. The C6 performance counter (CSR 0x7e2,
 * 160 MHz, via port.c) timestamps ISR entry and task wake, sharing the port's
 * tick base.
 *
 * The registers are defined here rather than pulled from the arch/HAL headers:
 * this app-local file does not inherit those include paths, and case 4 needs
 * only the handful of SYSTIMER fields below (offsets from the ESP32-C6
 * systimer_reg.h). */
#define C6_SYSTIMER_BASE          0x6000A000UL
#define C6_SYSTIMER_CONF          (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x00))
#define C6_SYSTIMER_UNIT0_OP      (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x04))
#define C6_SYSTIMER_TARGET1_HI    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x24))
#define C6_SYSTIMER_TARGET1_LO    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x28))
#define C6_SYSTIMER_TARGET1_CONF  (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x38))
#define C6_SYSTIMER_UNIT0_HI      (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x40))
#define C6_SYSTIMER_UNIT0_LO      (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x44))
#define C6_SYSTIMER_COMP1_LOAD    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x54))
#define C6_SYSTIMER_INT_ENA       (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x64))
#define C6_SYSTIMER_INT_CLR       (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x6c))

#define C6_TARGET1_WORK_EN   (1u << 23) /* SYSTIMER_CONF[23] */
#define C6_UNIT0_UPDATE      (1u << 30) /* SYSTIMER_UNIT0_OP[30] write trigger */
#define C6_UNIT0_VALID       (1u << 29) /* SYSTIMER_UNIT0_OP[29] read: valid */
#define C6_TARGET1_ONESHOT   (0u << 30) /* SYSTIMER_TARGET1_CONF[30] */
#define C6_TARGET1_UNIT0     (0u << 31) /* SYSTIMER_TARGET1_CONF[31] */
#define C6_COMP1_LOAD_SYNC   (1u << 0)  /* SYSTIMER_COMP1_LOAD[0] */
#define C6_TARGET1_INT_BIT   (1u << 1)  /* SYSTIMER_INT_ENA/CLR[1] */

/* SYSTIMER_TARGET1_EDGE_INTR_SOURCE and ESP_IRQ_FIRSTPERIPH (16 + 1) from the
 * pinned arch headers. */
#define C6_SYSTIMER_TARGET1_SOURCE 58
#define C6_SYSTIMER_TARGET1_IRQ    (C6_SYSTIMER_TARGET1_SOURCE + 17)
/* 100 us at the 16 MHz SYSTIMER tick. The trigger delay does not affect the
 * ISR-entry-to-task measurement, so an approximate target is fine. */
#define ISR_ALARM_TICKS 1600u

extern uint64_t nuttx_bench_c6_now_ticks(void);
/* NuttX espressif interrupt-matrix setup, declared locally to keep this
 * app-local harness free of arch/HAL headers. Returns the CPU interrupt
 * (>= 0) or a negative error. */
extern int esp_setup_irq(int source, int priority, int type,
                         int (*handler)(int, void *, void *), void *arg);
extern int esp_teardown_irq(int source, int cpuint);

static sem_t             s_isr_sem;
static volatile uint64_t s_isr_entry;
static int               s_isr_active;
static volatile int      s_isr_armed;
static int               s_isr_cpuint = -1;

/* Latch and read counter 0 with the HAL's validity/tearing safeguards:
 * trigger an update, wait for the valid bit, then read LO/HI/LO and retry if
 * LO changed (a torn read would otherwise put the target ~2^32 ticks away). */
static int systimer_now(uint64_t *out)
{
    for (int attempt = 0; attempt < 2; attempt++) {
        uint32_t lo;
        uint32_t hi;
        uint32_t lo2;
        int spin = 0;

        C6_SYSTIMER_UNIT0_OP = C6_UNIT0_UPDATE;
        while ((C6_SYSTIMER_UNIT0_OP & C6_UNIT0_VALID) == 0) {
            if (++spin > 1000) {
                return -1;
            }
        }
        lo = C6_SYSTIMER_UNIT0_LO;
        hi = C6_SYSTIMER_UNIT0_HI;
        lo2 = C6_SYSTIMER_UNIT0_LO;
        if (lo == lo2) {
            *out = ((uint64_t)hi << 32) | lo;
            return 0;
        }
    }
    return -1;
}

static int systimer_t1_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;

    /* Mask the source first so a relatch cannot re-enter this handler (it runs
     * at the same priority as the tick and the USB console), then deassert the
     * level and acknowledge. */
    C6_SYSTIMER_INT_ENA &= ~C6_TARGET1_INT_BIT;
    C6_SYSTIMER_CONF &= ~C6_TARGET1_WORK_EN;
    (void)C6_SYSTIMER_CONF; /* flush posted MMIO writes */
    C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
    if (!s_isr_armed) {
        return 0; /* spurious re-entry: do not post */
    }
    s_isr_armed = 0;
    s_isr_entry = nuttx_bench_c6_now_ticks();
    (void)nxsem_post(&s_isr_sem);
    return 0;
}

static int isr_arm(void)
{
    uint64_t now;
    uint64_t target;

    /* Disable before retargeting; clamp into the future. */
    C6_SYSTIMER_CONF &= ~C6_TARGET1_WORK_EN;
    C6_SYSTIMER_INT_ENA &= ~C6_TARGET1_INT_BIT;
    if (systimer_now(&now) != 0) {
        return -1;
    }
    target = now + ISR_ALARM_TICKS;
    if (target <= now + 2) {
        target = now + 2;
    }
    C6_SYSTIMER_TARGET1_CONF = C6_TARGET1_ONESHOT | C6_TARGET1_UNIT0;
    C6_SYSTIMER_TARGET1_HI = (uint32_t)(target >> 32);
    C6_SYSTIMER_TARGET1_LO = (uint32_t)(target & 0xffffffffu);
    C6_SYSTIMER_COMP1_LOAD = C6_COMP1_LOAD_SYNC;
    C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
    while (nxsem_trywait(&s_isr_sem) == 0) {
        /* drain any stale post so arm/wait stay paired */
    }
    s_isr_armed = 1;
    C6_SYSTIMER_CONF |= C6_TARGET1_WORK_EN;
    C6_SYSTIMER_INT_ENA |= C6_TARGET1_INT_BIT; /* enable last */
    return 0;
}

bench_status nuttx_bench_isr_create(void *ctx)
{
    int cpuint;

    (void)ctx;
    if (s_isr_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_isr_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }

    C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
    C6_SYSTIMER_INT_ENA |= C6_TARGET1_INT_BIT;
    /* Priority 1, LEVEL trigger: SOC_SYSTIMER_INT_LEVEL is 1 on the C6, so the
     * SYSTIMER interrupt must be set up as level-triggered (the working system
     * tick uses ESP_IRQ_TRIGGER_LEVEL for the same peripheral). */
    cpuint = esp_setup_irq(C6_SYSTIMER_TARGET1_SOURCE, 0, 0, systimer_t1_isr,
                           NULL);
    if (cpuint < 0) {
        /* Level 1 rejected: fall back to the tick's default level. */
        cpuint = esp_setup_irq(C6_SYSTIMER_TARGET1_SOURCE, 1, 0,
                               systimer_t1_isr, NULL);
    }
    if (cpuint < 0) {
        (void)sem_destroy(&s_isr_sem);
        return BENCH_PORT;
    }
    s_isr_cpuint = cpuint;
    up_enable_irq(C6_SYSTIMER_TARGET1_IRQ);
    s_isr_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_isr_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (isr_arm() != 0) {
        return BENCH_PORT;
    }
    /* Bounded wait: a missed alarm must fail the run, not wedge the device. */
    if (nxsem_tickwait(&s_isr_sem, MSEC2TICK(50)) < 0) {
        C6_SYSTIMER_INT_ENA &= ~C6_TARGET1_INT_BIT;
        C6_SYSTIMER_CONF &= ~C6_TARGET1_WORK_EN;
        C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
        s_isr_armed = 0;
        return BENCH_PORT;
    }
    uint64_t wake = nuttx_bench_c6_now_ticks();
    *ticks = (wake >= s_isr_entry) ? (wake - s_isr_entry) : 0;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        up_disable_irq(C6_SYSTIMER_TARGET1_IRQ);
        C6_SYSTIMER_INT_ENA &= ~C6_TARGET1_INT_BIT;
        C6_SYSTIMER_CONF &= ~C6_TARGET1_WORK_EN;
        s_isr_armed = 0;
        if (s_isr_cpuint >= 0) {
            (void)esp_teardown_irq(C6_SYSTIMER_TARGET1_SOURCE, s_isr_cpuint);
            s_isr_cpuint = -1;
        }
        (void)sem_destroy(&s_isr_sem);
        s_isr_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_isr_arm(void)
{
    return (isr_arm() != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_isr_wait(void)
{
    if (nxsem_tickwait(&s_isr_sem, MSEC2TICK(50)) < 0) {
        C6_SYSTIMER_INT_ENA &= ~C6_TARGET1_INT_BIT;
        C6_SYSTIMER_CONF &= ~C6_TARGET1_WORK_EN;
        C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
        s_isr_armed = 0;
        return (int)BENCH_PORT;
    }
    return 0;
}

uint64_t bench_rust_isr_entry(void)
{
    return s_isr_entry;
}

/* ------------------------------------------- trigger to ISR entry (10) */

/* ESP32-C6 software-asserted CPU interrupt. Writing bit 0 of
 * INTPRI_CPU_INTR_FROM_CPU_2_REG asserts interrupt-matrix source
 * ETS_FROM_CPU_INTR2 (24), one of the two FROM_CPU sources not reserved for
 * cross-core scheduling. The task masks interrupts across the counter read and
 * the assert, so the exception is taken at the unmask and the handler's first
 * instruction is the ISR-entry timestamp. Value = trigger -> entry, with no
 * nominal period subtracted. */
#define C6_INTPRI_BASE       0x600C5000UL
#define C6_INTPRI_FROM_CPU2  (*(volatile uint32_t *)(C6_INTPRI_BASE + 0x98))
#define C6_FROM_CPU2_BIT     (1u << 0)
#define C6_FROM_CPU2_SOURCE  24 /* ETS_FROM_CPU_INTR2_SOURCE */
#define C6_FROM_CPU2_IRQ     (C6_FROM_CPU2_SOURCE + 17)

static sem_t             s_trig_sem;
static volatile uint64_t s_trig_entry;
static int               s_trig_active;
static int               s_trig_cpuint = -1;

static int from_cpu2_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_INTPRI_FROM_CPU2 = 0; /* deassert the level source before anything else */
    s_trig_entry = nuttx_bench_c6_now_ticks();
    (void)nxsem_post(&s_trig_sem);
    return 0;
}

bench_status nuttx_bench_trigger_isr_create(void *ctx)
{
    int cpuint;
    (void)ctx;
    if (s_trig_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_trig_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    C6_INTPRI_FROM_CPU2 = 0;
    cpuint = esp_setup_irq(C6_FROM_CPU2_SOURCE, 0, 0, from_cpu2_isr, NULL);
    if (cpuint < 0) {
        cpuint = esp_setup_irq(C6_FROM_CPU2_SOURCE, 1, 0, from_cpu2_isr, NULL);
    }
    if (cpuint < 0) {
        (void)sem_destroy(&s_trig_sem);
        return BENCH_PORT;
    }
    s_trig_cpuint = cpuint;
    up_enable_irq(C6_FROM_CPU2_IRQ);
    s_trig_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_trig_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_trig_sem); /* drain any stale signal */
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = nuttx_bench_c6_now_ticks();
    C6_INTPRI_FROM_CPU2 = C6_FROM_CPU2_BIT;
    leave_critical_section(flags); /* unmask: the exception is taken here */
    if (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) {
        C6_INTPRI_FROM_CPU2 = 0;
        return BENCH_PORT;
    }
    uint64_t entry = s_trig_entry;
    if (entry < t0) {
        return BENCH_CORRUPT;
    }
    *ticks = entry - t0;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        up_disable_irq(C6_FROM_CPU2_IRQ);
        C6_INTPRI_FROM_CPU2 = 0;
        if (s_trig_cpuint >= 0) {
            (void)esp_teardown_irq(C6_FROM_CPU2_SOURCE, s_trig_cpuint);
            s_trig_cpuint = -1;
        }
        (void)sem_destroy(&s_trig_sem);
        s_trig_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_trig_drain(void)
{
    (void)nxsem_trywait(&s_trig_sem);
    return 0;
}

uint64_t bench_rust_trig_fire(void)
{
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = nuttx_bench_c6_now_ticks();
    C6_INTPRI_FROM_CPU2 = C6_FROM_CPU2_BIT;
    leave_critical_section(flags);
    return t0;
}

int bench_rust_trig_wait(void)
{
    if (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) {
        C6_INTPRI_FROM_CPU2 = 0;
        return (int)BENCH_PORT;
    }
    return 0;
}

uint64_t bench_rust_trig_entry(void)
{
    return s_trig_entry;
}

/* ------------------------------------ periodic jitter under load (11) */

/* Case 11 on ESP32-C6 NuttX. SYSTIMER target1 releases the task every 750 us
 * (unit0 at 16 MHz -> 12000 counts) with a self-correcting re-arm at
 * last_target + P; target2 injects a busy-loop ISR every 137 us (2192 counts) at
 * a higher priority. The activation error is measured in the unit0 raw-count
 * domain (base, grid and wake stamp all unit0), so it is idle-safe and
 * drift-free; it is scaled by 10 to the adapter's 160 MHz fine tick. The load
 * body is calibrated against the 160 MHz CSR performance counter while busy.
 * Target 0 is the NuttX system tick; targets 1/2 (sources 58/59) are free. */
#define C6_SYSTIMER_TARGET2_HI    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x2c))
#define C6_SYSTIMER_TARGET2_LO    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x30))
#define C6_SYSTIMER_TARGET2_CONF  (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x3c))
#define C6_SYSTIMER_COMP2_LOAD    (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x58))
#define C6_TARGET2_WORK_EN   (1u << 22)
#define C6_TARGET2_ONESHOT   (0u << 30)
#define C6_TARGET2_UNIT0     (0u << 31)
#define C6_COMP2_LOAD_SYNC   (1u << 0)
#define C6_TARGET2_INT_BIT   (1u << 2)
#define C6_SYSTIMER_TARGET2_SOURCE 59
#define C6_SYSTIMER_TARGET2_IRQ    (C6_SYSTIMER_TARGET2_SOURCE + 17)

#define PJIT_P_TICKS    12000u /* 750 us at 16 MHz */
#define PJIT_LOAD_TICKS 2192u  /* 137 us at 16 MHz */
#define PJIT_LOAD_TARGET 7000u /* busy cycles per load ISR (~44 us at 160 MHz) */
#define PJIT_TICK_SCALE 10u    /* 160 MHz adapter tick / 16 MHz unit0 */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static sem_t             s_pjit_sem;
static volatile uint64_t s_pjit_rel_target;
#if PJIT_LOAD_ENABLE
static volatile uint64_t s_pjit_load_target;
static volatile uint32_t s_pjit_iters;
#endif
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
static volatile uint64_t s_pjit_r0;
static int               s_pjit_active;
static int               s_pjit_rel_cpuint = -1;
#if PJIT_LOAD_ENABLE
static int               s_pjit_load_cpuint = -1;
#endif

static int pjit_set_target1(uint64_t target)
{
    C6_SYSTIMER_TARGET1_CONF = C6_TARGET1_ONESHOT | C6_TARGET1_UNIT0;
    C6_SYSTIMER_TARGET1_HI = (uint32_t)(target >> 32);
    C6_SYSTIMER_TARGET1_LO = (uint32_t)target;
    C6_SYSTIMER_COMP1_LOAD = C6_COMP1_LOAD_SYNC;
    return 0;
}

#if PJIT_LOAD_ENABLE
static int pjit_set_target2(uint64_t target)
{
    C6_SYSTIMER_TARGET2_CONF = C6_TARGET2_ONESHOT | C6_TARGET2_UNIT0;
    C6_SYSTIMER_TARGET2_HI = (uint32_t)(target >> 32);
    C6_SYSTIMER_TARGET2_LO = (uint32_t)target;
    C6_SYSTIMER_COMP2_LOAD = C6_COMP2_LOAD_SYNC;
    return 0;
}
#endif

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

static int pjit_release_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT;
    s_pjit_rel_target += PJIT_P_TICKS;
    (void)pjit_set_target1(s_pjit_rel_target);
    s_pjit_releases++;
    (void)nxsem_post(&s_pjit_sem);
    return 0;
}

#if PJIT_LOAD_ENABLE
static int pjit_load_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_SYSTIMER_INT_CLR = C6_TARGET2_INT_BIT;
    s_pjit_load_target += PJIT_LOAD_TICKS;
    (void)pjit_set_target2(s_pjit_load_target);
    pjit_load_body(s_pjit_iters);
    return 0;
}
#endif

bench_status nuttx_bench_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_pjit_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    s_pjit_releases = 0;
    s_pjit_n = 0;

#if PJIT_LOAD_ENABLE
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        irqstate_t flags = enter_critical_section();
        uint64_t a = nuttx_bench_c6_now_ticks();
        pjit_load_body(s_pjit_iters);
        uint64_t d = nuttx_bench_c6_now_ticks() - a;
        leave_critical_section(flags);
        if (d >= PJIT_LOAD_TARGET &&
            d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_iters++;
        } else {
            uint32_t next =
                (uint32_t)(((uint64_t)s_pjit_iters * PJIT_LOAD_TARGET) / d);
            s_pjit_iters = (next == 0U) ? 1U : next;
        }
    }
#endif

    uint64_t now;
    if (systimer_now(&now) != 0) {
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    s_pjit_rel_target = now + PJIT_P_TICKS;
    s_pjit_r0 = s_pjit_rel_target;
    (void)pjit_set_target1(s_pjit_rel_target);
#if PJIT_LOAD_ENABLE
    s_pjit_load_target = now + PJIT_LOAD_TICKS;
    (void)pjit_set_target2(s_pjit_load_target);
#endif

    int rel_cpuint = esp_setup_irq(C6_SYSTIMER_TARGET1_SOURCE, 0, 0,
                                   pjit_release_isr, NULL);
    if (rel_cpuint < 0) {
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    s_pjit_rel_cpuint = rel_cpuint;
    up_enable_irq(C6_SYSTIMER_TARGET1_IRQ);
#if PJIT_LOAD_ENABLE
    int load_cpuint = esp_setup_irq(C6_SYSTIMER_TARGET2_SOURCE, 1, 0,
                                    pjit_load_isr, NULL);
    if (load_cpuint < 0) {
        up_disable_irq(C6_SYSTIMER_TARGET1_IRQ);
        (void)esp_teardown_irq(C6_SYSTIMER_TARGET1_SOURCE, s_pjit_rel_cpuint);
        s_pjit_rel_cpuint = -1;
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    s_pjit_load_cpuint = load_cpuint;
    up_enable_irq(C6_SYSTIMER_TARGET2_IRQ);
#endif

    C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT | C6_TARGET2_INT_BIT;
    C6_SYSTIMER_INT_ENA |= C6_TARGET1_INT_BIT | C6_TARGET2_INT_BIT;
    C6_SYSTIMER_CONF |= C6_TARGET1_WORK_EN
#if PJIT_LOAD_ENABLE
                        | C6_TARGET2_WORK_EN
#endif
        ;
    s_pjit_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_pjit_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) {
        return BENCH_PORT;
    }
    uint64_t wake;
    if (systimer_now(&wake) != 0) {
        return BENCH_PORT;
    }
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return BENCH_CORRUPT; /* coalesced/missed release */
    }
    uint64_t ideal = s_pjit_r0 + ((uint64_t)n * PJIT_P_TICKS);
    if (wake < ideal) {
        return BENCH_CORRUPT;
    }
    *ticks = (wake - ideal) * PJIT_TICK_SCALE;
    return BENCH_OK;
}

/* Fine-grained primitives for the native Rust pjit sampling loop. */
int bench_rust_pjit_wait(void)
{
    return (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) ? (int)BENCH_PORT
                                                             : 0;
}

int bench_rust_pjit_read(uint64_t *ticks)
{
    uint64_t wake;
    if (systimer_now(&wake) != 0) {
        return (int)BENCH_PORT;
    }
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return (int)BENCH_CORRUPT; /* coalesced/missed release */
    }
    uint64_t ideal = s_pjit_r0 + ((uint64_t)n * PJIT_P_TICKS);
    if (wake < ideal) {
        return (int)BENCH_CORRUPT;
    }
    *ticks = (wake - ideal) * PJIT_TICK_SCALE;
    return 0;
}

bench_status nuttx_bench_pjit_destroy(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        C6_SYSTIMER_INT_ENA &= ~(C6_TARGET1_INT_BIT | C6_TARGET2_INT_BIT);
        C6_SYSTIMER_CONF &= ~(C6_TARGET1_WORK_EN | C6_TARGET2_WORK_EN);
        C6_SYSTIMER_INT_CLR = C6_TARGET1_INT_BIT | C6_TARGET2_INT_BIT;
        up_disable_irq(C6_SYSTIMER_TARGET1_IRQ);
        up_disable_irq(C6_SYSTIMER_TARGET2_IRQ);
        if (s_pjit_rel_cpuint >= 0) {
            (void)esp_teardown_irq(C6_SYSTIMER_TARGET1_SOURCE,
                                   s_pjit_rel_cpuint);
            s_pjit_rel_cpuint = -1;
        }
#if PJIT_LOAD_ENABLE
        if (s_pjit_load_cpuint >= 0) {
            (void)esp_teardown_irq(C6_SYSTIMER_TARGET2_SOURCE,
                                   s_pjit_load_cpuint);
            s_pjit_load_cpuint = -1;
        }
#endif
        (void)sem_destroy(&s_pjit_sem);
        s_pjit_active = 0;
    }
    return BENCH_OK;
}

/* Case 25: maximum interrupt-disabled time proxy. The C6 has no spare SYSTIMER
 * target (0 = tick, 1/2 = case 4/11), so case 25 reuses target 2 (source 59)
 * after case 11's destroy; an isolated 25 run finds it free at boot. The probe
 * is a 50 us one-shot in the 16 MHz unit0 domain; the task busy-waits and
 * timestamps with the 160 MHz CSR, so the interval is idle-safe. MAXDIS_CONTROL=1
 * holds a ~80 us critical section as the positive control. */
#define MAXDIS_PERIOD_U0           800U   /* 50 us @ 16 MHz unit0 */
#define MAXDIS_PERIOD_TICKS       8000U   /* 50 us @ 160 MHz */
#define MAXDIS_CONTROL_CRIT_TICKS 12800U  /* 80 us @ 160 MHz */
#define MAXDIS_SPIN_LIMIT         16000000U /* ~100 ms cap */

static volatile uint32_t s_maxdis_isr_ts;
static volatile int s_maxdis_done;
static int s_maxdis_active;
static int s_maxdis_cpuint = -1;

static int maxdis_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_SYSTIMER_INT_ENA &= ~C6_TARGET2_INT_BIT;
    C6_SYSTIMER_CONF &= ~C6_TARGET2_WORK_EN;
    (void)C6_SYSTIMER_CONF; /* flush posted MMIO writes */
    C6_SYSTIMER_INT_CLR = C6_TARGET2_INT_BIT;
    s_maxdis_isr_ts = (uint32_t)nuttx_bench_c6_now_ticks();
    s_maxdis_done = 1;
    return 0;
}

static int maxdis_arm(void)
{
    uint64_t now;
    uint64_t target;
    C6_SYSTIMER_CONF &= ~C6_TARGET2_WORK_EN;
    C6_SYSTIMER_INT_ENA &= ~C6_TARGET2_INT_BIT;
    if (systimer_now(&now) != 0) {
        return -1;
    }
    target = now + MAXDIS_PERIOD_U0;
    C6_SYSTIMER_TARGET2_CONF = C6_TARGET2_ONESHOT | C6_TARGET2_UNIT0;
    C6_SYSTIMER_TARGET2_HI = (uint32_t)(target >> 32);
    C6_SYSTIMER_TARGET2_LO = (uint32_t)target;
    C6_SYSTIMER_COMP2_LOAD = C6_COMP2_LOAD_SYNC;
    C6_SYSTIMER_INT_CLR = C6_TARGET2_INT_BIT;
    C6_SYSTIMER_CONF |= C6_TARGET2_WORK_EN;
    C6_SYSTIMER_INT_ENA |= C6_TARGET2_INT_BIT; /* enable last */
    return 0;
}

/* Fine-grained primitives for the native Rust maxdis sampling loop. */
int bench_rust_maxdis_begin(void)
{
    s_maxdis_done = 0;
    return 0;
}

int bench_rust_maxdis_arm(void)
{
    return (maxdis_arm() != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_maxdis_wait(uint64_t t0)
{
    while (s_maxdis_done == 0) {
        if (((uint32_t)nuttx_bench_c6_now_ticks() - (uint32_t)t0) >
            MAXDIS_SPIN_LIMIT) {
            return (int)BENCH_PORT;
        }
    }
    return 0;
}

uint64_t bench_rust_maxdis_isr_ts(void)
{
    return (uint64_t)s_maxdis_isr_ts;
}

bench_status nuttx_bench_maxdis_create(void *ctx)
{
    (void)ctx;
    if (s_maxdis_active) {
        return BENCH_OK;
    }
    C6_SYSTIMER_INT_CLR = C6_TARGET2_INT_BIT;
    C6_SYSTIMER_INT_ENA |= C6_TARGET2_INT_BIT;
    s_maxdis_cpuint = esp_setup_irq(C6_SYSTIMER_TARGET2_SOURCE, 1, 0,
                                    maxdis_isr, NULL);
    if (s_maxdis_cpuint < 0) {
        return BENCH_PORT;
    }
    up_enable_irq(C6_SYSTIMER_TARGET2_IRQ);
    s_maxdis_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_maxdis_active || ticks == NULL) {
        return BENCH_PORT;
    }
    s_maxdis_done = 0;
    uint32_t t0 = (uint32_t)nuttx_bench_c6_now_ticks();
    if (maxdis_arm() != 0) {
        return BENCH_PORT;
    }
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
    irqstate_t flags = enter_critical_section();
    uint32_t c0 = (uint32_t)nuttx_bench_c6_now_ticks();
    while (((uint32_t)nuttx_bench_c6_now_ticks() - c0) <
           MAXDIS_CONTROL_CRIT_TICKS) {
    }
    leave_critical_section(flags);
#endif
    while (s_maxdis_done == 0) {
        if (((uint32_t)nuttx_bench_c6_now_ticks() - t0) > MAXDIS_SPIN_LIMIT) {
            return BENCH_PORT;
        }
    }
    uint32_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)(elapsed - MAXDIS_PERIOD_TICKS);
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_destroy(void *ctx)
{
    (void)ctx;
    if (s_maxdis_active) {
        C6_SYSTIMER_INT_ENA &= ~C6_TARGET2_INT_BIT;
        C6_SYSTIMER_CONF &= ~C6_TARGET2_WORK_EN;
        C6_SYSTIMER_INT_CLR = C6_TARGET2_INT_BIT;
        up_disable_irq(C6_SYSTIMER_TARGET2_IRQ);
        if (s_maxdis_cpuint >= 0) {
            (void)esp_teardown_irq(C6_SYSTIMER_TARGET2_SOURCE, s_maxdis_cpuint);
            s_maxdis_cpuint = -1;
        }
        s_maxdis_active = 0;
    }
    return BENCH_OK;
}

/* Case 20: nested interrupt overhead. FROM_CPU2 (source 24, LEVEL1) runs a
 * fixed-work spin and force-asserts FROM_CPU3 (source 25, LEVEL3) ten times;
 * measure() = min(nested) - min(baseline) over three runs each. FROM_CPU2 is
 * case 10 and FROM_CPU3 is unused elsewhere on this path; both are torn down
 * before the next case. NESTED_CONTROL=1 disables the higher line. Timestamps
 * use the 160 MHz C6 performance counter. */
#define C6_INTPRI_FROM_CPU3  (*(volatile uint32_t *)(C6_INTPRI_BASE + 0x9c))
#define C6_FROM_CPU3_BIT     (1u << 0)
#define C6_FROM_CPU3_SOURCE  25 /* ETS_FROM_CPU_INTR3_SOURCE */
#define C6_FROM_CPU3_IRQ     (C6_FROM_CPU3_SOURCE + 17)
#define NESTED_SPIN  1000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;
static int s_nested_active;
static int s_nested_low_cpuint = -1;
static int s_nested_high_cpuint = -1;

static int nested_c6_low_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_INTPRI_FROM_CPU2 = 0; /* deassert the level source first */
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            C6_INTPRI_FROM_CPU3 = C6_FROM_CPU3_BIT; /* LEVEL3 preempts here */
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
    return 0;
}

static int nested_c6_high_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    C6_INTPRI_FROM_CPU3 = 0; /* deassert before returning */
    return 0;
}

bench_status nuttx_bench_nested_create(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        return BENCH_OK;
    }
    s_nested_low_done = 0;
    s_nested_pend_h = 0;
    C6_INTPRI_FROM_CPU2 = 0;
    C6_INTPRI_FROM_CPU3 = 0;
    s_nested_low_cpuint = esp_setup_irq(C6_FROM_CPU2_SOURCE, 1, 0,
                                        nested_c6_low_isr, NULL);
    if (s_nested_low_cpuint < 0) {
        return BENCH_PORT;
    }
    s_nested_high_cpuint = esp_setup_irq(C6_FROM_CPU3_SOURCE, 3, 0,
                                         nested_c6_high_isr, NULL);
    if (s_nested_high_cpuint < 0) {
        (void)esp_teardown_irq(C6_FROM_CPU2_SOURCE, s_nested_low_cpuint);
        s_nested_low_cpuint = -1;
        return BENCH_PORT;
    }
    up_enable_irq(C6_FROM_CPU2_IRQ);
    up_enable_irq(C6_FROM_CPU3_IRQ);
    s_nested_active = 1;
    return BENCH_OK;
}

static uint32_t nested_c6_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_pend_h = pend_h;
    uint32_t t0 = (uint32_t)nuttx_bench_c6_now_ticks();
    C6_INTPRI_FROM_CPU2 = C6_FROM_CPU2_BIT;
    while (s_nested_low_done == 0) {
    }
    return (uint32_t)nuttx_bench_c6_now_ticks() - t0;
}

/* Fine-grained primitive for the native Rust nested-irq loop. */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_c6_run(pend_h);
}

bench_status nuttx_bench_nested_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_nested_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint32_t base = nested_c6_run(0);
    uint32_t nested = nested_c6_run(1);
    for (int i = 0; i < 2; i++) {
        uint32_t b = nested_c6_run(0);
        uint32_t n = nested_c6_run(1);
        if (b < base) base = b;
        if (n < nested) nested = n;
    }
    *ticks = (nested > base) ? (uint64_t)(nested - base) : 0U;
    return BENCH_OK;
}

bench_status nuttx_bench_nested_destroy(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        up_disable_irq(C6_FROM_CPU2_IRQ);
        up_disable_irq(C6_FROM_CPU3_IRQ);
        C6_INTPRI_FROM_CPU2 = 0;
        C6_INTPRI_FROM_CPU3 = 0;
        if (s_nested_low_cpuint >= 0) {
            (void)esp_teardown_irq(C6_FROM_CPU2_SOURCE, s_nested_low_cpuint);
            s_nested_low_cpuint = -1;
        }
        if (s_nested_high_cpuint >= 0) {
            (void)esp_teardown_irq(C6_FROM_CPU3_SOURCE, s_nested_high_cpuint);
            s_nested_high_cpuint = -1;
        }
        s_nested_active = 0;
    }
    return BENCH_OK;
}

#elif defined(CONFIG_ARCH_CHIP_NRF52)

#include <nuttx/irq.h>
#include <nuttx/arch.h>
#include <nuttx/semaphore.h>

/* nRF52840 TIMER1 one-shot compare. TIMER1 is claimed only by the nrf52 timer
 * driver, which this bench config does not select, so the peripheral is free.
 * Prescaler 4 -> 16 MHz / 2^4 = 1 MHz; CC[0] = 20 -> a 20 us one-pulse.
 * Shorted COMPARE0 -> CLEAR + STOP makes it a one-shot. */
#define NRF_TIMER1_BASE    0x40009000UL
#define NRF_TIM_TASKS_START (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x000))
#define NRF_TIM_TASKS_STOP  (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x004))
#define NRF_TIM_TASKS_CLEAR (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x00C))
#define NRF_TIM_EVENTS_CMP0 (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x140))
#define NRF_TIM_SHORTS      (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x200))
#define NRF_TIM_INTENSET    (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x304))
#define NRF_TIM_MODE        (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x504))
#define NRF_TIM_BITMODE     (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x508))
#define NRF_TIM_PRESCALER   (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x510))
#define NRF_TIM_CC0         (*(volatile uint32_t *)(NRF_TIMER1_BASE + 0x540))

/* nRF52 TIMER INTENSET compare bits start at bit 16 (not bit 0), and the
 * COMPARE0 STOP shortcut is bit 8 (CLEAR is bit 0): see
 * arch/arm/src/nrf52/hardware/nrf52_tim.h (TIM_INT_COMPARE/TIM_SHORTS_*). */
#define NRF_TIM_INT_COMPARE0        (1u << 16)
#define NRF_TIM_SHORT_COMPARE_CLEAR (1u << 0)
#define NRF_TIM_SHORT_COMPARE_STOP  (1u << 8)

/* NuttX external IRQ = 16 + peripheral vector. TIMER1 vector 9 -> 25. */
#define TIMER1_IRQ 25

/* EGU0 / swi0 (vector 20) is unused by this build; force-pend it for case 10.
 * No peripheral setup is needed: NVIC_SetPendingIRQ (up_trigger_irq) fires the
 * handler. */
#define TRIG_IRQ 36

extern uint64_t nuttx_bench_tick_raw(void);

static sem_t            s_isr_sem;
static volatile uint64_t s_isr_entry;
static int              s_isr_active;

static int timer1_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;

    NRF_TIM_EVENTS_CMP0 = 0; /* clear the compare event */
    s_isr_entry = nuttx_bench_tick_raw();
    (void)nxsem_post(&s_isr_sem);
    return 0;
}

static void timer1_arm(void)
{
    NRF_TIM_TASKS_CLEAR = 1;
    NRF_TIM_TASKS_START = 1;
}

bench_status nuttx_bench_isr_create(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_isr_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }

    NRF_TIM_MODE = 0;          /* timer mode */
    NRF_TIM_BITMODE = 3;       /* 32-bit */
    NRF_TIM_PRESCALER = 4;     /* 1 MHz */
    NRF_TIM_CC0 = 20;          /* 20 us */
    NRF_TIM_SHORTS = NRF_TIM_SHORT_COMPARE_CLEAR | NRF_TIM_SHORT_COMPARE_STOP;
    NRF_TIM_INTENSET = NRF_TIM_INT_COMPARE0;
    NRF_TIM_EVENTS_CMP0 = 0;

    if (irq_attach(TIMER1_IRQ, timer1_isr, NULL) != 0) {
        (void)sem_destroy(&s_isr_sem);
        return BENCH_PORT;
    }
    up_enable_irq(TIMER1_IRQ);
    s_isr_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_isr_active || ticks == NULL) {
        return BENCH_PORT;
    }
    while (nxsem_trywait(&s_isr_sem) == 0) {
        /* drain any stale post so arm/wait stay paired */
    }
    timer1_arm();
    if (nxsem_tickwait(&s_isr_sem, MSEC2TICK(100)) < 0) {
        return BENCH_PORT;
    }
    uint64_t wake = nuttx_bench_tick_raw();
    if (wake < s_isr_entry) {
        *ticks = (uint64_t)-1;
        return BENCH_CORRUPT;
    }
    *ticks = wake - s_isr_entry;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        up_disable_irq(TIMER1_IRQ);
        (void)irq_detach(TIMER1_IRQ);
        NRF_TIM_TASKS_STOP = 1;
        NRF_TIM_INTENSET = 0;
        (void)sem_destroy(&s_isr_sem);
        s_isr_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_isr_arm(void)
{
    timer1_arm();
    return 0;
}

int bench_rust_isr_wait(void)
{
    return (nxsem_tickwait(&s_isr_sem, MSEC2TICK(100)) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_isr_entry(void)
{
    return s_isr_entry;
}

/* ------------------------------------------- trigger to ISR entry (10) */

static sem_t             s_trig_sem;
static volatile uint64_t s_trig_entry;
static int               s_trig_active;

static int trig_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    s_trig_entry = nuttx_bench_tick_raw();
    (void)nxsem_post(&s_trig_sem);
    return 0;
}

bench_status nuttx_bench_trigger_isr_create(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_trig_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(TRIG_IRQ, trig_isr, NULL) != 0) {
        (void)sem_destroy(&s_trig_sem);
        return BENCH_PORT;
    }
    up_enable_irq(TRIG_IRQ);
    s_trig_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_trig_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_trig_sem); /* drain any stale signal */
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = nuttx_bench_tick_raw();
    up_trigger_irq(TRIG_IRQ, 0);
    leave_critical_section(flags); /* unmask: the exception is taken here */
    if (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) {
        return BENCH_PORT;
    }
    uint64_t entry = s_trig_entry;
    if (entry < t0) {
        return BENCH_CORRUPT;
    }
    *ticks = entry - t0;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        up_disable_irq(TRIG_IRQ);
        (void)irq_detach(TRIG_IRQ);
        (void)sem_destroy(&s_trig_sem);
        s_trig_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_trig_drain(void)
{
    (void)nxsem_trywait(&s_trig_sem);
    return 0;
}

uint64_t bench_rust_trig_fire(void)
{
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = nuttx_bench_tick_raw();
    up_trigger_irq(TRIG_IRQ, 0);
    leave_critical_section(flags);
    return t0;
}

int bench_rust_trig_wait(void)
{
    return (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_trig_entry(void)
{
    return s_trig_entry;
}

/* ------------------------------------ periodic jitter under load (11) */

/* Case 11 on nRF52840 NuttX. TIMER2 auto-reloads at an exact 750 us
 * (16 MHz/PRESCALER 0 -> 12000 ticks, SHORTS COMPARE0_CLEAR) and gives the
 * semaphore; TIMER3 injects a busy-loop ISR every 137 us (2192 ticks) at a
 * higher priority. The activation error is captured from TIMER2's own counter
 * (TASKS_CAPTURE[1]): COMPARE0_CLEAR resets it at each release, so the count is
 * the release-to-wake latency in 16 MHz ticks, scaled by 4 to the declared
 * 64 MHz tick. DWT is not used for the measurement because CYCCNT stops while
 * the CPU idles in the semaphore-blocked wait; it is used only for the
 * interrupts-masked load-body calibration. NuttX external IRQ = 16 + peripheral
 * vector: TIMER2 vector 10 -> 26, TIMER3 vector 26 -> 42. */
#define NRF_TIMER2_BASE 0x4000A000UL
#define NRF_TIMER3_BASE 0x4001A000UL
#define NRF_T2(off) (*(volatile uint32_t *)(NRF_TIMER2_BASE + (off)))
#define NRF_T3(off) (*(volatile uint32_t *)(NRF_TIMER3_BASE + (off)))
#define NRF_TASKS_START    0x000UL
#define NRF_TASKS_STOP     0x004UL
#define NRF_TASKS_CLEAR    0x00CUL
#define NRF_TASKS_CAPTURE1 0x044UL
#define NRF_EVENTS_CMP0    0x140UL
#define NRF_SHORTS         0x200UL
#define NRF_INTENSET       0x304UL
#define NRF_INTENCLR       0x308UL
#define NRF_MODE           0x504UL
#define NRF_BITMODE        0x508UL
#define NRF_PRESCALER      0x510UL
#define NRF_CC0            0x540UL
#define NRF_CC1            0x544UL

#define NRF_TIMER2_IRQ_EXT 26 /* vector 10 + 16 */
#define NRF_TIMER3_IRQ_EXT 42 /* vector 26 + 16 */
#define PJIT_P_TICKS     12000U
#define PJIT_LOAD_TICKS  2192U
#define PJIT_LOAD_TARGET 3000U
#define PJIT_TICK_SCALE  4U
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static sem_t             s_pjit_sem;
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
#if PJIT_LOAD_ENABLE
static volatile uint32_t s_pjit_iters;
#endif
static int               s_pjit_active;

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

static int pjit_release_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    if (NRF_T2(NRF_EVENTS_CMP0) == 0U) {
        return 0;
    }
    NRF_T2(NRF_EVENTS_CMP0) = 0U;
    s_pjit_releases++;
    (void)nxsem_post(&s_pjit_sem);
    return 0;
}

#if PJIT_LOAD_ENABLE
static int pjit_load_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    NRF_T3(NRF_EVENTS_CMP0) = 0U;
    pjit_load_body(s_pjit_iters);
    return 0;
}
#endif

bench_status nuttx_bench_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_pjit_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    s_pjit_releases = 0;
    s_pjit_n = 0;
#if PJIT_LOAD_ENABLE
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        irqstate_t flags = enter_critical_section();
        uint64_t a = nuttx_bench_tick_raw();
        pjit_load_body(s_pjit_iters);
        uint64_t d = nuttx_bench_tick_raw() - a;
        leave_critical_section(flags);
        if (d >= PJIT_LOAD_TARGET &&
            d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_iters++;
        } else {
            uint32_t next =
                (uint32_t)(((uint64_t)s_pjit_iters * PJIT_LOAD_TARGET) / d);
            s_pjit_iters = (next == 0U) ? 1U : next;
        }
    }
#endif

    NRF_T2(NRF_MODE) = 0;
    NRF_T2(NRF_BITMODE) = 0;
    NRF_T2(NRF_PRESCALER) = 0;
    NRF_T2(NRF_CC0) = PJIT_P_TICKS;
    NRF_T2(NRF_EVENTS_CMP0) = 0;
    NRF_T2(NRF_SHORTS) = NRF_TIM_SHORT_COMPARE_CLEAR;
    NRF_T2(NRF_INTENSET) = NRF_TIM_INT_COMPARE0;
    if (irq_attach(NRF_TIMER2_IRQ_EXT, pjit_release_isr, NULL) != 0) {
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    up_enable_irq(NRF_TIMER2_IRQ_EXT);

#if PJIT_LOAD_ENABLE
    NRF_T3(NRF_MODE) = 0;
    NRF_T3(NRF_BITMODE) = 0;
    NRF_T3(NRF_PRESCALER) = 0;
    NRF_T3(NRF_CC0) = PJIT_LOAD_TICKS;
    NRF_T3(NRF_EVENTS_CMP0) = 0;
    NRF_T3(NRF_SHORTS) = NRF_TIM_SHORT_COMPARE_CLEAR;
    NRF_T3(NRF_INTENSET) = NRF_TIM_INT_COMPARE0;
    if (irq_attach(NRF_TIMER3_IRQ_EXT, pjit_load_isr, NULL) != 0) {
        up_disable_irq(NRF_TIMER2_IRQ_EXT);
        (void)irq_detach(NRF_TIMER2_IRQ_EXT);
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    up_enable_irq(NRF_TIMER3_IRQ_EXT);
#endif

    NRF_T2(NRF_TASKS_CLEAR) = 1;
    NRF_T2(NRF_TASKS_START) = 1;
#if PJIT_LOAD_ENABLE
    NRF_T3(NRF_TASKS_CLEAR) = 1;
    NRF_T3(NRF_TASKS_START) = 1;
#endif
    s_pjit_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_pjit_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) {
        return BENCH_PORT;
    }
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return BENCH_CORRUPT; /* coalesced/missed release */
    }
    NRF_T2(NRF_TASKS_CAPTURE1) = 1U;
    uint32_t t = NRF_T2(NRF_CC1);
    if (t >= PJIT_P_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)t * PJIT_TICK_SCALE;
    return BENCH_OK;
}

/* Fine-grained primitives for the native Rust pjit sampling loop. */
int bench_rust_pjit_wait(void)
{
    return (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) ? (int)BENCH_PORT
                                                             : 0;
}

int bench_rust_pjit_read(uint64_t *ticks)
{
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return (int)BENCH_CORRUPT; /* coalesced/missed release */
    }
    NRF_T2(NRF_TASKS_CAPTURE1) = 1U;
    uint32_t t = NRF_T2(NRF_CC1);
    if (t >= PJIT_P_TICKS) {
        return (int)BENCH_CORRUPT;
    }
    *ticks = (uint64_t)t * PJIT_TICK_SCALE;
    return 0;
}

bench_status nuttx_bench_pjit_destroy(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        NRF_T2(NRF_TASKS_STOP) = 1;
        NRF_T3(NRF_TASKS_STOP) = 1;
        NRF_T2(NRF_INTENCLR) = NRF_TIM_INT_COMPARE0;
        NRF_T3(NRF_INTENCLR) = NRF_TIM_INT_COMPARE0;
        up_disable_irq(NRF_TIMER2_IRQ_EXT);
        up_disable_irq(NRF_TIMER3_IRQ_EXT);
        (void)irq_detach(NRF_TIMER2_IRQ_EXT);
        (void)irq_detach(NRF_TIMER3_IRQ_EXT);
        (void)sem_destroy(&s_pjit_sem);
        s_pjit_active = 0;
    }
    return BENCH_OK;
}

/* Case 25: maximum interrupt-disabled time proxy. TIMER4 (free; TIMER1/2/3 and
 * EGU serve cases 4/10/11) is a one-pulse probe at 50 us. The nRF DWT stalls
 * while the CPU idles, so the task busy-waits on a flag (staying running) and
 * the 64 MHz DWT keeps counting; measure() returns the delay beyond the period.
 * MAXDIS_CONTROL=1 holds a ~80 us critical section as the positive control.
 * Timer: PRESCALER 4 -> 1 MHz, CC0 = 50 (50 us). NuttX external IRQ = 16 +
 * vector: TIMER4 vector 27 -> 43. */
#define MAXDIS_TIMER4_BASE 0x4001B000UL
#define MD4(off) (*(volatile uint32_t *)(MAXDIS_TIMER4_BASE + (off)))
#define MD4_TASKS_START 0x000UL
#define MD4_TASKS_STOP  0x004UL
#define MD4_TASKS_CLEAR 0x00CUL
#define MD4_EVENTS_CMP0 0x140UL
#define MD4_SHORTS      0x200UL
#define MD4_INTENSET    0x304UL
#define MD4_MODE        0x504UL
#define MD4_BITMODE     0x508UL
#define MD4_PRESCALER   0x510UL
#define MD4_CC0         0x540UL
#define MAXDIS_TIMER4_IRQ_EXT 43
#define MAXDIS_SHORT_COMPARE0_STOP (1UL << 8)
#define MAXDIS_INT_COMPARE0        (1UL << 16)
#define MAXDIS_DEMCR      (*(volatile uint32_t *)0xe000edfcUL)
#define MAXDIS_DWT_CTRL   (*(volatile uint32_t *)0xe0001000UL)
#define MAXDIS_DWT_CYCCNT (*(volatile uint32_t *)0xe0001004UL)

#define MAXDIS_PERIOD_TICKS       3200U /* 50 us @ 64 MHz */
#define MAXDIS_CONTROL_CRIT_TICKS 5120U /* 80 us @ 64 MHz */
#define MAXDIS_SPIN_LIMIT         6400000U /* ~100 ms cap */

static volatile uint32_t s_maxdis_isr_ts;
static volatile int s_maxdis_done;
static int s_maxdis_active;

static inline uint32_t maxdis_cycles(void)
{
    MAXDIS_DEMCR |= (1UL << 24);
    MAXDIS_DWT_CTRL |= 1UL;
    return MAXDIS_DWT_CYCCNT;
}

static int maxdis_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    if (MD4(MD4_EVENTS_CMP0) == 0U) {
        return 0;
    }
    MD4(MD4_EVENTS_CMP0) = 0U;
    MD4(MD4_TASKS_STOP) = 1U;
    MD4(MD4_TASKS_CLEAR) = 1U;
    s_maxdis_isr_ts = maxdis_cycles();
    s_maxdis_done = 1;
    return 0;
}

/* Fine-grained primitives for the native Rust maxdis sampling loop. */
int bench_rust_maxdis_begin(void)
{
    s_maxdis_done = 0;
    return 0;
}

int bench_rust_maxdis_arm(void)
{
    MD4(MD4_EVENTS_CMP0) = 0U;
    MD4(MD4_TASKS_CLEAR) = 1U;
    MD4(MD4_TASKS_START) = 1U;
    return 0;
}

int bench_rust_maxdis_wait(uint64_t t0)
{
    while (s_maxdis_done == 0) {
        if ((maxdis_cycles() - (uint32_t)t0) > MAXDIS_SPIN_LIMIT) {
            return (int)BENCH_PORT;
        }
    }
    return 0;
}

uint64_t bench_rust_maxdis_isr_ts(void)
{
    return (uint64_t)s_maxdis_isr_ts;
}

bench_status nuttx_bench_maxdis_create(void *ctx)
{
    (void)ctx;
    (void)maxdis_cycles(); /* enable the DWT (not sticky on this part) */
    MD4(MD4_MODE) = 0U;
    MD4(MD4_BITMODE) = 0U;
    MD4(MD4_PRESCALER) = 4U; /* 16 MHz / 2^4 = 1 MHz */
    MD4(MD4_CC0) = 50U;      /* 50 us */
    MD4(MD4_SHORTS) = MAXDIS_SHORT_COMPARE0_STOP;
    MD4(MD4_EVENTS_CMP0) = 0U;
    MD4(MD4_INTENSET) = MAXDIS_INT_COMPARE0;
    if (irq_attach(MAXDIS_TIMER4_IRQ_EXT, maxdis_isr, NULL) != 0) {
        return BENCH_PORT;
    }
    up_enable_irq(MAXDIS_TIMER4_IRQ_EXT);
    s_maxdis_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_maxdis_active || ticks == NULL) {
        return BENCH_PORT;
    }
    s_maxdis_done = 0;
    uint32_t t0 = maxdis_cycles();
    MD4(MD4_EVENTS_CMP0) = 0U;
    MD4(MD4_TASKS_CLEAR) = 1U;
    MD4(MD4_TASKS_START) = 1U;
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
    irqstate_t flags = enter_critical_section();
    uint32_t c0 = maxdis_cycles();
    while ((maxdis_cycles() - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
    }
    leave_critical_section(flags);
#endif
    while (s_maxdis_done == 0) {
        if ((maxdis_cycles() - t0) > MAXDIS_SPIN_LIMIT) {
            return BENCH_PORT;
        }
    }
    uint32_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)(elapsed - MAXDIS_PERIOD_TICKS);
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_destroy(void *ctx)
{
    (void)ctx;
    if (s_maxdis_active) {
        MD4(MD4_TASKS_STOP) = 1U;
        up_disable_irq(MAXDIS_TIMER4_IRQ_EXT);
        (void)irq_detach(MAXDIS_TIMER4_IRQ_EXT);
        s_maxdis_active = 0;
    }
    return BENCH_OK;
}

/* Case 20: nested interrupt overhead. EGU1/SWI1 (external IRQ 37, lowest
 * priority) runs a fixed-work spin and force-pends EGU2/SWI2 (external IRQ 38,
 * higher priority) ten times; measure() = min(nested) - min(baseline) over three
 * runs each. EGU0 is case 10; TIMER1/2/3/4 serve cases 4/11/25. NESTED_CONTROL=1
 * disables the higher IRQ. nRF has 3 priority bits, so the NVIC IPR bytes use
 * 0xE0 (priority 7) and 0x40 (priority 2). */
#define NESTED_SPIN  1000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif
#define NESTED_LOW_IRQ  37 /* SWI1/EGU1 vector 21 + 16 */
#define NESTED_HIGH_IRQ 38 /* SWI2/EGU2 vector 22 + 16 */
#define NESTED_NVIC_IPR(irq) (*(volatile uint8_t *)(0xE000E400UL + ((irq) - 16U)))

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;
static int s_nested_active;

static int nested_low_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            up_trigger_irq(NESTED_HIGH_IRQ, 0);
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
    return 0;
}

static int nested_high_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    return 0;
}

bench_status nuttx_bench_nested_create(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        return BENCH_OK;
    }
    s_nested_low_done = 0;
    s_nested_pend_h = 0;
    if (irq_attach(NESTED_LOW_IRQ, nested_low_isr, NULL) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(NESTED_HIGH_IRQ, nested_high_isr, NULL) != 0) {
        (void)irq_detach(NESTED_LOW_IRQ);
        return BENCH_PORT;
    }
    NESTED_NVIC_IPR(NESTED_LOW_IRQ) = 0xE0U;
    NESTED_NVIC_IPR(NESTED_HIGH_IRQ) = 0x40U;
    up_enable_irq(NESTED_LOW_IRQ);
    up_enable_irq(NESTED_HIGH_IRQ);
    s_nested_active = 1;
    return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_pend_h = pend_h;
    const uint32_t t0 = maxdis_cycles();
    up_trigger_irq(NESTED_LOW_IRQ, 0);
    while (s_nested_low_done == 0) {
    }
    return maxdis_cycles() - t0;
}

/* Fine-grained primitive for the native Rust nested-irq loop. */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_run(pend_h);
}

bench_status nuttx_bench_nested_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_nested_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint32_t base = nested_run(0);
    uint32_t nested = nested_run(1);
    for (int i = 0; i < 2; i++) {
        uint32_t b = nested_run(0);
        uint32_t n = nested_run(1);
        if (b < base) base = b;
        if (n < nested) nested = n;
    }
    *ticks = (nested > base) ? (uint64_t)(nested - base) : 0U;
    return BENCH_OK;
}

bench_status nuttx_bench_nested_destroy(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        up_disable_irq(NESTED_LOW_IRQ);
        up_disable_irq(NESTED_HIGH_IRQ);
        (void)irq_detach(NESTED_LOW_IRQ);
        (void)irq_detach(NESTED_HIGH_IRQ);
        s_nested_active = 0;
    }
    return BENCH_OK;
}

#elif defined(CONFIG_ARCH_CHIP_RP23XX)

#include <nuttx/irq.h>
#include <nuttx/arch.h>
#include <nuttx/semaphore.h>

/* RP2350 TIMER0/TIMER1 are free-running 64-bit microsecond counters (the OS
 * tick is SysTick; rp23xx_oneshot.c claims ALARM0 only when
 * CONFIG_RP23XX_SYSTIMER_TICKLESS is selected, which this bench defconfig does
 * not). ALARMn is an absolute compare; writing it arms, writing 0 disarms.
 * ALARM0..3 of TIMERn raise NVIC lines TIMERn_IRQ_0..3. Timestamps use the
 * Cortex-M33 DWT cycle counter at 150 MHz (nuttx_bench_tick_raw). NuttX external
 * IRQ = 16 (RP23XX_IRQ_EXTINT) + peripheral vector:
 *   TIMER0 ALARM0 -> 16, TIMER0 ALARM1 -> 17, TIMER1 ALARM0 -> 20,
 *   TIMER1 ALARM2 -> 22 (case-10 software trigger).
 */
#define RP_TIMER0_BASE 0x400b0000UL
#define RP_TIMER1_BASE 0x400b8000UL
#define RP_TIMER_ALARM0(b) (*(volatile uint32_t *)((b) + 0x10))
#define RP_TIMER_ALARM1(b) (*(volatile uint32_t *)((b) + 0x14))
#define RP_TIMER_AWL(b)    (*(volatile uint32_t *)((b) + 0x28))
#define RP_TIMER_SOURCE(b) (*(volatile uint32_t *)((b) + 0x38))
#define RP_TIMER_INTR(b)   (*(volatile uint32_t *)((b) + 0x3c))
#define RP_TIMER_INTE(b)   (*(volatile uint32_t *)((b) + 0x40))

#define RP_TIMER0_IRQ_0 16
#define RP_TIMER0_IRQ_1 17
#define RP_TIMER1_IRQ_0 20
#define RP_TIMER1_IRQ_2 22

#define RP_ALARM0 (1u << 0)
#define RP_ALARM1 (1u << 1)
#define RP_ALARM2 (1u << 2)

extern uint64_t nuttx_bench_tick_raw(void);

/* ------------------------------------------- interrupt to task (4) */

static sem_t             s_isr_sem;
static volatile uint64_t s_isr_entry;
static int               s_isr_active;

static int timer0_alarm0_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM0; /* clear ALARM0 */
    s_isr_entry = nuttx_bench_tick_raw();
    (void)nxsem_post(&s_isr_sem);
    return 0;
}

bench_status nuttx_bench_isr_create(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_isr_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    RP_TIMER_SOURCE(RP_TIMER0_BASE) = 0; /* default 1 MHz tick for the 20 us alarm */
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM0;
    RP_TIMER_INTE(RP_TIMER0_BASE) |= RP_ALARM0;
    if (irq_attach(RP_TIMER0_IRQ_0, timer0_alarm0_isr, NULL) != 0) {
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM0;
        (void)sem_destroy(&s_isr_sem);
        return BENCH_PORT;
    }
    up_enable_irq(RP_TIMER0_IRQ_0);
    s_isr_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_isr_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_isr_sem); /* drain any stale signal */
    RP_TIMER_ALARM0(RP_TIMER0_BASE) = RP_TIMER_AWL(RP_TIMER0_BASE) + 20u;
    if (nxsem_tickwait(&s_isr_sem, MSEC2TICK(50)) < 0) {
        return BENCH_PORT;
    }
    uint64_t wake = nuttx_bench_tick_raw();
    *ticks = (wake >= s_isr_entry) ? (wake - s_isr_entry) : 0;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        RP_TIMER_ALARM0(RP_TIMER0_BASE) = 0; /* disarm */
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM0;
        up_disable_irq(RP_TIMER0_IRQ_0);
        (void)irq_detach(RP_TIMER0_IRQ_0);
        (void)sem_destroy(&s_isr_sem);
        s_isr_active = 0;
    }
    return BENCH_OK;
}

/* ------------------------------------------- trigger to ISR entry (10) */

/* Software-asserted minimal interrupt: TIMER1 ALARM2's NVIC line is force-pended
 * with up_trigger_irq while interrupts are masked, so the exception is taken at
 * the unmask; the handler timestamps its first instruction. */
static sem_t             s_trig_sem;
static volatile uint64_t s_trig_entry;
static int               s_trig_active;

static int trig_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    RP_TIMER_INTR(RP_TIMER1_BASE) = RP_ALARM2;
    s_trig_entry = nuttx_bench_tick_raw();
    (void)nxsem_post(&s_trig_sem);
    return 0;
}

bench_status nuttx_bench_trigger_isr_create(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_trig_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(RP_TIMER1_IRQ_2, trig_isr, NULL) != 0) {
        (void)sem_destroy(&s_trig_sem);
        return BENCH_PORT;
    }
    up_enable_irq(RP_TIMER1_IRQ_2);
    s_trig_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_trig_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_trig_sem); /* drain any stale signal */
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = nuttx_bench_tick_raw();
    up_trigger_irq(RP_TIMER1_IRQ_2, 0);
    leave_critical_section(flags); /* unmask: the exception is taken here */
    if (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) {
        return BENCH_PORT;
    }
    uint64_t entry = s_trig_entry;
    if (entry < t0) {
        return BENCH_CORRUPT;
    }
    *ticks = entry - t0;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        up_disable_irq(TRIG_IRQ);
        (void)irq_detach(TRIG_IRQ);
        (void)sem_destroy(&s_trig_sem);
        s_trig_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_trig_drain(void)
{
    (void)nxsem_trywait(&s_trig_sem);
    return 0;
}

uint64_t bench_rust_trig_fire(void)
{
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = (uint64_t)up_perf_gettime();
    up_trigger_irq(TRIG_IRQ, 0);
    leave_critical_section(flags);
    return t0;
}

int bench_rust_trig_wait(void)
{
    return (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_trig_entry(void)
{
    return s_trig_entry;
}

/* ------------------------------------ periodic jitter under load (11) */

/* RP2350 case 11 runs wholly in the TIMERAWL clk_sys domain. TIMER_SOURCE=1
 * selects clk_sys (150 MHz) instead of the default 1 MHz tick, so TIMERAWL
 * counts the same clock as the port's DWT ticks_per_second with 1-cycle
 * resolution. TIMER0 ALARM1 releases every 750 us (112500 counts); its ISR
 * re-arms at last + P (self-correcting grid) and posts the semaphore. TIMER1
 * ALARM0 injects a timer-calibrated busy-loop ISR every 137 us. measure()
 * returns wake - (R0 + n*P) in clk_sys counts, where R0 is the first hardware
 * release instant (grid origin). The 1 MHz tick and the 150 MHz DWT are not
 * exactly proportional, so a mixed-domain version drifted; keeping the whole
 * case in TIMERAWL removes that. */
#define PJIT_P_COUNTS    112500u /* 750 us @ clk_sys 150 MHz */
#define PJIT_LOAD_COUNTS 20550u  /* 137 us @ clk_sys 150 MHz */
#define PJIT_LOAD_TARGET 7000u   /* busy clk_sys counts per load ISR (~35% duty) */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static sem_t             s_pjit_sem;
static volatile uint64_t s_pjit_r0;
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
static volatile uint32_t s_pjit_alarm;
#if PJIT_LOAD_ENABLE
static volatile uint32_t s_pjit_load_alarm;
static volatile uint32_t s_pjit_iters;
#endif
static int               s_pjit_active;

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

static int pjit_release_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM1;
    s_pjit_alarm += PJIT_P_COUNTS;
    RP_TIMER_ALARM1(RP_TIMER0_BASE) = s_pjit_alarm;
    s_pjit_releases++;
    (void)nxsem_post(&s_pjit_sem);
    return 0;
}

#if PJIT_LOAD_ENABLE
static int pjit_load_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    RP_TIMER_INTR(RP_TIMER1_BASE) = RP_ALARM0;
    s_pjit_load_alarm += PJIT_LOAD_COUNTS;
    RP_TIMER_ALARM0(RP_TIMER1_BASE) = s_pjit_load_alarm;
    pjit_load_body(s_pjit_iters);
    return 0;
}
#endif

bench_status nuttx_bench_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_pjit_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    s_pjit_r0 = 0;
    s_pjit_releases = 0;
    s_pjit_n = 0;

    /* Both timers count clk_sys, matching ticks_per_second. */
    RP_TIMER_SOURCE(RP_TIMER0_BASE) = 1;
    RP_TIMER_SOURCE(RP_TIMER1_BASE) = 1;

#if PJIT_LOAD_ENABLE
    /* Calibrate the load body against TIMERAWL (the same clock as the grid) with
     * interrupts masked, so no ISR can inflate the measurement. */
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        irqstate_t flags = enter_critical_section();
        uint32_t a = RP_TIMER_AWL(RP_TIMER0_BASE);
        pjit_load_body(s_pjit_iters);
        uint32_t d = RP_TIMER_AWL(RP_TIMER0_BASE) - a;
        leave_critical_section(flags);
        if (d >= PJIT_LOAD_TARGET &&
            d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_iters++;
        } else {
            uint32_t next =
                (uint32_t)(((uint64_t)s_pjit_iters * PJIT_LOAD_TARGET) / d);
            s_pjit_iters = (next == 0U) ? 1U : next;
        }
    }
#endif

    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM1;
    RP_TIMER_INTE(RP_TIMER0_BASE) |= RP_ALARM1;
    if (irq_attach(RP_TIMER0_IRQ_1, pjit_release_isr, NULL) != 0) {
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM1;
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
#if PJIT_LOAD_ENABLE
    RP_TIMER_INTR(RP_TIMER1_BASE) = RP_ALARM0;
    RP_TIMER_INTE(RP_TIMER1_BASE) |= RP_ALARM0;
    if (irq_attach(RP_TIMER1_IRQ_0, pjit_load_isr, NULL) != 0) {
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM1;
        (void)irq_detach(RP_TIMER0_IRQ_1);
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
#endif

    /* R0 is the first hardware release instant (grid origin), independent of
     * ISR-entry latency. Arm before enabling the line. */
    s_pjit_alarm = RP_TIMER_AWL(RP_TIMER0_BASE) + PJIT_P_COUNTS;
    s_pjit_r0 = s_pjit_alarm;
    RP_TIMER_ALARM1(RP_TIMER0_BASE) = s_pjit_alarm;
    up_enable_irq(RP_TIMER0_IRQ_1);
#if PJIT_LOAD_ENABLE
    s_pjit_load_alarm = RP_TIMER_AWL(RP_TIMER1_BASE) + PJIT_LOAD_COUNTS;
    RP_TIMER_ALARM0(RP_TIMER1_BASE) = s_pjit_load_alarm;
    up_enable_irq(RP_TIMER1_IRQ_0);
#endif
    s_pjit_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_pjit_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) {
        return BENCH_PORT;
    }
    uint32_t wake = RP_TIMER_AWL(RP_TIMER0_BASE);
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return BENCH_CORRUPT; /* a release was coalesced: period missed */
    }
    uint32_t ideal = (uint32_t)s_pjit_r0 + n * PJIT_P_COUNTS;
    uint32_t err = wake - ideal;
    if ((int32_t)err < 0) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)err;
    return BENCH_OK;
}

bench_status nuttx_bench_pjit_destroy(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        RP_TIMER_ALARM1(RP_TIMER0_BASE) = 0;
        RP_TIMER_ALARM0(RP_TIMER1_BASE) = 0;
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM1;
        RP_TIMER_INTE(RP_TIMER1_BASE) &= ~RP_ALARM0;
        up_disable_irq(RP_TIMER0_IRQ_1);
        up_disable_irq(RP_TIMER1_IRQ_0);
        (void)irq_detach(RP_TIMER0_IRQ_1);
        (void)irq_detach(RP_TIMER1_IRQ_0);
        (void)sem_destroy(&s_pjit_sem);
        s_pjit_active = 0;
    }
    return BENCH_OK;
}

/* Case 25: maximum interrupt-disabled time proxy. TIMER0 ALARM2 (free; ALARM0/1
 * serve cases 4/11) is a one-pulse probe in the clk_sys 150 MHz domain; the task
 * busy-waits on a flag and timestamps with the 150 MHz DWT, so the interval is
 * exact and idle-safe. measure() returns the delay beyond the 50 us period;
 * MAXDIS_CONTROL=1 holds a ~80 us critical section as the positive control.
 * NuttX external IRQ = 16 + vector: TIMER0 ALARM2 -> 18. */
#define RP_TIMER_ALARM2(b) (*(volatile uint32_t *)((b) + 0x18))
#define RP_TIMER0_IRQ_2 18

#define MAXDIS_PERIOD_TICKS       7500U   /* 50 us @ 150 MHz */
#define MAXDIS_CONTROL_CRIT_TICKS 12000U  /* 80 us @ 150 MHz */
#define MAXDIS_SPIN_LIMIT         15000000U /* ~100 ms cap */
#define MAXDIS_DEMCR      (*(volatile uint32_t *)0xe000edfcUL)
#define MAXDIS_DWT_CTRL   (*(volatile uint32_t *)0xe0001000UL)
#define MAXDIS_DWT_CYCCNT (*(volatile uint32_t *)0xe0001004UL)

static volatile uint32_t s_maxdis_isr_ts;
static volatile int s_maxdis_done;
static int s_maxdis_active;

static inline uint32_t maxdis_cycles(void)
{
    MAXDIS_DEMCR |= (1UL << 24);
    MAXDIS_DWT_CTRL |= 1UL;
    return MAXDIS_DWT_CYCCNT;
}

static int maxdis_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    if ((RP_TIMER_INTR(RP_TIMER0_BASE) & RP_ALARM2) == 0U) {
        return 0;
    }
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM2; /* clear */
    RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM2;
    s_maxdis_isr_ts = maxdis_cycles();
    s_maxdis_done = 1;
    return 0;
}

bench_status nuttx_bench_maxdis_create(void *ctx)
{
    (void)ctx;
    (void)maxdis_cycles();
    RP_TIMER_SOURCE(RP_TIMER0_BASE) = 1U; /* clk_sys 150 MHz */
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM2;
    RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM2;
    if (irq_attach(RP_TIMER0_IRQ_2, maxdis_isr, NULL) != 0) {
        return BENCH_PORT;
    }
    up_enable_irq(RP_TIMER0_IRQ_2);
    s_maxdis_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_maxdis_active || ticks == NULL) {
        return BENCH_PORT;
    }
    s_maxdis_done = 0;
    uint32_t t0 = maxdis_cycles();
    RP_TIMER_INTR(RP_TIMER0_BASE) = RP_ALARM2;
    RP_TIMER_INTE(RP_TIMER0_BASE) |= RP_ALARM2;
    RP_TIMER_ALARM2(RP_TIMER0_BASE) =
        RP_TIMER_AWL(RP_TIMER0_BASE) + MAXDIS_PERIOD_TICKS;
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
    irqstate_t flags = enter_critical_section();
    uint32_t c0 = maxdis_cycles();
    while ((maxdis_cycles() - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
    }
    leave_critical_section(flags);
#endif
    while (s_maxdis_done == 0) {
        if ((maxdis_cycles() - t0) > MAXDIS_SPIN_LIMIT) {
            return BENCH_PORT;
        }
    }
    uint32_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)(elapsed - MAXDIS_PERIOD_TICKS);
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_destroy(void *ctx)
{
    (void)ctx;
    if (s_maxdis_active) {
        RP_TIMER_ALARM2(RP_TIMER0_BASE) = 0; /* disarm */
        RP_TIMER_INTE(RP_TIMER0_BASE) &= ~RP_ALARM2;
        up_disable_irq(RP_TIMER0_IRQ_2);
        (void)irq_detach(RP_TIMER0_IRQ_2);
        RP_TIMER_SOURCE(RP_TIMER0_BASE) = 0; /* restore 1 MHz for case 4/11 */
        s_maxdis_active = 0;
    }
    return BENCH_OK;
}

/* Case 20: nested interrupt overhead. RP_TIMER1_IRQ_1 (external IRQ 21, lowest
 * priority) runs a fixed-work spin and force-pends RP_TIMER1_IRQ_3 (IRQ 23,
 * higher priority) ten times; measure() = min(nested) - min(baseline) over three
 * runs each. TIMER0_0/1/2 and TIMER1_0/2 serve cases 4/11/25/10. The RP2350 has
 * 4 priority bits, so the NVIC IPR bytes are 0xF0 (lowest) and 0x30 (higher). */
#define NESTED_SPIN  1000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif
#define NESTED_LOW_IRQ  21 /* TIMER1_IRQ_1 vector 5 + 16 */
#define NESTED_HIGH_IRQ 23 /* TIMER1_IRQ_3 vector 7 + 16 */
#define NESTED_NVIC_IPR(irq) (*(volatile uint8_t *)(0xE000E400UL + ((irq) - 16U)))

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;
static int s_nested_active;

static int nested_low_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            up_trigger_irq(NESTED_HIGH_IRQ, 0);
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
    return 0;
}

static int nested_high_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    return 0;
}

bench_status nuttx_bench_nested_create(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        return BENCH_OK;
    }
    s_nested_low_done = 0;
    s_nested_pend_h = 0;
    if (irq_attach(NESTED_LOW_IRQ, nested_low_isr, NULL) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(NESTED_HIGH_IRQ, nested_high_isr, NULL) != 0) {
        (void)irq_detach(NESTED_LOW_IRQ);
        return BENCH_PORT;
    }
    NESTED_NVIC_IPR(NESTED_LOW_IRQ) = 0xF0U;
    NESTED_NVIC_IPR(NESTED_HIGH_IRQ) = 0x30U;
    up_enable_irq(NESTED_LOW_IRQ);
    up_enable_irq(NESTED_HIGH_IRQ);
    s_nested_active = 1;
    return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_pend_h = pend_h;
    const uint32_t t0 = maxdis_cycles();
    up_trigger_irq(NESTED_LOW_IRQ, 0);
    while (s_nested_low_done == 0) {
    }
    return maxdis_cycles() - t0;
}

/* Fine-grained primitive for the native Rust nested-irq loop. */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_run(pend_h);
}

bench_status nuttx_bench_nested_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_nested_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint32_t base = nested_run(0);
    uint32_t nested = nested_run(1);
    for (int i = 0; i < 2; i++) {
        uint32_t b = nested_run(0);
        uint32_t n = nested_run(1);
        if (b < base) base = b;
        if (n < nested) nested = n;
    }
    *ticks = (nested > base) ? (uint64_t)(nested - base) : 0U;
    return BENCH_OK;
}

bench_status nuttx_bench_nested_destroy(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        up_disable_irq(NESTED_LOW_IRQ);
        up_disable_irq(NESTED_HIGH_IRQ);
        (void)irq_detach(NESTED_LOW_IRQ);
        (void)irq_detach(NESTED_HIGH_IRQ);
        s_nested_active = 0;
    }
    return BENCH_OK;
}

#else

#include <nuttx/irq.h>
#include <nuttx/arch.h>
#include <nuttx/semaphore.h>

/* STM32F4 / STM32H7 TIM2..TIM5 (APB1). The timer register layout and the NVIC
 * vectors are identical, but the RCC APB1 enable register and the APB1 timer
 * clock differ. On the STM32H723 (520 MHz SYSCLK) PCLK1 = HCLK/4 = 65 MHz and
 * the APB1 timer clock is 2*PCLK1 = 130 MHz; on the STM32F407 it is 84 MHz.
 * Both have TIM2EN..TIM5EN in bits 0..3 of the APB1 enable register. The F4 RCC
 * is at 0x40023800 and its APB1ENR at +0x40; the H7 RCC is at 0x58024400 and
 * its APB1LENR at +0xE8 (using the F4 address on the H7 bus-faults). */
#if defined(CONFIG_ARCH_CHIP_STM32H7)
#define RCC_APB1ENR       (*(volatile uint32_t *)0x580244E8UL) /* RCC_APB1LENR */
#define BENCH_APB1_TIM_HZ 130000000UL
#else
#define RCC_APB1ENR       (*(volatile uint32_t *)(0x40023800UL + 0x40))
#define BENCH_APB1_TIM_HZ 84000000UL
#endif

#define TIM2_IRQ 44 /* STM32_IRQ_FIRST(16) + 28 */
#define TIM2_BASE 0x40000000UL
#define TIM2_CR1  (*(volatile uint32_t *)(TIM2_BASE + 0x00))
#define TIM2_DIER (*(volatile uint32_t *)(TIM2_BASE + 0x0C))
#define TIM2_SR   (*(volatile uint32_t *)(TIM2_BASE + 0x10))
#define TIM2_EGR  (*(volatile uint32_t *)(TIM2_BASE + 0x14))
#define TIM2_PSC  (*(volatile uint32_t *)(TIM2_BASE + 0x28))
#define TIM2_ARR  (*(volatile uint32_t *)(TIM2_BASE + 0x2C))

#define TIM_CR1_CEN  (1u << 0)
#define TIM_CR1_OPM  (1u << 3)
#define TIM_DIER_UIE (1u << 0)
#define TIM_EGR_UG   (1u << 0)

static sem_t            s_isr_sem;
static volatile uint64_t s_isr_entry;
static int              s_isr_active;

static int tim2_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;

    TIM2_SR = 0; /* clear the update flag */
    s_isr_entry = (uint64_t)up_perf_gettime();
    (void)nxsem_post(&s_isr_sem);
    return 0;
}

static void tim2_arm(void)
{
    TIM2_SR = 0;
    TIM2_CR1 = TIM_CR1_OPM | TIM_CR1_CEN; /* one pulse */
}

bench_status nuttx_bench_isr_create(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_isr_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }

    RCC_APB1ENR |= (1u << 0); /* TIM2EN */

    TIM2_CR1 = 0;
    TIM2_PSC = (BENCH_APB1_TIM_HZ / 1000000UL) - 1UL; /* APB1 timer -> 1 MHz */
    TIM2_ARR = 20;  /* 20 us one-pulse */
    TIM2_DIER = TIM_DIER_UIE;
    TIM2_EGR = TIM_EGR_UG;
    TIM2_SR = 0;

    if (irq_attach(TIM2_IRQ, tim2_isr, NULL) != 0) {
        (void)sem_destroy(&s_isr_sem);
        return BENCH_PORT;
    }
    up_enable_irq(TIM2_IRQ);
    s_isr_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_isr_active || ticks == NULL) {
        return BENCH_PORT;
    }
    tim2_arm();
    if (nxsem_wait(&s_isr_sem) < 0) {
        return BENCH_PORT;
    }
    uint64_t wake = (uint64_t)up_perf_gettime();
    *ticks = (wake >= s_isr_entry) ? (wake - s_isr_entry) : 0;
    return BENCH_OK;
}

bench_status nuttx_bench_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_isr_active) {
        up_disable_irq(TIM2_IRQ);
        (void)irq_detach(TIM2_IRQ);
        TIM2_CR1 = 0;
        TIM2_DIER = 0;
        (void)sem_destroy(&s_isr_sem);
        s_isr_active = 0;
    }
    return BENCH_OK;
}

/* Fine-grained primitives for the native Rust isr_to_task loop. */
int bench_rust_isr_arm(void)
{
    tim2_arm();
    return 0;
}

int bench_rust_isr_wait(void)
{
    return (nxsem_wait(&s_isr_sem) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_isr_entry(void)
{
    return s_isr_entry;
}

/* ------------------------------------------- trigger to ISR entry (10) */

/* STM32F4 software-asserted minimal interrupt. The task masks interrupts,
 * reads the DWT counter, pends spare NVIC line EXTI1 with up_trigger_irq and
 * unmasks, so the exception is taken at the unmask; the handler timestamps its
 * first instruction. Value = trigger -> entry, no nominal subtraction. EXTI0
 * (22) is claimed by the board button driver, so EXTI1 (23) is used. */
#define TRIG_IRQ 23 /* STM32_IRQ_FIRST(16) + 7 = EXTI1 */

static sem_t             s_trig_sem;
static volatile uint64_t s_trig_entry;
static int               s_trig_active;

static int trig_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    s_trig_entry = (uint64_t)up_perf_gettime();
    (void)nxsem_post(&s_trig_sem);
    return 0;
}

bench_status nuttx_bench_trigger_isr_create(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_trig_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(TRIG_IRQ, trig_isr, NULL) != 0) {
        (void)sem_destroy(&s_trig_sem);
        return BENCH_PORT;
    }
    up_enable_irq(TRIG_IRQ);
    s_trig_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_trig_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_trig_sem); /* drain any stale signal */
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = (uint64_t)up_perf_gettime();
    up_trigger_irq(TRIG_IRQ, 0);
    leave_critical_section(flags); /* unmask: the exception is taken here */
    if (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) {
        return BENCH_PORT;
    }
    uint64_t entry = s_trig_entry;
    if (entry < t0) {
        return BENCH_CORRUPT;
    }
    *ticks = entry - t0;
    return BENCH_OK;
}

bench_status nuttx_bench_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        up_disable_irq(TRIG_IRQ);
        (void)irq_detach(TRIG_IRQ);
        (void)sem_destroy(&s_trig_sem);
        s_trig_active = 0;
    }
    return BENCH_OK;
}

int bench_rust_trig_drain(void)
{
    (void)nxsem_trywait(&s_trig_sem);
    return 0;
}

uint64_t bench_rust_trig_fire(void)
{
    irqstate_t flags = enter_critical_section();
    uint64_t t0 = (uint64_t)up_perf_gettime();
    up_trigger_irq(TRIG_IRQ, 0);
    leave_critical_section(flags);
    return t0;
}

int bench_rust_trig_wait(void)
{
    return (nxsem_tickwait(&s_trig_sem, MSEC2TICK(50)) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_trig_entry(void)
{
    return s_trig_entry;
}

/* ------------------------------------ periodic jitter under load (11) */

/* Case 11 on STM32F407 NuttX. TIM3 (APB1, 84 MHz) auto-reloads at an exact
 * 750 us (ARR 62999 = 126000 DWT cycles at 168 MHz) and stamps the DWT cycle
 * count of the first release as R0; TIM4 injects a busy-loop ISR every 137 us
 * (ARR 11507 = 23016 cycles, ~35% duty at an 8000-cycle budget). measure()
 * reports wake - (R0 + n*P) in the adapter's DWT ticks (168 MHz, scale 1), the
 * signed activation error against the ideal release grid, so a constant release
 * latency is retained rather than first-differenced. TIM3/TIM4 are unused by
 * the bench defconfig (TIM2 serves case 4, EXTI1 case 10). NuttX external IRQ =
 * 16 + peripheral vector: TIM3 vector 29 -> 45, TIM4 vector 30 -> 46. */
#define PJIT_TIM3_BASE 0x40000400UL
#define PJIT_TIM4_BASE 0x40000800UL
#define PJIT_CR1(b)      (*(volatile uint32_t *)((b) + 0x00))
#define PJIT_DIER(b)     (*(volatile uint32_t *)((b) + 0x0C))
#define PJIT_SR(b)       (*(volatile uint32_t *)((b) + 0x10))
#define PJIT_EGR(b)      (*(volatile uint32_t *)((b) + 0x14))
#define PJIT_PSC(b)      (*(volatile uint32_t *)((b) + 0x28))
#define PJIT_ARR(b)      (*(volatile uint32_t *)((b) + 0x2C))
#define PJIT_RCC_APB1ENR RCC_APB1ENR
#define PJIT_RCC_TIM3EN  (1u << 1)
#define PJIT_RCC_TIM4EN  (1u << 2)
#define PJIT_CR1_CEN     (1u << 0)
#define PJIT_DIER_UIE    (1u << 0)
#define PJIT_EGR_UG      (1u << 0)
#define PJIT_SR_UIF      (1u << 0)

#define PJIT_TIM3_IRQ 45 /* STM32_IRQ_FIRST(16) + 29 */
#define PJIT_TIM4_IRQ 46 /* STM32_IRQ_FIRST(16) + 30 */

/* The release grid (PJIT_P_CYCLES) is measured in DWT cycles via up_perf_gettime,
 * so it scales with the CPU clock (168 MHz F4, 520 MHz H7). The TIM3/TIM4
 * auto-reload counts depend only on the APB1 timer clock (BENCH_APB1_TIM_HZ):
 * ARR = timer_hz * period - 1. */
#if defined(CONFIG_ARCH_CHIP_STM32H7)
#define PJIT_P_CYCLES    390000U                     /* 750 us @ 520 MHz DWT */
#define PJIT_LOAD_CYCLES 71240U                      /* 137 us @ 520 MHz DWT */
#define PJIT_LOAD_TARGET 24762U                      /* busy cycles per load ISR */
#else
#define PJIT_P_CYCLES    126000U                     /* 750 us @ 168 MHz DWT */
#define PJIT_LOAD_CYCLES 23016U                      /* 137 us @ 168 MHz DWT */
#define PJIT_LOAD_TARGET 8000U                       /* busy cycles per load ISR */
#endif
/* TIM3/TIM4 are 16-bit counters (ARR <= 65535). On the H7 the 130 MHz APB1
 * timer needs 97500 counts for 750 us, which overflows a 16-bit ARR and wraps
 * to ~246 us, so every release is missed. Prescale by 2 on the H7 so the counts
 * fit; the STM32F4 (84 MHz) already fits with PSC=0. */
#if defined(CONFIG_ARCH_CHIP_STM32H7)
#define PJIT_TIMER_PSC   1U
#else
#define PJIT_TIMER_PSC   0U
#endif
#define PJIT_TIMER_ARR   ((((BENCH_APB1_TIM_HZ / (PJIT_TIMER_PSC + 1U)) / 1000UL) * 3UL / 4UL) - 1UL) /* 750 us */
#define PJIT_LOAD_ARR    ((((BENCH_APB1_TIM_HZ / (PJIT_TIMER_PSC + 1U)) / 1000000UL) * 137UL) - 1UL)  /* 137 us */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static sem_t             s_pjit_sem;
static volatile uint64_t s_pjit_r0;
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
#if PJIT_LOAD_ENABLE
static volatile uint32_t s_pjit_iters;
#endif
static int               s_pjit_active;

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

static int pjit_release_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    if ((PJIT_SR(PJIT_TIM3_BASE) & PJIT_SR_UIF) == 0U) {
        return 0;
    }
    PJIT_SR(PJIT_TIM3_BASE) = 0;
    if (s_pjit_releases == 0U) {
        s_pjit_r0 = (uint64_t)up_perf_gettime();
    }
    s_pjit_releases++;
    (void)nxsem_post(&s_pjit_sem);
    return 0;
}

#if PJIT_LOAD_ENABLE
static int pjit_load_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    PJIT_SR(PJIT_TIM4_BASE) = 0;
    pjit_load_body(s_pjit_iters);
    return 0;
}
#endif

bench_status nuttx_bench_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        return BENCH_OK;
    }
    if (sem_init(&s_pjit_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    s_pjit_r0 = 0;
    s_pjit_releases = 0;
    s_pjit_n = 0;

#if PJIT_LOAD_ENABLE
    /* Calibrate the load body to PJIT_LOAD_TARGET cycles with interrupts
     * masked (outside any timed section). Bounded to avoid spinning forever. */
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        irqstate_t flags = enter_critical_section();
        uint64_t a = (uint64_t)up_perf_gettime();
        pjit_load_body(s_pjit_iters);
        uint64_t d = (uint64_t)up_perf_gettime() - a;
        leave_critical_section(flags);
        if (d >= PJIT_LOAD_TARGET &&
            d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_iters++;
        } else {
            uint32_t next =
                (uint32_t)(((uint64_t)s_pjit_iters * PJIT_LOAD_TARGET) / d);
            s_pjit_iters = (next == 0U) ? 1U : next;
        }
    }
#endif

    PJIT_RCC_APB1ENR |= PJIT_RCC_TIM3EN | PJIT_RCC_TIM4EN;

    PJIT_CR1(PJIT_TIM3_BASE) = 0;
    PJIT_PSC(PJIT_TIM3_BASE) = PJIT_TIMER_PSC;
    PJIT_ARR(PJIT_TIM3_BASE) = PJIT_TIMER_ARR;
    PJIT_DIER(PJIT_TIM3_BASE) = PJIT_DIER_UIE;
    PJIT_EGR(PJIT_TIM3_BASE) = PJIT_EGR_UG;
    PJIT_SR(PJIT_TIM3_BASE) = 0;
    if (irq_attach(PJIT_TIM3_IRQ, pjit_release_isr, NULL) != 0) {
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    up_enable_irq(PJIT_TIM3_IRQ);

#if PJIT_LOAD_ENABLE
    PJIT_CR1(PJIT_TIM4_BASE) = 0;
    PJIT_PSC(PJIT_TIM4_BASE) = PJIT_TIMER_PSC;
    PJIT_ARR(PJIT_TIM4_BASE) = PJIT_LOAD_ARR;
    PJIT_DIER(PJIT_TIM4_BASE) = PJIT_DIER_UIE;
    PJIT_EGR(PJIT_TIM4_BASE) = PJIT_EGR_UG;
    PJIT_SR(PJIT_TIM4_BASE) = 0;
    if (irq_attach(PJIT_TIM4_IRQ, pjit_load_isr, NULL) != 0) {
        up_disable_irq(PJIT_TIM3_IRQ);
        (void)irq_detach(PJIT_TIM3_IRQ);
        (void)sem_destroy(&s_pjit_sem);
        return BENCH_PORT;
    }
    up_enable_irq(PJIT_TIM4_IRQ);
#endif

    PJIT_CR1(PJIT_TIM3_BASE) = PJIT_CR1_CEN;
#if PJIT_LOAD_ENABLE
    PJIT_CR1(PJIT_TIM4_BASE) = PJIT_CR1_CEN;
#endif
    s_pjit_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_pjit_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) {
        return BENCH_PORT;
    }
    uint64_t wake = (uint64_t)up_perf_gettime();
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced (the task was preempted longer than one
         * period, e.g. during a console flush). Report this sample as a miss
         * but resync the release counter so one transient backlog does not
         * poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return BENCH_CORRUPT;
    }
    uint64_t ideal = s_pjit_r0 + ((uint64_t)n * (uint64_t)PJIT_P_CYCLES);
    if (wake < ideal) {
        /* The release arrived before the nominal grid point: re-anchor the
         * grid on this wake so the case recovers instead of missing forever. */
        s_pjit_r0 = wake - ((uint64_t)n * (uint64_t)PJIT_P_CYCLES);
        return BENCH_CORRUPT;
    }
    *ticks = wake - ideal;
    return BENCH_OK;
}

/* Fine-grained primitives for the native Rust pjit sampling loop. */
int bench_rust_pjit_wait(void)
{
    return (nxsem_tickwait(&s_pjit_sem, MSEC2TICK(100)) < 0) ? (int)BENCH_PORT
                                                             : 0;
}

int bench_rust_pjit_read(uint64_t *ticks)
{
    uint64_t wake = (uint64_t)up_perf_gettime();
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced (the task was preempted longer than one
         * period, e.g. during a console flush). Report this sample as a miss
         * but resync the release counter so one transient backlog does not
         * poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return (int)BENCH_CORRUPT;
    }
    uint64_t ideal = s_pjit_r0 + ((uint64_t)n * (uint64_t)PJIT_P_CYCLES);
    if (wake < ideal) {
        /* The release arrived before the nominal grid point: re-anchor the
         * grid on this wake so the case recovers instead of missing forever. */
        s_pjit_r0 = wake - ((uint64_t)n * (uint64_t)PJIT_P_CYCLES);
        return (int)BENCH_CORRUPT;
    }
    *ticks = wake - ideal;
    return 0;
}

bench_status nuttx_bench_pjit_destroy(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        PJIT_CR1(PJIT_TIM3_BASE) = 0;
        PJIT_CR1(PJIT_TIM4_BASE) = 0;
        PJIT_DIER(PJIT_TIM3_BASE) = 0;
        PJIT_DIER(PJIT_TIM4_BASE) = 0;
        up_disable_irq(PJIT_TIM3_IRQ);
        up_disable_irq(PJIT_TIM4_IRQ);
        (void)irq_detach(PJIT_TIM3_IRQ);
        (void)irq_detach(PJIT_TIM4_IRQ);
        (void)sem_destroy(&s_pjit_sem);
        s_pjit_active = 0;
    }
    return BENCH_OK;
}

/* Case 25: maximum interrupt-disabled time proxy. TIM5 (free; TIM2/3/4 and
 * EXTI1 serve cases 4/10/11) is a one-pulse probe at 50 us; measure() returns
 * the delay beyond that period and the headline is the maximum sample.
 * Timestamps use the 168 MHz DWT via up_perf_gettime(). enter_critical_section()
 * masks the timer IRQ; MAXDIS_CONTROL=1 holds a ~80 us critical section as the
 * positive control. */
#define MAXDIS_TIM5_BASE 0x40000C00UL
#define MAXDIS_DIER (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x0C))
#define MAXDIS_SR   (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x10))
#define MAXDIS_EGR  (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x14))
#define MAXDIS_CNT  (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x24))
#define MAXDIS_PSC  (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x28))
#define MAXDIS_ARR  (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x2C))
#define MAXDIS_CR1  (*(volatile uint32_t *)(MAXDIS_TIM5_BASE + 0x00))
#define MAXDIS_TIM5_IRQ 66 /* STM32_IRQ_FIRST(16) + TIM5 vector 50 */
#define MAXDIS_TIM5EN   (1u << 3)
#define MAXDIS_CR1_OPM  (1u << 3)
#define MAXDIS_CR1_URS  (1u << 2)

/* TIM5 ARR in APB1 timer counts (50 us); the DWT reference below is the
 * up_perf_gettime tick (168 MHz F4, 520 MHz H7). */
#define MAXDIS_ARR_VAL            (((BENCH_APB1_TIM_HZ / 1000000UL) * 50UL) - 1UL)
#if defined(CONFIG_ARCH_CHIP_STM32H7)
#define MAXDIS_PERIOD_TICKS       26000U  /* 50 us @ 520 MHz DWT */
#define MAXDIS_CONTROL_CRIT_TICKS 41600U  /* 80 us @ 520 MHz DWT */
#else
#define MAXDIS_PERIOD_TICKS       8400U   /* 50 us @ 168 MHz DWT */
#define MAXDIS_CONTROL_CRIT_TICKS 13440U  /* 80 us @ 168 MHz DWT */
#endif

static sem_t             s_maxdis_sem;
static volatile uint64_t s_maxdis_isr_ts;
static int               s_maxdis_active;

static int maxdis_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    if ((MAXDIS_SR & PJIT_SR_UIF) == 0U) {
        return 0;
    }
    MAXDIS_SR = 0;
    s_maxdis_isr_ts = (uint64_t)up_perf_gettime();
    (void)nxsem_post(&s_maxdis_sem);
    return 0;
}

/* Fine-grained primitives for the native Rust maxdis sampling loop. */
int bench_rust_maxdis_begin(void)
{
    (void)nxsem_trywait(&s_maxdis_sem); /* drain a stale signal */
    return 0;
}

int bench_rust_maxdis_arm(void)
{
    MAXDIS_SR = 0;
    MAXDIS_CNT = 0;
    MAXDIS_CR1 = MAXDIS_CR1_OPM | MAXDIS_CR1_URS | PJIT_CR1_CEN;
    return 0;
}

int bench_rust_maxdis_wait(uint64_t t0)
{
    (void)t0;
    return (nxsem_wait(&s_maxdis_sem) < 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_maxdis_isr_ts(void)
{
    return s_maxdis_isr_ts;
}

bench_status nuttx_bench_maxdis_create(void *ctx)
{
    (void)ctx;
    if (sem_init(&s_maxdis_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    PJIT_RCC_APB1ENR |= MAXDIS_TIM5EN;
    MAXDIS_CR1 = 0;
    MAXDIS_PSC = 0;
    MAXDIS_ARR = MAXDIS_ARR_VAL;
    MAXDIS_EGR = PJIT_EGR_UG; /* latch PSC/ARR */
    MAXDIS_SR = 0;
    MAXDIS_DIER = PJIT_DIER_UIE;
    if (irq_attach(MAXDIS_TIM5_IRQ, maxdis_isr, NULL) != 0) {
        (void)sem_destroy(&s_maxdis_sem);
        return BENCH_PORT;
    }
    up_enable_irq(MAXDIS_TIM5_IRQ);
    s_maxdis_active = 1;
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_maxdis_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)nxsem_trywait(&s_maxdis_sem); /* drain a stale signal */
    uint64_t t0 = (uint64_t)up_perf_gettime();
    MAXDIS_SR = 0;
    MAXDIS_CNT = 0;
    MAXDIS_CR1 = MAXDIS_CR1_OPM | MAXDIS_CR1_URS | PJIT_CR1_CEN;
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
    irqstate_t flags = enter_critical_section();
    uint64_t c0 = (uint64_t)up_perf_gettime();
    while (((uint64_t)up_perf_gettime() - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
    }
    leave_critical_section(flags);
#endif
    if (nxsem_wait(&s_maxdis_sem) < 0) {
        return BENCH_PORT;
    }
    uint64_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = elapsed - MAXDIS_PERIOD_TICKS;
    return BENCH_OK;
}

bench_status nuttx_bench_maxdis_destroy(void *ctx)
{
    (void)ctx;
    if (s_maxdis_active) {
        MAXDIS_CR1 = 0;
        MAXDIS_DIER = 0;
        up_disable_irq(MAXDIS_TIM5_IRQ);
        (void)irq_detach(MAXDIS_TIM5_IRQ);
        (void)sem_destroy(&s_maxdis_sem);
        s_maxdis_active = 0;
    }
    return BENCH_OK;
}

/* Case 20: nested interrupt overhead. EXTI2 (external IRQ 24, lowest priority)
 * runs a fixed-work spin and force-pends EXTI3 (external IRQ 25, higher
 * priority) ten times; measure() = min(nested) - min(baseline) over three runs
 * each. NESTED_CONTROL=1 disables the higher IRQ and must report 0. EXTI0 is
 * the board button, EXTI1 is case 10, TIM2/3/4/5 serve cases 4/11/25. */
#define NESTED_SPIN  2000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif
#define NESTED_LOW_IRQ  24 /* STM32_IRQ_FIRST(16) + EXTI2(8) */
#define NESTED_HIGH_IRQ 25 /* STM32_IRQ_FIRST(16) + EXTI3(9) */
/* NuttX's up_prioritize_irq needs CONFIG_ARCH_IRQPRIO (off in this defconfig),
 * so set the NVIC priority byte directly. External IRQ n -> IPR byte n-16. */
#define NESTED_NVIC_IPR(irq) (*(volatile uint8_t *)(0xE000E400UL + ((irq) - 16U)))

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;
static int s_nested_active;

static int nested_low_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            up_trigger_irq(NESTED_HIGH_IRQ, 0);
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
    return 0;
}

static int nested_high_isr(int irq, void *context, void *arg)
{
    (void)irq;
    (void)context;
    (void)arg;
    return 0;
}

bench_status nuttx_bench_nested_create(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        return BENCH_OK;
    }
    s_nested_low_done = 0;
    s_nested_pend_h = 0;
    if (irq_attach(NESTED_LOW_IRQ, nested_low_isr, NULL) != 0) {
        return BENCH_PORT;
    }
    if (irq_attach(NESTED_HIGH_IRQ, nested_high_isr, NULL) != 0) {
        (void)irq_detach(NESTED_LOW_IRQ);
        return BENCH_PORT;
    }
    NESTED_NVIC_IPR(NESTED_LOW_IRQ) = 0xF0U;  /* lowest priority */
    NESTED_NVIC_IPR(NESTED_HIGH_IRQ) = 0x30U; /* higher: preempts EXTI2 */
    up_enable_irq(NESTED_LOW_IRQ);
    up_enable_irq(NESTED_HIGH_IRQ);
    s_nested_active = 1;
    return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_pend_h = pend_h;
    const uint32_t t0 = (uint32_t)up_perf_gettime();
    up_trigger_irq(NESTED_LOW_IRQ, 0);
    while (s_nested_low_done == 0) {
    }
    return (uint32_t)up_perf_gettime() - t0;
}

/* Fine-grained primitive for the native Rust nested-irq loop. */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_run(pend_h);
}

bench_status nuttx_bench_nested_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_nested_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint32_t base = nested_run(0);
    uint32_t nested = nested_run(1);
    for (int i = 0; i < 2; i++) {
        uint32_t b = nested_run(0);
        uint32_t n = nested_run(1);
        if (b < base) base = b;
        if (n < nested) nested = n;
    }
    *ticks = (nested > base) ? (uint64_t)(nested - base) : 0U;
    return BENCH_OK;
}

bench_status nuttx_bench_nested_destroy(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        up_disable_irq(NESTED_LOW_IRQ);
        up_disable_irq(NESTED_HIGH_IRQ);
        (void)irq_detach(NESTED_LOW_IRQ);
        (void)irq_detach(NESTED_HIGH_IRQ);
        s_nested_active = 0;
    }
    return BENCH_OK;
}

#endif /* CONFIG_ARCH_CHIP_ESP32C6 */

/* Complete C measurement paths exposed to the Rust adapter.  Rust calls these
 * instead of sequencing begin/arm/wait through separate FFI calls, so both
 * languages use the same timestamp boundaries. */
bench_status bench_isr_measure(uint64_t *ticks)
{
    return nuttx_bench_isr_measure(NULL, ticks);
}

bench_status bench_pjit_measure(uint64_t *ticks)
{
    return nuttx_bench_pjit_measure(NULL, ticks);
}

bench_status bench_maxdis_measure(uint64_t *ticks)
{
    return nuttx_bench_maxdis_measure(NULL, ticks);
}
