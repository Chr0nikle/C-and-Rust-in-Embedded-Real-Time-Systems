/* NuttX bench_port adapter.
 *
 *   now_ticks        : ESP32-C6 reads the CPU performance counter (custom CSR
 *                      0x7e2, CPU_CLK 160 MHz) so microsecond latencies are
 *                      resolved; the NuttX system tick is only 100 Hz. STM32F4
 *                      uses the DWT cycle counter via up_perf_gettime() (needs
 *                      CONFIG_ARCH_PERF_EVENTS), which the STM32 start code
 *                      initialises with SYSCLK.
 *   ticks_per_second : CONFIG_ESPRESSIF_CPU_FREQ_MHZ * 1e6 (C6) or
 *                      168,000,000 (STM32F407 SYSCLK).
 *   queue            : POSIX mqueue with capacity 1 and a 4-byte payload.
 *   handoff          : pthread peer (higher static priority) + two POSIX
 *                      semaphores; the peer timestamps its own wake.
 *   jitter           : clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME) with a
 *                      1 ms grid; the reported value is |interval - period|.
 *   isr_to_task      : STM32F4 TIM2 harness in isr.c; unsupported on the C6,
 *                      which therefore does not advertise case 4.
 *
 * Create/destroy run outside the timed sections; the timed path only signals,
 * waits and timestamps.
 */
#define _POSIX_C_SOURCE 200809L
#include "rtos_bench.h"
#include <time.h>
#include <mqueue.h>
#include <fcntl.h>
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <sched.h>
#include <stdlib.h>
#ifdef __NuttX__
#include <malloc.h> /* struct mallinfo / mallinfo() are NuttX kernel APIs */
#endif
#if defined(CONFIG_ARCH_CHIP_ESP32C6) || defined(CONFIG_ARCH_CHIP_STM32) || \
    defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_RP23XX)
#include <nuttx/wdog.h>
#else
/* Host syntax-check build: the NuttX tree is unavailable, so provide opaque
 * stand-ins. These are compiled but never run on the host. */
struct wdog_s {
    uintptr_t opaque[8];
};
typedef uintptr_t wdparm_t;
extern int wd_start(struct wdog_s *wdog, int delay, void (*entry)(wdparm_t), wdparm_t arg);
extern int wd_cancel(struct wdog_s *wdog);
#endif

#if defined(CONFIG_ARCH_CHIP_NRF52)
/* The nRF52 port does not select ARCH_HAVE_PERF_EVENTS, so NuttX's up_perf_*
 * (DWT-backed armv7-m) is unavailable. Read the Cortex-M4 DWT cycle counter
 * directly at the 64 MHz core clock. CYCCNT is 32-bit (wraps ~67 s at 64 MHz);
 * every measured interval is far below that. Raw registers keep this file
 * independent of NuttX arch internals. */
#define NRF52_DWT_HZ 64000000ULL
#define NRF52_DEMCR          (*(volatile uint32_t *)0xe000edfcUL)
#define NRF52_DWT_CTRL       (*(volatile uint32_t *)0xe0001000UL)
#define NRF52_DWT_CYCCNT     (*(volatile uint32_t *)0xe0001004UL)
#define NRF52_DEMCR_TRCENA   (1UL << 24)
#define NRF52_DWT_CYCCNTENA  (1UL << 0)

static inline void nrf52_dwt_init(void)
{
    /* Idempotent: enabling must NOT reset CYCCNT. The Rust shim calls this on
     * every timestamp read, and zeroing the counter mid-run would make deltas
     * negative (case corrupt). CYCCNT is already 0 out of reset. */
    NRF52_DEMCR |= NRF52_DEMCR_TRCENA;
    NRF52_DWT_CTRL |= NRF52_DWT_CYCCNTENA;
}

static inline uint64_t nrf52_dwt_cycles(void)
{
    return (uint64_t)NRF52_DWT_CYCCNT;
}
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
/* The RP2350 Cortex-M33 does not select ARCH_HAVE_PERF_EVENTS, so up_perf_*
 * (DWT-backed armv8-m) is unavailable. Read the Cortex-M33 DWT cycle counter
 * directly at the 150 MHz system clock (board.h BOARD_SYS_FREQ). CYCCNT is
 * 32-bit (wraps ~28.6 s at 150 MHz); every measured interval is far below that.
 * Enabling must be idempotent and must not reset CYCCNT, because the Rust shim
 * re-asserts it on every timestamp read. */
#define RP23XX_DWT_HZ 150000000ULL
#define RP23XX_DEMCR          (*(volatile uint32_t *)0xe000edfcUL)
#define RP23XX_DWT_CTRL       (*(volatile uint32_t *)0xe0001000UL)
#define RP23XX_DWT_CYCCNT     (*(volatile uint32_t *)0xe0001004UL)
#define RP23XX_DEMCR_TRCENA   (1UL << 24)
#define RP23XX_DWT_CYCCNTENA  (1UL << 0)

static inline void rp23xx_dwt_init(void)
{
    RP23XX_DEMCR |= RP23XX_DEMCR_TRCENA;
    RP23XX_DWT_CTRL |= RP23XX_DWT_CYCCNTENA;
}

static inline uint64_t rp23xx_dwt_cycles(void)
{
    return (uint64_t)RP23XX_DWT_CYCCNT;
}
#elif !defined(CONFIG_ARCH_CHIP_ESP32C6)
/* ARMv7-M DWT cycle counter, provided by CONFIG_ARCH_PERF_EVENTS. Declared
 * here so this file also passes the host syntax check without NuttX headers. */
clock_t up_perf_gettime(void);
#else
/* ESP32-C6 performance counter. SOC_CPU_HAS_CSR_PC=1 on the C6, so the standard
 * `cycle` CSR is illegal (MCAUSE 2); the CPU exposes a custom 32-bit cycle
 * counter at CSR 0x7e2, enabled through 0x7e0/0x7e1 (as the ESP32-C3 port
 * does). The 100 Hz NuttX system tick is far too coarse for microsecond
 * latencies. The 32-bit counter suffices: intervals are microseconds and it
 * wraps every ~27 s at 160 MHz. */
#define C6_CSR_PCER 0x7e0
#define C6_CSR_PCMR 0x7e1
#define C6_CSR_PCCR 0x7e2

static inline void c6_perf_enable(void)
{
    uint32_t one = 1;
    __asm__ volatile("csrw 0x7e0, %0" ::"r"(one));
    __asm__ volatile("csrw 0x7e1, %0" ::"r"(one));
}

static inline uint32_t c6_cycle_count(void)
{
    uint32_t v;
    __asm__ volatile("csrr %0, 0x7e2" : "=r"(v));
    return v;
}
#endif

#if defined(CONFIG_ARCH_CHIP_ESP32C6)
/* ISR-safe read of the C6 performance counter, used by the case-4 harness so
 * its timestamps share the port's tick base. Enabling is done once at port
 * init; an ISR must not rewrite the CSR configuration. */
uint64_t nuttx_bench_c6_now_ticks(void)
{
    return (uint64_t)c6_cycle_count();
}
#endif

#if defined(CONFIG_ARCH_CHIP_NRF52)
/* ISR-safe read of the DWT counter for the isr.c case-4/10 harnesses, so their
 * timestamps share the port's tick base. Enabled once at port init. */
uint64_t nuttx_bench_tick_raw(void)
{
    /* The DWT can be disabled while the core is idle.  Re-asserting the
     * enable bits is idempotent and does not reset CYCCNT; doing it here keeps
     * ISR-entry and wake timestamps live even when the Rust path has just
     * passed through an idle/low-power interval. */
    nrf52_dwt_init();
    return nrf52_dwt_cycles();
}
#endif

#if defined(CONFIG_ARCH_CHIP_RP23XX)
/* ISR-safe read of the RP2350 Cortex-M33 DWT counter for the isr.c case-4/10/11
 * harnesses, so their timestamps share the port's tick base. Enabled once at
 * port init. */
uint64_t nuttx_bench_tick_raw(void)
{
    return rp23xx_dwt_cycles();
}
#endif

/* STM32F407 SYSCLK (HSE 8 MHz -> PLL 168 MHz), matching the FreeRTOS port. */
#define STM32F4_SYSCLK_HZ 168000000ULL
/* STM32H723 SYSCLK on the FK723M1 board with the NuttX nucleo-h723zg port:
 * 25 MHz HSE crystal -> PLL1 (M=5,N=104,P=1) -> 520 MHz at VOS0 (RM0468 VOS0
 * is selected by NuttX's default PWR_D3CR VOS=0b11). 520 MHz is the common H7
 * clock for all four paths. The ARMv7-M DWT runs at the CPU clock. */
#define STM32H7_SYSCLK_HZ 520000000ULL

#define MQ_NAME        "/rtos_bench_mq"
#define MQ_MAX_MESSAGES 1
#define MQ_MESSAGE_SIZE 4
#define MQ_FLAGS       (O_RDWR | O_CREAT | O_EXCL)
#define MQ_MODE        0644

/* Scheduled period for periodic_jitter, 1 ms expressed in the port's ticks. */
#define JITTER_PERIOD_NS 1000000

static mqd_t s_mq = (mqd_t)-1;
static uint64_t s_ticks_per_second = 1;

/* Monotonic tick source shared by every case. */
static bench_status now_ticks_raw(uint64_t *ticks)
{
    if (ticks == NULL) {
        return BENCH_INVALID;
    }
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
    *ticks = (uint64_t)c6_cycle_count();
#elif defined(CONFIG_ARCH_CHIP_NRF52)
    *ticks = nrf52_dwt_cycles();
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
    *ticks = rp23xx_dwt_cycles();
#else
    /* DWT CYCCNT (32-bit, 168 MHz => wraps every ~25.56 s; every measured
     * interval is far below that, so a sample delta never wraps). The value is
     * cast to u64 without a software high-word; any future case with a window
     * longer than the 32-bit wrap must add rollover extension (see the ThreadX/
     * ChibiOS/RT-Thread/RIOT/RTEMS ports). */
    *ticks = (uint64_t)up_perf_gettime();
#endif
    return BENCH_OK;
}

static bench_status port_now_ticks(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    return now_ticks_raw(ticks);
}

static bench_status port_queue_create(void *ctx)
{
    (void)ctx;
    mq_unlink(MQ_NAME);
    s_mq = mq_open(MQ_NAME, MQ_FLAGS, MQ_MODE, NULL);
    if (s_mq == (mqd_t)-1) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_queue_send(void *ctx, uint32_t value)
{
    (void)ctx;
    if (s_mq == (mqd_t)-1) {
        return BENCH_PORT;
    }
    uint32_t v = value;
    if (mq_send(s_mq, (const char *)&v, sizeof(v), 0) == -1) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_queue_receive(void *ctx, uint32_t *value)
{
    (void)ctx;
    if (value == NULL) {
        return BENCH_INVALID;
    }
    if (s_mq == (mqd_t)-1) {
        return BENCH_PORT;
    }
    uint32_t v = 0;
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_mq, (char *)&v, sizeof(v), &prio);
    if (n != sizeof(v)) {
        return BENCH_PORT;
    }
    *value = v;
    return BENCH_OK;
}

static bench_status port_queue_destroy(void *ctx)
{
    (void)ctx;
    if (s_mq != (mqd_t)-1) {
        mq_close(s_mq);
        s_mq = (mqd_t)-1;
    }
    mq_unlink(MQ_NAME);
    return BENCH_OK;
}

/* ------------------------------------------------------------ task handoff */

/* The peer blocks on s_handoff_signal; the benchmark task posts it and waits on
 * s_handoff_done. The peer timestamps its own wake after sem_wait returns. It
 * runs at the highest static priority so the post wakes it immediately. */
static sem_t            s_handoff_signal;
static sem_t            s_handoff_done;
static pthread_t        s_handoff_peer;
static volatile uint64_t s_handoff_wake;
static volatile int     s_handoff_quit;
static int              s_handoff_active;

static void *handoff_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (sem_wait(&s_handoff_signal) != 0) {
            continue;
        }
        if (s_handoff_quit) {
            break;
        }
        uint64_t wake = 0;
        if (now_ticks_raw(&wake) == BENCH_OK) {
            s_handoff_wake = wake;
        }
        (void)sem_post(&s_handoff_done);
    }
    return NULL;
}

static bench_status port_handoff_create(void *ctx)
{
    (void)ctx;
    s_handoff_quit = 0;
    if (sem_init(&s_handoff_signal, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (sem_init(&s_handoff_done, 0, 0) != 0) {
        return BENCH_PORT;
    }

    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    sp.sched_priority = sched_get_priority_max(SCHED_FIFO);
    (void)pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_handoff_peer, &attr, handoff_peer_entry, NULL);
    (void)pthread_attr_destroy(&attr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_handoff_active = 1;
    return BENCH_OK;
}

static bench_status port_handoff_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_handoff_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (sem_post(&s_handoff_signal) != 0) {
        return BENCH_PORT;
    }
    if (sem_wait(&s_handoff_done) != 0) {
        return BENCH_PORT;
    }
    *ticks = s_handoff_wake;
    return BENCH_OK;
}

static bench_status port_handoff_destroy(void *ctx)
{
    (void)ctx;
    if (s_handoff_active) {
        s_handoff_quit = 1;
        (void)sem_post(&s_handoff_signal);
        (void)pthread_join(s_handoff_peer, NULL);
        s_handoff_active = 0;
    }
    (void)sem_destroy(&s_handoff_signal);
    (void)sem_destroy(&s_handoff_done);
    return BENCH_OK;
}

/* ------------------------------------------------------- periodic jitter */

static struct timespec s_jitter_next;
static uint64_t        s_jitter_prev;

static void jitter_advance(void)
{
    s_jitter_next.tv_nsec += JITTER_PERIOD_NS;
    while (s_jitter_next.tv_nsec >= 1000000000L) {
        s_jitter_next.tv_nsec -= 1000000000L;
        s_jitter_next.tv_sec += 1;
    }
}

static bench_status port_jitter_create(void *ctx)
{
    (void)ctx;
    if (clock_gettime(CLOCK_MONOTONIC, &s_jitter_next) != 0) {
        return BENCH_PORT;
    }
    jitter_advance();
    if (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &s_jitter_next, NULL) != 0) {
        return BENCH_PORT;
    }
    return now_ticks_raw(&s_jitter_prev);
}

static bench_status port_jitter_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_INVALID;
    }
    jitter_advance();
    if (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &s_jitter_next, NULL) != 0) {
        return BENCH_PORT;
    }
    uint64_t now = 0;
    bench_status status = now_ticks_raw(&now);
    if (status != BENCH_OK) {
        return status;
    }
    uint64_t period = s_ticks_per_second / 1000ULL; /* 1 ms in port ticks */
    uint64_t interval = (now >= s_jitter_prev) ? (now - s_jitter_prev) : 0;
    *ticks = (interval >= period) ? (interval - period) : (period - interval);
    s_jitter_prev = now;
    return BENCH_OK;
}

static bench_status port_jitter_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

/* ------------------------------------------------------------ isr_to_task */

/* Implemented in isr.c (NuttX/STM32F4 TIM2 harness; unsupported on the C6). */
/* ---------------------------------------------- yield / context switch (5) */

/* An equal-priority peer loops on sched_yield(); the benchmark yields and
 * confirms the peer ran, timing one full round trip. NuttX SCHED_FIFO
 * re-queues a yielding thread at the tail of its priority list, so an
 * equal-priority ready peer runs. */
static pthread_t         s_yield_peer;
static volatile uint32_t s_yield_peer_runs;
static volatile uint32_t s_yield_peer_started;
static volatile int      s_yield_quit;

static void *yield_peer_entry(void *arg)
{
    (void)arg;
    s_yield_peer_started = 1;
    while (!s_yield_quit) {
        s_yield_peer_runs++;
        sched_yield();
    }
    return NULL;
}

/* Task-count sweep (case 5): extra cooperative same-priority pthreads. */
#define PEERS_MAX_EXTRA 32U
static pthread_t    s_peers_extra[PEERS_MAX_EXTRA];
static uint32_t     s_peers_extra_count;
static volatile int s_peers_quit[PEERS_MAX_EXTRA];

static void *peers_extra_entry(void *arg)
{
    uintptr_t idx = (uintptr_t)arg;
    while (!s_peers_quit[idx]) {
        sched_yield();
    }
    return NULL;
}

static bench_status port_peers_set(void *ctx, uint32_t count)
{
    (void)ctx;
    uint32_t want = (count > 0U) ? (count - 1U) : 0U;
    if (want > PEERS_MAX_EXTRA) {
        return BENCH_PORT;
    }
    while (s_peers_extra_count > want) {
        s_peers_extra_count--;
        s_peers_quit[s_peers_extra_count] = 1;
        (void)pthread_join(s_peers_extra[s_peers_extra_count], NULL);
    }
    if (s_peers_extra_count == want) {
        return BENCH_OK;
    }
    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    int policy = SCHED_FIFO;
    if (pthread_getschedparam(pthread_self(), &policy, &sp) != 0) {
        (void)pthread_attr_destroy(&attr);
        return BENCH_PORT;
    }
    (void)pthread_attr_setschedpolicy(&attr, policy);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    (void)pthread_attr_setstacksize(&attr, 1024);
    while (s_peers_extra_count < want) {
        s_peers_quit[s_peers_extra_count] = 0;
        int rc = pthread_create(&s_peers_extra[s_peers_extra_count], &attr,
                                peers_extra_entry,
                                (void *)(uintptr_t)s_peers_extra_count);
        if (rc != 0) {
            (void)pthread_attr_destroy(&attr);
            return BENCH_PORT;
        }
        s_peers_extra_count++;
    }
    (void)pthread_attr_destroy(&attr);
    return BENCH_OK;
}

static bench_status port_yield_create(void *ctx)
{
    (void)ctx;
    s_yield_quit = 0;
    s_yield_peer_runs = 0;
    s_yield_peer_started = 0;

    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    int policy = SCHED_FIFO;
    if (pthread_getschedparam(pthread_self(), &policy, &sp) != 0) {
        (void)pthread_attr_destroy(&attr);
        return BENCH_PORT;
    }
    (void)pthread_attr_setschedpolicy(&attr, policy);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_yield_peer, &attr, yield_peer_entry, NULL);
    (void)pthread_attr_destroy(&attr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    for (int i = 0; i < 1000 && s_yield_peer_started == 0; i++) {
        sched_yield();
    }
    if (s_yield_peer_started == 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_yield_now(void *ctx)
{
    (void)ctx;
    if (!s_yield_peer_started) {
        return BENCH_PORT;
    }
    uint32_t before = s_yield_peer_runs;
    for (int i = 0; i < 64; i++) {
        sched_yield();
        if (s_yield_peer_runs != before) {
            return BENCH_OK;
        }
    }
    return BENCH_CORRUPT;
}

static bench_status port_yield_destroy(void *ctx)
{
    (void)ctx;
    while (s_peers_extra_count > 0U) {
        s_peers_extra_count--;
        s_peers_quit[s_peers_extra_count] = 1;
        (void)pthread_join(s_peers_extra[s_peers_extra_count], NULL);
    }
    if (s_yield_peer_started) {
        s_yield_quit = 1;
        (void)pthread_join(s_yield_peer, NULL);
        s_yield_peer_started = 0;
    }
    return BENCH_OK;
}

/* --------------------------------------------------- preempt latency (6) */

/* H blocks on s_preempt_sem one priority above the benchmark thread (L); L
 * posts, H is released immediately, timestamps itself and blocks again so L
 * resumes and returns the delta. */
static sem_t             s_preempt_sem;
static pthread_t         s_preempt_task;
static volatile uint64_t s_preempt_wake;
static volatile uint32_t s_preempt_seq;
static volatile uint32_t s_preempt_ready;
static volatile int      s_preempt_quit;

static void *preempt_high_entry(void *arg)
{
    (void)arg;
    s_preempt_ready = 1;
    while (!s_preempt_quit) {
        if (sem_wait(&s_preempt_sem) != 0) {
            continue;
        }
        if (s_preempt_quit) {
            break;
        }
        uint64_t wake = 0;
        if (now_ticks_raw(&wake) == BENCH_OK) {
            s_preempt_wake = wake;
        }
        s_preempt_seq++;
    }
    return NULL;
}

static bench_status port_preempt_create(void *ctx)
{
    (void)ctx;
    s_preempt_quit = 0;
    s_preempt_wake = 0;
    s_preempt_seq = 0;
    s_preempt_ready = 0;
    if (sem_init(&s_preempt_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }

    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        (void)sem_destroy(&s_preempt_sem);
        return BENCH_PORT;
    }
    struct sched_param sp;
    int policy = SCHED_FIFO;
    if (pthread_getschedparam(pthread_self(), &policy, &sp) != 0) {
        (void)pthread_attr_destroy(&attr);
        (void)sem_destroy(&s_preempt_sem);
        return BENCH_PORT;
    }
    /* One priority above the benchmark thread so the post preempts at once. */
    sp.sched_priority += 1;
    if (sp.sched_priority > sched_get_priority_max(policy)) {
        (void)pthread_attr_destroy(&attr);
        (void)sem_destroy(&s_preempt_sem);
        return BENCH_PORT; /* no higher priority available */
    }
    (void)pthread_attr_setschedpolicy(&attr, policy);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_preempt_task, &attr, preempt_high_entry, NULL);
    (void)pthread_attr_destroy(&attr);
    if (rc != 0) {
        (void)sem_destroy(&s_preempt_sem);
        return BENCH_PORT;
    }
    for (int i = 0; i < 1000 && s_preempt_ready == 0; i++) {
        sched_yield();
    }
    if (s_preempt_ready == 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_preempt_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL || s_preempt_ready == 0) {
        return BENCH_PORT;
    }
    uint32_t seq = s_preempt_seq;
    uint64_t start = 0;
    if (now_ticks_raw(&start) != BENCH_OK) {
        return BENCH_PORT;
    }
    if (sem_post(&s_preempt_sem) != 0) {
        return BENCH_PORT;
    }
    if (s_preempt_seq == seq) {
        return BENCH_CORRUPT; /* no preemption */
    }
    uint64_t wake = s_preempt_wake;
    *ticks = (wake >= start) ? (wake - start) : 0;
    return BENCH_OK;
}

static bench_status port_preempt_destroy(void *ctx)
{
    (void)ctx;
    if (s_preempt_ready) {
        s_preempt_quit = 1;
        (void)sem_post(&s_preempt_sem);
        (void)pthread_join(s_preempt_task, NULL);
        s_preempt_ready = 0;
    }
    (void)sem_destroy(&s_preempt_sem);
    return BENCH_OK;
}

/* ------------------------------------------------------- ipc ping-pong */

/* Two POSIX message queues carry a request and its echo; the peer runs at the
 * highest static priority so each request wakes it immediately. measure() times
 * the full round trip (two context switches) and rejects a mismatched reply. */

#define MQ_IPC_REQ "/rtos_bench_ipc_req"
#define MQ_IPC_REP "/rtos_bench_ipc_rep"

static mqd_t         s_ipc_req = (mqd_t)-1;
static mqd_t         s_ipc_rep = (mqd_t)-1;
static pthread_t     s_ipc_peer;
static volatile int  s_ipc_quit;
static int           s_ipc_active;

static void *ipc_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t v = 0;
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_ipc_req, (char *)&v, sizeof(v), &prio);
        if (n != sizeof(v)) {
            continue;
        }
        if (s_ipc_quit) {
            break;
        }
        (void)mq_send(s_ipc_rep, (const char *)&v, sizeof(v), 0);
    }
    return NULL;
}

static bench_status port_ipc_create(void *ctx)
{
    (void)ctx;
    s_ipc_quit = 0;
    mq_unlink(MQ_IPC_REQ);
    mq_unlink(MQ_IPC_REP);
    s_ipc_req = mq_open(MQ_IPC_REQ, MQ_FLAGS, MQ_MODE, NULL);
    s_ipc_rep = mq_open(MQ_IPC_REP, MQ_FLAGS, MQ_MODE, NULL);
    if (s_ipc_req == (mqd_t)-1 || s_ipc_rep == (mqd_t)-1) {
        return BENCH_PORT;
    }
    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    sp.sched_priority = sched_get_priority_max(SCHED_FIFO);
    (void)pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_ipc_peer, &attr, ipc_peer_entry, NULL);
    (void)pthread_attr_destroy(&attr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_ipc_active = 1;
    return BENCH_OK;
}

static bench_status port_ipc_measure(void *ctx, uint32_t value, uint64_t *ticks)
{
    (void)ctx;
    if (!s_ipc_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start = 0;
    if (now_ticks_raw(&start) != BENCH_OK) {
        return BENCH_PORT;
    }
    uint32_t v = value;
    if (mq_send(s_ipc_req, (const char *)&v, sizeof(v), 0) == -1) {
        return BENCH_PORT;
    }
    uint32_t received_value = 0;
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_ipc_rep, (char *)&received_value, sizeof(received_value), &prio);
    uint64_t end = 0;
    if (now_ticks_raw(&end) != BENCH_OK) {
        return BENCH_PORT;
    }
    if (n != sizeof(received_value)) {
        return BENCH_PORT;
    }
    if (received_value != value) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}

static bench_status port_ipc_destroy(void *ctx)
{
    (void)ctx;
    if (s_ipc_active) {
        s_ipc_quit = 1;
        uint32_t sentinel = 0;
        (void)mq_send(s_ipc_req, (const char *)&sentinel, sizeof(sentinel), 0);
        (void)pthread_join(s_ipc_peer, NULL);
        s_ipc_active = 0;
    }
    if (s_ipc_req != (mqd_t)-1) {
        mq_close(s_ipc_req);
        s_ipc_req = (mqd_t)-1;
    }
    if (s_ipc_rep != (mqd_t)-1) {
        mq_close(s_ipc_rep);
        s_ipc_rep = (mqd_t)-1;
    }
    mq_unlink(MQ_IPC_REQ);
    mq_unlink(MQ_IPC_REP);
    return BENCH_OK;
}

/* Case 18: IPC payload sweep. POSIX message queues carry variable-length
 * messages natively, so one request/reply pair and one echo peer serve every
 * payload size; the timed region includes the kernel's size-proportional copy.
 * measure() sends `payload_bytes` and rejects a short or mismatched reply.
 * Timing uses now_ticks_raw(), the board's declared counter. */
#define MQ_IPC_PAYLOAD_REQ "/rtos_bench_ipc_payload_req"
#define MQ_IPC_PAYLOAD_REP "/rtos_bench_ipc_payload_rep"
#define IPC_PAYLOAD_MAX 256

static mqd_t          s_ipc_payload_req = (mqd_t)-1;
static mqd_t          s_ipc_payload_rep = (mqd_t)-1;
static pthread_t      s_ipc_payload_peer;
static volatile int   s_ipc_payload_quit;
static int            s_ipc_payload_active;
static uint8_t        s_ipc_payload_tx[IPC_PAYLOAD_MAX];
static uint8_t        s_ipc_payload_rx[IPC_PAYLOAD_MAX];

static void *ipc_payload_peer_entry(void *arg)
{
    (void)arg;
    uint8_t buf[IPC_PAYLOAD_MAX];
    for (;;) {
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_ipc_payload_req, (char *)buf, sizeof(buf), &prio);
        if (n < 0) {
            continue;
        }
        if (s_ipc_payload_quit) {
            break;
        }
        (void)mq_send(s_ipc_payload_rep, (const char *)buf, (size_t)n, 0);
    }
    return NULL;
}

static bench_status port_ipc_payload_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < IPC_PAYLOAD_MAX; i++) {
        s_ipc_payload_tx[i] = (uint8_t)(0xA5u ^ i);
    }
    s_ipc_payload_quit = 0;
    mq_unlink(MQ_IPC_PAYLOAD_REQ);
    mq_unlink(MQ_IPC_PAYLOAD_REP);
    struct mq_attr attr;
    attr.mq_maxmsg = 1;
    attr.mq_msgsize = IPC_PAYLOAD_MAX;
    attr.mq_flags = 0;
    s_ipc_payload_req = mq_open(MQ_IPC_PAYLOAD_REQ, MQ_FLAGS, MQ_MODE, &attr);
    s_ipc_payload_rep = mq_open(MQ_IPC_PAYLOAD_REP, MQ_FLAGS, MQ_MODE, &attr);
    if (s_ipc_payload_req == (mqd_t)-1 || s_ipc_payload_rep == (mqd_t)-1) {
        return BENCH_PORT;
    }
    pthread_attr_t pattr;
    if (pthread_attr_init(&pattr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    sp.sched_priority = sched_get_priority_max(SCHED_FIFO);
    (void)pthread_attr_setschedpolicy(&pattr, SCHED_FIFO);
    (void)pthread_attr_setschedparam(&pattr, &sp);
    (void)pthread_attr_setinheritsched(&pattr, PTHREAD_EXPLICIT_SCHED);
    /* The peer holds a 256-byte receive buffer; give it a comfortable stack so
     * its behaviour does not depend on the default pthread stack size. */
    (void)pthread_attr_setstacksize(&pattr, 4096);
    int rc = pthread_create(&s_ipc_payload_peer, &pattr, ipc_payload_peer_entry, NULL);
    (void)pthread_attr_destroy(&pattr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_ipc_payload_active = 1;
    return BENCH_OK;
}

static bench_status port_ipc_payload_measure(void *ctx, uint32_t payload_bytes, uint64_t *ticks)
{
    (void)ctx;
    if (!s_ipc_payload_active || ticks == NULL || payload_bytes == 0
        || payload_bytes > IPC_PAYLOAD_MAX) {
        return BENCH_PORT;
    }
    uint64_t start = 0;
    if (now_ticks_raw(&start) != BENCH_OK) {
        return BENCH_PORT;
    }
    if (mq_send(s_ipc_payload_req, (const char *)s_ipc_payload_tx, payload_bytes, 0) == -1) {
        return BENCH_PORT;
    }
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_ipc_payload_rep, (char *)s_ipc_payload_rx, IPC_PAYLOAD_MAX, &prio);
    uint64_t end = 0;
    if (now_ticks_raw(&end) != BENCH_OK) {
        return BENCH_PORT;
    }
    if (n != (ssize_t)payload_bytes) {
        return BENCH_CORRUPT;
    }
    for (uint32_t i = 0; i < payload_bytes; i++) {
        if (s_ipc_payload_rx[i] != s_ipc_payload_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    *ticks = end - start;
    return BENCH_OK;
}

static bench_status port_ipc_payload_destroy(void *ctx)
{
    (void)ctx;
    if (s_ipc_payload_active) {
        s_ipc_payload_quit = 1;
        uint8_t sentinel = 0;
        (void)mq_send(s_ipc_payload_req, (const char *)&sentinel, 1, 0);
        (void)pthread_join(s_ipc_payload_peer, NULL);
        s_ipc_payload_active = 0;
    }
    if (s_ipc_payload_req != (mqd_t)-1) {
        mq_close(s_ipc_payload_req);
        s_ipc_payload_req = (mqd_t)-1;
    }
    if (s_ipc_payload_rep != (mqd_t)-1) {
        mq_close(s_ipc_payload_rep);
        s_ipc_payload_rep = (mqd_t)-1;
    }
    mq_unlink(MQ_IPC_PAYLOAD_REQ);
    mq_unlink(MQ_IPC_PAYLOAD_REP);
    return BENCH_OK;
}

/* Case 19: IPC throughput. A producer pushes BENCH_TP_MSGS 64-byte messages into
 * a bounded POSIX message queue that a SCHED_FIFO consumer drains; measure()
 * accumulates now_ticks_raw() deltas so the 32-bit board counter wrap cannot
 * corrupt the multi-hundred-ms transfer. */
#define MQ_TP          "/rtos_bench_tp"
#define TP_MSG_BYTES   64
#define TP_MSGS        16384 /* 1 MiB / 64 B */
#define TP_QUEUE_DEPTH 8

static mqd_t          s_tp_mq = (mqd_t)-1;
static pthread_t      s_tp_peer;
static sem_t          s_tp_done;
static uint8_t        s_tp_tx[TP_MSG_BYTES];
static volatile uint32_t s_tp_target;
static volatile uint32_t s_tp_seen;
static volatile int   s_tp_quit;
static int            s_tp_active;

static void *tp_peer_entry(void *arg)
{
    uint8_t buf[TP_MSG_BYTES];
    (void)arg;
    for (;;) {
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_tp_mq, (char *)buf, sizeof(buf), &prio);
        if (n < 0) {
            continue;
        }
        if (s_tp_quit) {
            break;
        }
        if (++s_tp_seen >= s_tp_target) {
            (void)sem_post(&s_tp_done);
        }
    }
    return NULL;
}

static bench_status port_tp_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < TP_MSG_BYTES; i++) {
        s_tp_tx[i] = (uint8_t)(0x5Au ^ i);
    }
    s_tp_quit = 0;
    s_tp_seen = 0;
    s_tp_target = 0;
    mq_unlink(MQ_TP);
    struct mq_attr attr;
    attr.mq_maxmsg = TP_QUEUE_DEPTH;
    attr.mq_msgsize = TP_MSG_BYTES;
    attr.mq_flags = 0;
    s_tp_mq = mq_open(MQ_TP, MQ_FLAGS, MQ_MODE, &attr);
    if (s_tp_mq == (mqd_t)-1) {
        return BENCH_PORT;
    }
    if (sem_init(&s_tp_done, 0, 0) != 0) {
        return BENCH_PORT;
    }
    pthread_attr_t pattr;
    if (pthread_attr_init(&pattr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    sp.sched_priority = sched_get_priority_max(SCHED_FIFO);
    (void)pthread_attr_setschedpolicy(&pattr, SCHED_FIFO);
    (void)pthread_attr_setschedparam(&pattr, &sp);
    (void)pthread_attr_setinheritsched(&pattr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_tp_peer, &pattr, tp_peer_entry, NULL);
    (void)pthread_attr_destroy(&pattr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_tp_active = 1;
    return BENCH_OK;
}

static bench_status port_tp_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_tp_active || ticks == NULL) {
        return BENCH_PORT;
    }
    s_tp_seen = 0;
    s_tp_target = TP_MSGS;
    uint64_t total = 0;
    uint64_t previous_ticks = 0;
    if (now_ticks_raw(&previous_ticks) != BENCH_OK) {
        return BENCH_PORT;
    }
    for (uint32_t i = 0; i < TP_MSGS; i++) {
        if (mq_send(s_tp_mq, (const char *)s_tp_tx, TP_MSG_BYTES, 0) == -1) {
            return BENCH_PORT;
        }
        uint64_t now = 0;
        if (now_ticks_raw(&now) != BENCH_OK) {
            return BENCH_PORT;
        }
        total += (uint32_t)(now - previous_ticks);
        previous_ticks = now;
    }
    if (sem_wait(&s_tp_done) != 0) {
        return BENCH_PORT;
    }
    uint64_t now = 0;
    if (now_ticks_raw(&now) != BENCH_OK) {
        return BENCH_PORT;
    }
    total += (uint32_t)(now - previous_ticks);
    *ticks = total;
    return BENCH_OK;
}

static bench_status port_tp_destroy(void *ctx)
{
    (void)ctx;
    if (s_tp_active) {
        s_tp_quit = 1;
        uint8_t sentinel[TP_MSG_BYTES] = {0};
        (void)mq_send(s_tp_mq, (const char *)sentinel, TP_MSG_BYTES, 0);
        (void)pthread_join(s_tp_peer, NULL);
        s_tp_active = 0;
    }
    if (s_tp_mq != (mqd_t)-1) {
        mq_close(s_tp_mq);
        s_tp_mq = (mqd_t)-1;
    }
    (void)sem_destroy(&s_tp_done);
    mq_unlink(MQ_TP);
    return BENCH_OK;
}

/* Case 27: zero-copy IPC. The copied leg round-trips the 256-byte payload
 * through a capacity-1 message queue (POSIX mq copies the bytes); the zero-copy
 * leg sends only a pointer to a shared buffer through an 8/4-byte queue, so the
 * payload is never copied by the kernel. NuttX's POSIX mq has no zero-copy mode,
 * so a queue of references plus one shared buffer is the zero-copy mechanism.
 * measure() returns copied minus zero-copy now_ticks_raw(), clamped to 0 when a
 * sample transiently shows no benefit. The copied leg matches case 18 at 256 B. */
#define MQ_ZCOPY_CP_REQ "/rtos_bench_zcopy_cp_req"
#define MQ_ZCOPY_CP_REP "/rtos_bench_zcopy_cp_rep"
#define MQ_ZCOPY_ZC_REQ "/rtos_bench_zcopy_zc_req"
#define MQ_ZCOPY_ZC_REP "/rtos_bench_zcopy_zc_rep"
#define ZCOPY_BYTES 256

static mqd_t          s_zcopy_cp_req = (mqd_t)-1;
static mqd_t          s_zcopy_cp_rep = (mqd_t)-1;
static mqd_t          s_zcopy_zc_req = (mqd_t)-1;
static mqd_t          s_zcopy_zc_rep = (mqd_t)-1;
static pthread_t      s_zcopy_cp_peer;
static pthread_t      s_zcopy_zc_peer;
static volatile int   s_zcopy_quit;
static int            s_zcopy_active;
/* The STM32F4 NuttX image sits at its 112 KB SRAM limit, so case 27 reuses the
 * case-18 static payload buffers and keeps the shared buffer on the stack
 * rather than adding three more 256-byte arrays (which overflowed the region).
 * Cases run sequentially, so s_ipc_payload_tx/s_ipc_payload_rx are never live at once. */
#define s_zcopy_tx s_ipc_payload_tx
#define s_zcopy_rx s_ipc_payload_rx

static void *zcopy_copy_peer_entry(void *arg)
{
    (void)arg;
    uint8_t buf[ZCOPY_BYTES];
    for (;;) {
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_zcopy_cp_req, (char *)buf, sizeof(buf), &prio);
        if (n < 0) {
            continue;
        }
        if (s_zcopy_quit) {
            break;
        }
        (void)mq_send(s_zcopy_cp_rep, (const char *)buf, (size_t)n, 0);
    }
    return NULL;
}

static void *zcopy_ref_peer_entry(void *arg)
{
    (void)arg;
    void *ref = NULL;
    for (;;) {
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_zcopy_zc_req, (char *)&ref, sizeof(ref), &prio);
        if (n < 0) {
            continue;
        }
        if (s_zcopy_quit) {
            break;
        }
        (void)mq_send(s_zcopy_zc_rep, (const char *)&ref, sizeof(ref), 0);
    }
    return NULL;
}

static bench_status port_zcopy_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        s_zcopy_tx[i] = (uint8_t)(0x3Cu ^ i);
    }
    s_zcopy_quit = 0;
    mq_unlink(MQ_ZCOPY_CP_REQ);
    mq_unlink(MQ_ZCOPY_CP_REP);
    mq_unlink(MQ_ZCOPY_ZC_REQ);
    mq_unlink(MQ_ZCOPY_ZC_REP);
    struct mq_attr cp_attr;
    cp_attr.mq_maxmsg = 1;
    cp_attr.mq_msgsize = ZCOPY_BYTES;
    cp_attr.mq_flags = 0;
    struct mq_attr zc_attr;
    zc_attr.mq_maxmsg = 1;
    zc_attr.mq_msgsize = (long)sizeof(void *);
    zc_attr.mq_flags = 0;
    s_zcopy_cp_req = mq_open(MQ_ZCOPY_CP_REQ, MQ_FLAGS, MQ_MODE, &cp_attr);
    s_zcopy_cp_rep = mq_open(MQ_ZCOPY_CP_REP, MQ_FLAGS, MQ_MODE, &cp_attr);
    s_zcopy_zc_req = mq_open(MQ_ZCOPY_ZC_REQ, MQ_FLAGS, MQ_MODE, &zc_attr);
    s_zcopy_zc_rep = mq_open(MQ_ZCOPY_ZC_REP, MQ_FLAGS, MQ_MODE, &zc_attr);
    if (s_zcopy_cp_req == (mqd_t)-1 || s_zcopy_cp_rep == (mqd_t)-1
        || s_zcopy_zc_req == (mqd_t)-1 || s_zcopy_zc_rep == (mqd_t)-1) {
        return BENCH_PORT;
    }
    pthread_attr_t pattr;
    if (pthread_attr_init(&pattr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    sp.sched_priority = sched_get_priority_max(SCHED_FIFO);
    (void)pthread_attr_setschedpolicy(&pattr, SCHED_FIFO);
    (void)pthread_attr_setschedparam(&pattr, &sp);
    (void)pthread_attr_setinheritsched(&pattr, PTHREAD_EXPLICIT_SCHED);
    (void)pthread_attr_setstacksize(&pattr, 4096);
    int rc = pthread_create(&s_zcopy_cp_peer, &pattr, zcopy_copy_peer_entry, NULL);
    if (rc == 0) {
        rc = pthread_create(&s_zcopy_zc_peer, &pattr, zcopy_ref_peer_entry, NULL);
    }
    (void)pthread_attr_destroy(&pattr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_zcopy_active = 1;
    return BENCH_OK;
}

static bench_status port_zcopy_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_zcopy_active || ticks == NULL) {
        return BENCH_PORT;
    }
    uint8_t shared[ZCOPY_BYTES];
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        shared[i] = s_zcopy_tx[i];
    }
    /* Each leg is the minimum of several round trips. The NuttX scheduler/MQ
     * tail only adds time, so the minimum is the true cost; a single noisy leg
     * was what produced the clamped-zero differences. */
    const int k_iterations = 16;
    uint64_t copied = UINT64_MAX;
    for (int it = 0; it < k_iterations; it++) {
        uint64_t start = 0;
        if (now_ticks_raw(&start) != BENCH_OK) {
            return BENCH_PORT;
        }
        if (mq_send(s_zcopy_cp_req, (const char *)s_zcopy_tx, ZCOPY_BYTES, 0) == -1) {
            return BENCH_PORT;
        }
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_zcopy_cp_rep, (char *)s_zcopy_rx, ZCOPY_BYTES, &prio);
        uint64_t end = 0;
        if (now_ticks_raw(&end) != BENCH_OK) {
            return BENCH_PORT;
        }
        if (n != (ssize_t)ZCOPY_BYTES) {
            return BENCH_CORRUPT;
        }
        for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
            if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
                return BENCH_CORRUPT;
            }
        }
        uint64_t d = end - start;
        if (d < copied) {
            copied = d;
        }
    }

    void *ref = shared;
    uint64_t zcopy = UINT64_MAX;
    for (int it = 0; it < k_iterations; it++) {
        void *back = NULL;
        uint64_t start = 0;
        if (now_ticks_raw(&start) != BENCH_OK) {
            return BENCH_PORT;
        }
        if (mq_send(s_zcopy_zc_req, (const char *)&ref, sizeof(ref), 0) == -1) {
            return BENCH_PORT;
        }
        unsigned int prio = 0;
        ssize_t n = mq_receive(s_zcopy_zc_rep, (char *)&back, sizeof(back), &prio);
        uint64_t end = 0;
        if (now_ticks_raw(&end) != BENCH_OK) {
            return BENCH_PORT;
        }
        if (n != (ssize_t)sizeof(back)) {
            return BENCH_CORRUPT;
        }
        if (back != shared) {
            return BENCH_CORRUPT;
        }
        uint64_t d = end - start;
        if (d < zcopy) {
            zcopy = d;
        }
    }
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (shared[i] != s_zcopy_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    /* A non-positive difference is recorded as a no-benefit sentinel rather
     * than a silent 0, so the analysis can count no-benefit samples. */
    *ticks = (copied > zcopy) ? (copied - zcopy) : BENCH_ZCOPY_NO_BENEFIT_SENTINEL;
    return BENCH_OK;
}

static bench_status port_zcopy_destroy(void *ctx)
{
    (void)ctx;
    if (s_zcopy_active) {
        s_zcopy_quit = 1;
        void *sentinel = NULL;
        (void)mq_send(s_zcopy_cp_req, (const char *)s_zcopy_tx, 1, 0);
        (void)mq_send(s_zcopy_zc_req, (const char *)&sentinel, sizeof(sentinel), 0);
        (void)pthread_join(s_zcopy_cp_peer, NULL);
        (void)pthread_join(s_zcopy_zc_peer, NULL);
        s_zcopy_active = 0;
    }
    if (s_zcopy_cp_req != (mqd_t)-1) { mq_close(s_zcopy_cp_req); s_zcopy_cp_req = (mqd_t)-1; }
    if (s_zcopy_cp_rep != (mqd_t)-1) { mq_close(s_zcopy_cp_rep); s_zcopy_cp_rep = (mqd_t)-1; }
    if (s_zcopy_zc_req != (mqd_t)-1) { mq_close(s_zcopy_zc_req); s_zcopy_zc_req = (mqd_t)-1; }
    if (s_zcopy_zc_rep != (mqd_t)-1) { mq_close(s_zcopy_zc_rep); s_zcopy_zc_rep = (mqd_t)-1; }
    mq_unlink(MQ_ZCOPY_CP_REQ);
    mq_unlink(MQ_ZCOPY_CP_REP);
    mq_unlink(MQ_ZCOPY_ZC_REQ);
    mq_unlink(MQ_ZCOPY_ZC_REP);
    return BENCH_OK;
}

/* Case 24: heap fragmentation against the NuttX kernel allocator. Allocate
 * 256-byte blocks until ~20% of the starting free heap remains, free every
 * other block to leave non-adjacent holes, then read struct mallinfo (mxordblk
 * = largest free chunk, fordblks = total free). value =
 * round(1e6*(1 - mxordblk/fordblks)); freeing all blocks coalesces them. */
#define NX_FRAG_N 1024
#define NX_FRAG_BYTES 256u
/* The pointer array is allocated from the heap: the STM32F4 NuttX image is at
 * its SRAM limit, so a 4 KB static array would overflow the linker region. */
static void **s_nxfrag_blocks;

static bench_status port_frag_create(void *ctx)
{
    (void)ctx;
    s_nxfrag_blocks = malloc(NX_FRAG_N * sizeof(void *));
    if (s_nxfrag_blocks == NULL) {
        return BENCH_PORT;
    }
    for (uint32_t i = 0; i < NX_FRAG_N; ++i) {
        s_nxfrag_blocks[i] = NULL;
    }
    return BENCH_OK;
}

static bench_status port_frag_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL || s_nxfrag_blocks == NULL) {
        return BENCH_PORT;
    }
#ifdef __NuttX__
    struct mallinfo mi0 = mallinfo();
    int cap = mi0.fordblks;
    if (cap <= 0) {
        return BENCH_PORT;
    }
    uint32_t allocated = 0;
    while (allocated < NX_FRAG_N && mallinfo().fordblks > cap / 5) {
        void *b = malloc(NX_FRAG_BYTES);
        if (b == NULL) {
            break;
        }
        s_nxfrag_blocks[allocated++] = b;
    }
    if (allocated < 8u) {
        for (uint32_t i = 0; i < allocated; ++i) {
            free(s_nxfrag_blocks[i]);
            s_nxfrag_blocks[i] = NULL;
        }
        return BENCH_PORT;
    }
    for (uint32_t i = 0; i < allocated; i += 2) {
        free(s_nxfrag_blocks[i]);
        s_nxfrag_blocks[i] = NULL;
    }
    struct mallinfo mi = mallinfo();
    for (uint32_t i = 1; i < allocated; i += 2) {
        free(s_nxfrag_blocks[i]);
        s_nxfrag_blocks[i] = NULL;
    }
    if (mi.fordblks <= 0) {
        return BENCH_PORT;
    }
    long long frag = (mi.mxordblk < mi.fordblks)
                     ? (long long)(mi.fordblks - mi.mxordblk) : 0LL;
    *ticks = (uint64_t)((frag * 1000000LL) / (long long)mi.fordblks);
    return BENCH_OK;
#else
    return BENCH_UNSUPPORTED; /* host syntax check: mallinfo is NuttX-only */
#endif
}

static bench_status port_frag_destroy(void *ctx)
{
    (void)ctx;
    if (s_nxfrag_blocks == NULL) {
        return BENCH_OK;
    }
    for (uint32_t i = 0; i < NX_FRAG_N; ++i) {
        if (s_nxfrag_blocks[i] != NULL) {
            free(s_nxfrag_blocks[i]);
            s_nxfrag_blocks[i] = NULL;
        }
    }
    free(s_nxfrag_blocks);
    s_nxfrag_blocks = NULL;
    return BENCH_OK;
}

/* ------------------------------------------- priority-inheritance mutex */

/* H (the benchmark thread) blocks on a mutex held by L while M spins at a
 * priority between them. NuttX mutexes raise the holder to the highest waiter,
 * so L releases promptly; otherwise H waits out M's bounded spin. */

#define M_SPIN_NS 2000000LL /* ~2 ms */

static pthread_mutex_t s_mutex;
static sem_t           s_m_req;
static sem_t           s_m_held;
static sem_t           s_m_rel;
static sem_t           s_m_go;
static sem_t           s_m_done;
static pthread_t       s_m_task;
static pthread_t       s_l_task;
static volatile int    s_m_quit;
static volatile int    s_m_stop;
static int             s_m_made;

static void *mutex_low_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (sem_wait(&s_m_req) != 0) {
            continue;
        }
        if (s_m_quit) {
            break;
        }
        pthread_mutex_lock(&s_mutex);
        (void)sem_post(&s_m_held);
        (void)sem_wait(&s_m_rel);
        pthread_mutex_unlock(&s_mutex);
        if (s_m_quit) {
            break;
        }
    }
    return NULL;
}

static void *mutex_med_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (sem_wait(&s_m_go) != 0) {
            continue;
        }
        if (s_m_quit) {
            break;
        }
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        int64_t dl = (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec + M_SPIN_NS;
        for (;;) {
            if (s_m_stop) {
                break;
            }
            clock_gettime(CLOCK_MONOTONIC, &ts);
            int64_t now = (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
            if (now >= dl) {
                break;
            }
        }
        (void)sem_post(&s_m_done);
    }
    return NULL;
}

static bench_status port_mutex_create(void *ctx)
{
    (void)ctx;
    if (s_m_made) {
        return BENCH_OK;
    }
    if (sem_init(&s_m_req, 0, 0) != 0 || sem_init(&s_m_held, 0, 0) != 0 ||
        sem_init(&s_m_rel, 0, 0) != 0 || sem_init(&s_m_go, 0, 0) != 0 ||
        sem_init(&s_m_done, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (pthread_mutex_init(&s_mutex, NULL) != 0) {
        return BENCH_PORT;
    }
    s_m_quit = 0;

    struct sched_param cur;
    int policy = 0;
    if (pthread_getschedparam(pthread_self(), &policy, &cur) != 0) {
        return BENCH_PORT;
    }
    int maxp = sched_get_priority_max(SCHED_FIFO);
    if (cur.sched_priority + 2 > maxp) {
        return BENCH_PORT;
    }

    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    (void)pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);

    struct sched_param sp;
    sp.sched_priority = cur.sched_priority + 1;
    (void)pthread_attr_setschedparam(&attr, &sp);
    if (pthread_create(&s_m_task, &attr, mutex_med_entry, NULL) != 0) {
        (void)pthread_attr_destroy(&attr);
        return BENCH_PORT;
    }
    sp.sched_priority = cur.sched_priority + 2;
    (void)pthread_attr_setschedparam(&attr, &sp);
    if (pthread_create(&s_l_task, &attr, mutex_low_entry, NULL) != 0) {
        (void)pthread_attr_destroy(&attr);
        return BENCH_PORT;
    }
    (void)pthread_attr_destroy(&attr);
    s_m_made = 1;
    return BENCH_OK;
}

static bench_status port_mutex_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_m_made || ticks == NULL) {
        return BENCH_PORT;
    }
    s_m_stop = 0;
    if (sem_post(&s_m_req) != 0) {
        return BENCH_PORT;
    }
    if (sem_wait(&s_m_held) != 0) {
        return BENCH_PORT;
    }
    (void)sem_post(&s_m_go);
    uint64_t t0 = 0;
    if (now_ticks_raw(&t0) != BENCH_OK) {
        return BENCH_PORT;
    }
    (void)sem_post(&s_m_rel);
    pthread_mutex_lock(&s_mutex);
    uint64_t t1 = 0;
    if (now_ticks_raw(&t1) != BENCH_OK) {
        return BENCH_PORT;
    }
    s_m_stop = 1;
    (void)sem_wait(&s_m_done);
    pthread_mutex_unlock(&s_mutex);
    *ticks = t1 - t0;
    return BENCH_OK;
}

static bench_status port_mutex_destroy(void *ctx)
{
    (void)ctx;
    if (s_m_made) {
        s_m_quit = 1;
        (void)sem_post(&s_m_req);
        (void)sem_post(&s_m_rel);
        (void)sem_post(&s_m_go);
        (void)pthread_join(s_m_task, NULL);
        (void)pthread_join(s_l_task, NULL);
        s_m_made = 0;
    }
    (void)sem_destroy(&s_m_req);
    (void)sem_destroy(&s_m_held);
    (void)sem_destroy(&s_m_rel);
    (void)sem_destroy(&s_m_go);
    (void)sem_destroy(&s_m_done);
    (void)pthread_mutex_destroy(&s_mutex);
    return BENCH_OK;
}

/* ------------------------------------------------- timer callback (9) */

/* A one-shot watchdog (timer-interrupt context) is armed for 10 ms; the callback
 * timestamps itself and posts the semaphore. The nominal is expressed in the
 * port's raw tick unit so the deviation is comparable across boards. */

#define TIMER_PERIOD_SYS_TICKS 10 /* 10 ms at the 1 ms system tick */

/* The Rust boundary mirrors `struct wdog_s` as opaque 24-byte storage; a size
 * drift here must fail the build rather than corrupt memory. Guarded so the
 * host syntax-check (which uses an opaque stand-in above) skips it. */
#ifdef __NuttX__
_Static_assert(sizeof(struct wdog_s) == 24, "wdog_s size drift: update Rust boundary mirror");
#endif

static struct wdog_s     s_wdog;
static sem_t             s_timer_sem;
static volatile uint64_t s_timer_fire;

static void timer_wd(wdparm_t arg)
{
    (void)arg;
    uint64_t t = 0;
    if (now_ticks_raw(&t) == BENCH_OK) {
        s_timer_fire = t;
    }
    (void)sem_post(&s_timer_sem);
}

static bench_status port_timer_create(void *ctx)
{
    (void)ctx;
    if (sem_init(&s_timer_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_timer_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    while (sem_trywait(&s_timer_sem) == 0) {
        /* drain any stale signal */
    }
    uint64_t t0 = 0;
    if (now_ticks_raw(&t0) != BENCH_OK) {
        return BENCH_PORT;
    }
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
    uint64_t hz = (uint64_t)CONFIG_ESPRESSIF_CPU_FREQ_MHZ * 1000000ULL;
#elif defined(CONFIG_ARCH_CHIP_NRF52)
    uint64_t hz = NRF52_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
    uint64_t hz = RP23XX_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_STM32H7)
    uint64_t hz = STM32H7_SYSCLK_HZ;
#else
    uint64_t hz = STM32F4_SYSCLK_HZ;
#endif
    uint64_t period = hz / 100ULL; /* 10 ms in raw ticks */
    if (wd_start(&s_wdog, TIMER_PERIOD_SYS_TICKS, timer_wd, 0) != 0) {
        return BENCH_PORT;
    }
    if (sem_wait(&s_timer_sem) != 0) {
        return BENCH_PORT;
    }
    uint64_t t1 = s_timer_fire;
    uint64_t interval = (t1 >= t0) ? (t1 - t0) : 0;
    *ticks = (interval >= period) ? (interval - period) : (period - interval);
    return BENCH_OK;
}

static bench_status port_timer_destroy(void *ctx)
{
    (void)ctx;
    (void)wd_cancel(&s_wdog);
    (void)sem_destroy(&s_timer_sem);
    return BENCH_OK;
}

extern bench_status nuttx_bench_isr_create(void *ctx);
extern bench_status nuttx_bench_isr_measure(void *ctx, uint64_t *ticks);
extern bench_status nuttx_bench_isr_destroy(void *ctx);
extern bench_status nuttx_bench_trigger_isr_create(void *ctx);
extern bench_status nuttx_bench_trigger_isr_measure(void *ctx, uint64_t *ticks);
extern bench_status nuttx_bench_trigger_isr_destroy(void *ctx);
#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_RP23XX)
extern bench_status nuttx_bench_pjit_create(void *ctx);
extern bench_status nuttx_bench_pjit_measure(void *ctx, uint64_t *ticks);
extern bench_status nuttx_bench_pjit_destroy(void *ctx);
extern bench_status nuttx_bench_maxdis_create(void *ctx);
extern bench_status nuttx_bench_nested_create(void *ctx);
extern bench_status nuttx_bench_nested_measure(void *ctx, uint64_t *ticks);
extern bench_status nuttx_bench_nested_destroy(void *ctx);
extern bench_status nuttx_bench_maxdis_measure(void *ctx, uint64_t *ticks);
extern bench_status nuttx_bench_maxdis_destroy(void *ctx);
#endif

/* ------------------------------- uncontended sync / alloc / delay (12-15) */

/* The Rust boundary mirrors these two caller-allocated types as opaque
 * storage; a config change that alters either layout must fail the build here
 * rather than corrupt memory through a size drift in the Rust mirror. Guarded
 * so the host syntax-check (host sem_t/pthread_mutex_t differ) skips them. */
#ifdef __NuttX__
_Static_assert(sizeof(sem_t) == 16, "sem_t size drift: update Rust boundary mirror");
_Static_assert(sizeof(pthread_mutex_t) == 24, "pthread_mutex_t size drift: update Rust boundary mirror");
#endif

/* Case 12: uncontended binary-semaphore give -> take API round trip. */
static sem_t s_api_sem;
static bench_status port_sem_create(void *ctx)
{
    (void)ctx;
    if (sem_init(&s_api_sem, 0, 0) != 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}
static bench_status port_sem_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (sem_post(&s_api_sem) != 0) {
        return BENCH_PORT;
    }
    if (sem_wait(&s_api_sem) != 0) {
        return BENCH_CORRUPT;
    }
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    if (end < start) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}
static bench_status port_sem_destroy(void *ctx)
{
    (void)ctx;
    (void)sem_destroy(&s_api_sem);
    return BENCH_OK;
}

/* Case 13: uncontended mutex lock -> unlock API round trip. */
static pthread_mutex_t s_api_mutex = PTHREAD_MUTEX_INITIALIZER;
static bench_status port_mutex_u_create(void *ctx)
{
    (void)ctx;
    if (pthread_mutex_init(&s_api_mutex, NULL) != 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}
static bench_status port_mutex_u_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (pthread_mutex_lock(&s_api_mutex) != 0) {
        return BENCH_PORT;
    }
    if (pthread_mutex_unlock(&s_api_mutex) != 0) {
        return BENCH_PORT;
    }
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    if (end < start) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}
static bench_status port_mutex_u_destroy(void *ctx)
{
    (void)ctx;
    (void)pthread_mutex_destroy(&s_api_mutex);
    return BENCH_OK;
}

/* Case 14: allocator allocate -> free round trip (64-byte payload), NuttX
 * kernel heap via malloc/free. */
static bench_status port_alloc_create(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}
static bench_status port_alloc_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    void *p = malloc(64);
    if (p != NULL) {
        free(p);
    }
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    if (p == NULL || end < start) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}
static bench_status port_alloc_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

/* Case 15: one-shot delay accuracy. Requests 1 ms (usleep) and reports the
 * absolute deviation of the actual elapsed ticks from the nominal period. */
static bench_status port_sleep_create(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}
static bench_status port_sleep_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    /* Plain POSIX relative sleep (usleep is not declared under the strict
     * _POSIX_C_SOURCE=200809L that the host syntax-check uses). */
    const struct timespec req = { .tv_sec = 0, .tv_nsec = 1000000L };
    (void)nanosleep(&req, NULL);
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    const uint64_t nominal = s_ticks_per_second / 1000ULL;
    const uint64_t actual = end - start;
    *ticks = (actual >= nominal) ? (actual - nominal) : (nominal - actual);
    return BENCH_OK;
}
static bench_status port_sleep_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

/* ------------------------- scheduler overhead / notification / priority */

/* Case 16: one sched_yield() with no equal/higher-priority ready peer. */
static bench_status port_sched_create(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}
static bench_status port_sched_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    (void)sched_yield();
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    if (end < start) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}
static bench_status port_sched_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

/* Case 17: POSIX-semaphore notification. The peer runs one priority below the
 * benchmark, so it cannot signal before the benchmark blocks. */
static sem_t           s_notify_go;
static sem_t           s_notify_done;
static pthread_t       s_notify_peer;
static volatile uint64_t s_notify_signal;
static volatile int    s_notify_quit;
static int             s_notify_active;

static void *notify_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (sem_wait(&s_notify_go) != 0) {
            continue;
        }
        if (s_notify_quit) {
            break;
        }
        uint64_t t = 0;
        (void)now_ticks_raw(&t);
        s_notify_signal = t;
        (void)sem_post(&s_notify_done);
    }
    return NULL;
}

static bench_status port_notify_create(void *ctx)
{
    (void)ctx;
    s_notify_quit = 0;
    s_notify_active = 0;
    if (sem_init(&s_notify_go, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (sem_init(&s_notify_done, 0, 0) != 0) {
        return BENCH_PORT;
    }
    pthread_attr_t attr;
    if (pthread_attr_init(&attr) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    int policy = SCHED_FIFO;
    if (pthread_getschedparam(pthread_self(), &policy, &sp) != 0) {
        (void)pthread_attr_destroy(&attr);
        return BENCH_PORT;
    }
    int prio = sp.sched_priority - 1;
    const int minp = sched_get_priority_min(SCHED_FIFO);
    if (prio < minp) {
        prio = minp;
    }
    sp.sched_priority = prio;
    (void)pthread_attr_setschedpolicy(&attr, policy);
    (void)pthread_attr_setschedparam(&attr, &sp);
    (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    int rc = pthread_create(&s_notify_peer, &attr, notify_peer_entry, NULL);
    (void)pthread_attr_destroy(&attr);
    if (rc != 0) {
        return BENCH_PORT;
    }
    s_notify_active = 1;
    return BENCH_OK;
}

static bench_status port_notify_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_notify_active || ticks == NULL) {
        return BENCH_PORT;
    }
    while (sem_trywait(&s_notify_done) == 0) {
        /* drain stale tokens */
    }
    if (sem_post(&s_notify_go) != 0) {
        return BENCH_PORT;
    }
    if (sem_wait(&s_notify_done) != 0) {
        return BENCH_PORT;
    }
    uint64_t end;
    bench_status status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    const uint64_t signal = s_notify_signal;
    if (end < signal) {
        return BENCH_CORRUPT;
    }
    *ticks = end - signal;
    return BENCH_OK;
}

static bench_status port_notify_destroy(void *ctx)
{
    (void)ctx;
    if (s_notify_active) {
        s_notify_quit = 1;
        (void)sem_post(&s_notify_go);
        (void)pthread_join(s_notify_peer, NULL);
        s_notify_active = 0;
    }
    (void)sem_destroy(&s_notify_go);
    (void)sem_destroy(&s_notify_done);
    return BENCH_OK;
}

/* Case 26: priority scheduling conformance with four SCHED_FIFO peers. Each
 * peer is higher priority than the benchmark, so each sem_post wakes the
 * highest-priority blocked peer, which records its id and blocks for ack. */
#define NUTTX_PRIO_N 4
static sem_t            s_prio_release;
static sem_t            s_prio_all;
static sem_t            s_prio_ack;
static pthread_t        s_prio_task[NUTTX_PRIO_N];
static volatile uint32_t s_prio_n;
static volatile uint8_t  s_prio_trace[NUTTX_PRIO_N];
static volatile int     s_prio_quit;
static int              s_prio_active;

static void *prio_peer_entry(void *arg)
{
    const uint8_t id = (uint8_t)(uintptr_t)arg;
    for (;;) {
        if (sem_wait(&s_prio_release) != 0) {
            continue;
        }
        if (s_prio_quit) {
            break;
        }
        if (s_prio_n < NUTTX_PRIO_N) {
            s_prio_trace[s_prio_n] = id;
            s_prio_n++;
        }
        (void)sem_post(&s_prio_all);
        (void)sem_wait(&s_prio_ack);
    }
    return NULL;
}

static bench_status port_prio_create(void *ctx)
{
    (void)ctx;
    s_prio_quit = 0;
    s_prio_active = 0;
    if (sem_init(&s_prio_release, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (sem_init(&s_prio_all, 0, 0) != 0) {
        return BENCH_PORT;
    }
    if (sem_init(&s_prio_ack, 0, 0) != 0) {
        return BENCH_PORT;
    }
    struct sched_param sp;
    int policy = SCHED_FIFO;
    if (pthread_getschedparam(pthread_self(), &policy, &sp) != 0) {
        return BENCH_PORT;
    }
    const int maxp = sched_get_priority_max(policy);
    for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
#ifdef PRIO_INVERT
        const int prio = sp.sched_priority + 1 + (NUTTX_PRIO_N - 1 - i);
#else
        const int prio = sp.sched_priority + 1 + i;
#endif
        if (prio > maxp) {
            return BENCH_PORT;
        }
        pthread_attr_t attr;
        if (pthread_attr_init(&attr) != 0) {
            return BENCH_PORT;
        }
        struct sched_param csp = sp;
        csp.sched_priority = prio;
        (void)pthread_attr_setschedpolicy(&attr, policy);
        (void)pthread_attr_setschedparam(&attr, &csp);
        (void)pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
        int rc = pthread_create(&s_prio_task[i], &attr, prio_peer_entry,
                                (void *)(uintptr_t)i);
        (void)pthread_attr_destroy(&attr);
        if (rc != 0) {
            return BENCH_PORT;
        }
    }
    s_prio_active = 1;
    return BENCH_OK;
}

static bench_status port_prio_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_prio_active || ticks == NULL) {
        return BENCH_PORT;
    }
    s_prio_n = 0;
    for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
        (void)sem_post(&s_prio_release);
    }
    for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
        if (sem_wait(&s_prio_all) != 0) {
            return BENCH_PORT;
        }
    }
    int ok = (s_prio_n == NUTTX_PRIO_N);
    for (uint32_t i = 0; ok && i < NUTTX_PRIO_N; i++) {
        if (s_prio_trace[i] != (uint8_t)(NUTTX_PRIO_N - 1 - i)) {
            ok = 0;
        }
    }
    for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
        (void)sem_post(&s_prio_ack);
    }
    *ticks = (ok != 0) ? 1u : 0u;
    return BENCH_OK;
}

static bench_status port_prio_destroy(void *ctx)
{
    (void)ctx;
    if (s_prio_active) {
        s_prio_quit = 1;
        for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
            (void)sem_post(&s_prio_release);
        }
        for (uint8_t i = 0; i < NUTTX_PRIO_N; i++) {
            (void)pthread_join(s_prio_task[i], NULL);
        }
        s_prio_active = 0;
    }
    (void)sem_destroy(&s_prio_release);
    (void)sem_destroy(&s_prio_all);
    (void)sem_destroy(&s_prio_ack);
    return BENCH_OK;
}

bench_status nuttx_bench_port_init(bench_port *port)
{
    if (port == NULL) {
        return BENCH_INVALID;
    }
    /* Zero every field first: unset optional hooks (e.g. pjit_set_load) must
     * read as NULL, not as uninitialized stack garbage, or the runner treats
     * them as implemented and calls a garbage pointer. */
    *port = (bench_port){0};
    port->ctx = NULL;
    port->now_ticks = port_now_ticks;
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
    c6_perf_enable();
    port->ticks_per_second = (uint64_t)CONFIG_ESPRESSIF_CPU_FREQ_MHZ * 1000000ULL;
#elif defined(CONFIG_ARCH_CHIP_NRF52)
    nrf52_dwt_init();
    port->ticks_per_second = NRF52_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
    rp23xx_dwt_init();
    port->ticks_per_second = RP23XX_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_STM32H7)
    port->ticks_per_second = STM32H7_SYSCLK_HZ;
#else
    port->ticks_per_second = STM32F4_SYSCLK_HZ;
#endif
    s_ticks_per_second = port->ticks_per_second;
    port->queue_create = port_queue_create;
    port->queue_send = port_queue_send;
    port->queue_receive = port_queue_receive;
    port->queue_destroy = port_queue_destroy;
    port->handoff_create = port_handoff_create;
    port->handoff_measure = port_handoff_measure;
    port->handoff_destroy = port_handoff_destroy;
    port->jitter_create = port_jitter_create;
    port->jitter_measure = port_jitter_measure;
    port->jitter_destroy = port_jitter_destroy;
    port->isr_create = nuttx_bench_isr_create;
    port->isr_measure = nuttx_bench_isr_measure;
    port->isr_destroy = nuttx_bench_isr_destroy;
    port->trigger_isr_create = nuttx_bench_trigger_isr_create;
    port->trigger_isr_measure = nuttx_bench_trigger_isr_measure;
    port->trigger_isr_destroy = nuttx_bench_trigger_isr_destroy;
#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_RP23XX)
    port->pjit_create = nuttx_bench_pjit_create;
    port->pjit_measure = nuttx_bench_pjit_measure;
    port->pjit_destroy = nuttx_bench_pjit_destroy;
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
    port->nested_create = nuttx_bench_nested_create;
    port->nested_measure = nuttx_bench_nested_measure;
    port->nested_destroy = nuttx_bench_nested_destroy;
#endif
    port->maxdis_create = nuttx_bench_maxdis_create;
    port->maxdis_measure = nuttx_bench_maxdis_measure;
    port->maxdis_destroy = nuttx_bench_maxdis_destroy;
#endif
    port->yield_create = port_yield_create;
    port->yield_now = port_yield_now;
    port->peers_set = port_peers_set;
    port->yield_destroy = port_yield_destroy;
    port->preempt_create = port_preempt_create;
    port->preempt_measure = port_preempt_measure;
    port->preempt_destroy = port_preempt_destroy;
    port->ipc_create = port_ipc_create;
    port->ipc_measure = port_ipc_measure;
    port->ipc_destroy = port_ipc_destroy;
    port->ipc_payload_create = port_ipc_payload_create;
    port->ipc_payload_measure = port_ipc_payload_measure;
    port->ipc_payload_destroy = port_ipc_payload_destroy;
    port->tp_create = port_tp_create;
    port->tp_measure = port_tp_measure;
    port->tp_destroy = port_tp_destroy;
    port->zcopy_create = port_zcopy_create;
    port->zcopy_measure = port_zcopy_measure;
    port->zcopy_destroy = port_zcopy_destroy;
    port->frag_create = port_frag_create;
    port->frag_measure = port_frag_measure;
    port->frag_destroy = port_frag_destroy;
    port->mutex_create = port_mutex_create;
    port->mutex_measure = port_mutex_measure;
    port->mutex_destroy = port_mutex_destroy;
    port->timer_create = port_timer_create;
    port->timer_measure = port_timer_measure;
    port->timer_destroy = port_timer_destroy;
    port->sem_create = port_sem_create;
    port->sem_measure = port_sem_measure;
    port->sem_destroy = port_sem_destroy;
    port->mutex_u_create = port_mutex_u_create;
    port->mutex_u_measure = port_mutex_u_measure;
    port->mutex_u_destroy = port_mutex_u_destroy;
    port->alloc_create = port_alloc_create;
    port->alloc_measure = port_alloc_measure;
    port->alloc_destroy = port_alloc_destroy;
    port->sleep_create = port_sleep_create;
    port->sleep_measure = port_sleep_measure;
    port->sleep_destroy = port_sleep_destroy;
    port->sched_create = port_sched_create;
    port->sched_measure = port_sched_measure;
    port->sched_destroy = port_sched_destroy;
    port->notify_create = port_notify_create;
    port->notify_measure = port_notify_measure;
    port->notify_destroy = port_notify_destroy;
    port->prio_create = port_prio_create;
    port->prio_measure = port_prio_measure;
    port->prio_destroy = port_prio_destroy;
    return BENCH_OK;
}

/* --------------------------------------------------- Rust application core */
#ifdef BENCH_LANGUAGE_RUST
/* Thin shims for firmware/nuttx/rust. The C path reaches these operations
 * through the bench_port vtable; the shims let the Rust core call the same
 * POSIX/NuttX primitives without depending on the vtable. */
/* The Rust path does not call nuttx_bench_port_init, so the board cycle counter
 * must be enabled from the Rust-facing shims. Idempotent; cheap and done outside
 * the timed region. On the STM32F4 the NuttX start code enables DWT. */
static void rust_tick_enable(void)
{
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
    c6_perf_enable();
#elif defined(CONFIG_ARCH_CHIP_NRF52)
    nrf52_dwt_init();
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
    rp23xx_dwt_init();
#endif
}

uint64_t bench_rust_now_ticks(void)
{
    rust_tick_enable();
    uint64_t ticks = 0;
    (void)now_ticks_raw(&ticks);
    return ticks;
}

uint64_t bench_rust_ticks_per_second(void)
{
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
    return (uint64_t)CONFIG_ESPRESSIF_CPU_FREQ_MHZ * 1000000ULL;
#elif defined(CONFIG_ARCH_CHIP_NRF52)
    return NRF52_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
    return RP23XX_DWT_HZ;
#elif defined(CONFIG_ARCH_CHIP_STM32H7)
    return (uint64_t)STM32H7_SYSCLK_HZ;
#else
    return (uint64_t)STM32F4_SYSCLK_HZ;
#endif
}

int bench_rust_queue_create(void)
{
    return (int)port_queue_create(NULL);
}

int bench_rust_queue_send(uint32_t value)
{
    return (int)port_queue_send(NULL, value);
}

int bench_rust_queue_receive(uint32_t *value)
{
    return (int)port_queue_receive(NULL, value);
}

int bench_rust_queue_destroy(void)
{
    return (int)port_queue_destroy(NULL);
}

int bench_rust_handoff_create(void)
{
    return (int)port_handoff_create(NULL);
}

int bench_rust_handoff_measure(uint64_t *ticks)
{
    return (int)port_handoff_measure(NULL, ticks);
}

int bench_rust_handoff_destroy(void)
{
    return (int)port_handoff_destroy(NULL);
}

int bench_rust_handoff_post(void)
{
    return (sem_post(&s_handoff_signal) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_handoff_wait(void)
{
    return (sem_wait(&s_handoff_done) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_handoff_wake(void)
{
    return s_handoff_wake;
}

int bench_rust_jitter_create(void)
{
    return (int)port_jitter_create(NULL);
}

int bench_rust_jitter_measure(uint64_t *ticks)
{
    return (int)port_jitter_measure(NULL, ticks);
}

int bench_rust_jitter_destroy(void)
{
    return (int)port_jitter_destroy(NULL);
}

/* Thin clock shims: the Rust adapter keeps the 1 ms grid in u64 nanoseconds,
 * so only the CLOCK_MONOTONIC timespec handling stays in C. */
int bench_rust_monotonic_ns(uint64_t *ns)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
        return (int)BENCH_PORT;
    }
    *ns = (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
    return 0;
}

int bench_rust_abs_sleep_ns(uint64_t ns)
{
    const struct timespec req = {
        .tv_sec = (time_t)(ns / 1000000000ULL),
        .tv_nsec = (long)(ns % 1000000000ULL),
    };
    return clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &req, NULL);
}

int bench_rust_isr_create(void)
{
    return (int)nuttx_bench_isr_create(NULL);
}

int bench_rust_isr_measure(uint64_t *ticks)
{
    return (int)nuttx_bench_isr_measure(NULL, ticks);
}

int bench_rust_isr_destroy(void)
{
    return (int)nuttx_bench_isr_destroy(NULL);
}

int bench_rust_trigger_isr_create(void)
{
    return (int)nuttx_bench_trigger_isr_create(NULL);
}

int bench_rust_trigger_isr_measure(uint64_t *ticks)
{
    return (int)nuttx_bench_trigger_isr_measure(NULL, ticks);
}

int bench_rust_trigger_isr_destroy(void)
{
    return (int)nuttx_bench_trigger_isr_destroy(NULL);
}

int bench_rust_pjit_create(void)
{
#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_RP23XX)
    return (int)nuttx_bench_pjit_create(NULL);
#else
    return (int)BENCH_UNSUPPORTED; /* case 11 advertised on nRF52840, C6, STM32F4/H7 */
#endif
}

int bench_rust_pjit_destroy(void)
{
#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_RP23XX)
    return (int)nuttx_bench_pjit_destroy(NULL);
#else
    return BENCH_OK;
#endif
}

int bench_rust_maxdis_create(void)
{
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6)
    return (int)nuttx_bench_maxdis_create(NULL);
#else
    return (int)BENCH_UNSUPPORTED; /* case 25: STM32F4/H7/nRF52840/Pico2/C6 */
#endif
}

int bench_rust_maxdis_destroy(void)
{
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6)
    return (int)nuttx_bench_maxdis_destroy(NULL);
#else
    return BENCH_OK;
#endif
}

int bench_rust_nested_create(void)
{
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6)
    return (int)nuttx_bench_nested_create(NULL);
#else
    return (int)BENCH_UNSUPPORTED; /* case 20: STM32F4/H7/nRF52840/Pico2/C6 */
#endif
}

int bench_rust_nested_destroy(void)
{
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_STM32H7) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6)
    return (int)nuttx_bench_nested_destroy(NULL);
#else
    return BENCH_OK;
#endif
}

int bench_rust_yield_create(void)
{
    return (int)port_yield_create(NULL);
}

int bench_rust_yield_now(void)
{
    return (int)port_yield_now(NULL);
}

int bench_rust_yield_started(void)
{
    return s_yield_peer_started;
}

uint32_t bench_rust_yield_runs(void)
{
    return s_yield_peer_runs;
}

int bench_rust_peers_set(uint32_t count) { return (int)port_peers_set(NULL, count); }

int bench_rust_yield_destroy(void)
{
    return (int)port_yield_destroy(NULL);
}

int bench_rust_preempt_create(void)
{
    return (int)port_preempt_create(NULL);
}

int bench_rust_preempt_measure(uint64_t *ticks)
{
    return (int)port_preempt_measure(NULL, ticks);
}

int bench_rust_preempt_destroy(void)
{
    return (int)port_preempt_destroy(NULL);
}

int bench_rust_preempt_ready(void)
{
    return s_preempt_ready;
}

uint32_t bench_rust_preempt_seq(void)
{
    return s_preempt_seq;
}

int bench_rust_preempt_post(void)
{
    return (sem_post(&s_preempt_sem) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_preempt_wake(void)
{
    return s_preempt_wake;
}

int bench_rust_ipc_create(void)
{
    /* The IPC paths never call now_ticks(), so enable the cycle counter here. */
    rust_tick_enable();
    return (int)port_ipc_create(NULL);
}

int bench_rust_ipc_measure(uint32_t value, uint64_t *ticks)
{
    return (int)port_ipc_measure(NULL, value, ticks);
}

int bench_rust_ipc_destroy(void)
{
    return (int)port_ipc_destroy(NULL);
}

/* Per-operation shims for the native Rust timed loop (the echo peer stays C). */
int bench_rust_ipc_send(uint32_t value)
{
    if (!s_ipc_active) {
        return (int)BENCH_PORT;
    }
    uint32_t v = value;
    return (mq_send(s_ipc_req, (const char *)&v, sizeof(v), 0) == -1)
               ? (int)BENCH_PORT
               : 0;
}

int bench_rust_ipc_recv(uint32_t value, uint32_t *received_value)
{
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_ipc_rep, (char *)received_value, sizeof(*received_value), &prio);
    if (n != (ssize_t)sizeof(*received_value)) {
        return (int)BENCH_PORT;
    }
    return (*received_value == value) ? 0 : (int)BENCH_CORRUPT;
}

int bench_rust_ipc_payload_create(void)
{
    /* The payload-sweep path never calls now_ticks(); enable the counter here. */
    rust_tick_enable();
    return (int)port_ipc_payload_create(NULL);
}

int bench_rust_ipc_payload_measure(uint32_t payload_bytes, uint64_t *ticks)
{
    return (int)port_ipc_payload_measure(NULL, payload_bytes, ticks);
}

int bench_rust_ipc_payload_destroy(void)
{
    return (int)port_ipc_payload_destroy(NULL);
}

int bench_rust_ipc_payload_send(uint32_t payload_bytes)
{
    if (!s_ipc_payload_active || payload_bytes == 0 || payload_bytes > IPC_PAYLOAD_MAX) {
        return (int)BENCH_PORT;
    }
    return (mq_send(s_ipc_payload_req, (const char *)s_ipc_payload_tx, payload_bytes, 0) == -1)
               ? (int)BENCH_PORT
               : 0;
}

int bench_rust_ipc_payload_recv(uint32_t payload_bytes)
{
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_ipc_payload_rep, (char *)s_ipc_payload_rx, IPC_PAYLOAD_MAX, &prio);
    if (n != (ssize_t)payload_bytes) {
        return (int)BENCH_CORRUPT;
    }
    for (uint32_t i = 0; i < payload_bytes; i++) {
        if (s_ipc_payload_rx[i] != s_ipc_payload_tx[i]) {
            return (int)BENCH_CORRUPT;
        }
    }
    return 0;
}

int bench_rust_tp_create(void)
{
    /* The throughput path never calls now_ticks(); enable the board counter. */
    rust_tick_enable();
    return (int)port_tp_create(NULL);
}

int bench_rust_tp_measure(uint64_t *ticks)
{
    return (int)port_tp_measure(NULL, ticks);
}

int bench_rust_tp_destroy(void)
{
    return (int)port_tp_destroy(NULL);
}

unsigned int bench_rust_tp_count(void)
{
    return TP_MSGS;
}

int bench_rust_tp_start(void)
{
    if (!s_tp_active) {
        return (int)BENCH_PORT;
    }
    s_tp_seen = 0;
    s_tp_target = TP_MSGS;
    return 0;
}

int bench_rust_tp_send(void)
{
    return (mq_send(s_tp_mq, (const char *)s_tp_tx, TP_MSG_BYTES, 0) == -1)
               ? (int)BENCH_PORT
               : 0;
}

int bench_rust_tp_done(void)
{
    return (sem_wait(&s_tp_done) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_zcopy_create(void)
{
    /* The zero-copy path never calls now_ticks(); enable the board counter. */
    rust_tick_enable();
    return (int)port_zcopy_create(NULL);
}

int bench_rust_zcopy_measure(uint64_t *ticks)
{
    return (int)port_zcopy_measure(NULL, ticks);
}

int bench_rust_zcopy_destroy(void)
{
    return (int)port_zcopy_destroy(NULL);
}

/* The zero-copy case needs a shared buffer across the two legs, so the C port
 * owns it and the Rust adapter runs the per-leg minimum loops. */
static uint8_t s_zcopy_shared[ZCOPY_BYTES];

int bench_rust_zcopy_reset(void)
{
    if (!s_zcopy_active) {
        return (int)BENCH_PORT;
    }
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        s_zcopy_shared[i] = s_zcopy_tx[i];
    }
    return 0;
}

int bench_rust_zcopy_copied(uint64_t *ticks)
{
    if (!s_zcopy_active || ticks == NULL) {
        return (int)BENCH_PORT;
    }
    uint64_t start = 0, end = 0;
    if (now_ticks_raw(&start) != BENCH_OK) {
        return (int)BENCH_PORT;
    }
    if (mq_send(s_zcopy_cp_req, (const char *)s_zcopy_tx, ZCOPY_BYTES, 0) == -1) {
        return (int)BENCH_PORT;
    }
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_zcopy_cp_rep, (char *)s_zcopy_rx, ZCOPY_BYTES, &prio);
    if (now_ticks_raw(&end) != BENCH_OK) {
        return (int)BENCH_PORT;
    }
    if (n != (ssize_t)ZCOPY_BYTES) {
        return (int)BENCH_CORRUPT;
    }
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
            return (int)BENCH_CORRUPT;
        }
    }
    *ticks = end - start;
    return 0;
}

int bench_rust_zcopy_ref(uint64_t *ticks)
{
    if (!s_zcopy_active || ticks == NULL) {
        return (int)BENCH_PORT;
    }
    void *ref = s_zcopy_shared;
    void *back = NULL;
    uint64_t start = 0, end = 0;
    if (now_ticks_raw(&start) != BENCH_OK) {
        return (int)BENCH_PORT;
    }
    if (mq_send(s_zcopy_zc_req, (const char *)&ref, sizeof(ref), 0) == -1) {
        return (int)BENCH_PORT;
    }
    unsigned int prio = 0;
    ssize_t n = mq_receive(s_zcopy_zc_rep, (char *)&back, sizeof(back), &prio);
    if (now_ticks_raw(&end) != BENCH_OK) {
        return (int)BENCH_PORT;
    }
    if (n != (ssize_t)sizeof(back) || back != ref) {
        return (int)BENCH_CORRUPT;
    }
    *ticks = end - start;
    return 0;
}

int bench_rust_zcopy_verify(void)
{
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (s_zcopy_shared[i] != s_zcopy_tx[i]) {
            return (int)BENCH_CORRUPT;
        }
    }
    return 0;
}

int bench_rust_frag_create(void)
{
    return (int)port_frag_create(NULL);
}

int bench_rust_frag_measure(uint64_t *ticks)
{
    return (int)port_frag_measure(NULL, ticks);
}

int bench_rust_frag_destroy(void)
{
    return (int)port_frag_destroy(NULL);
}

#ifdef __NuttX__
/* Fine-grained shims so the Rust adapter owns the fragmentation loop. */
int bench_rust_fordblks(void)
{
    return (int)mallinfo().fordblks;
}

int bench_rust_mxordblk(void)
{
    return (int)mallinfo().mxordblk;
}

unsigned int bench_rust_frag_n(void)
{
    return NX_FRAG_N;
}

int bench_rust_frag_alloc(unsigned int idx)
{
    void *b = malloc(NX_FRAG_BYTES);
    if (b == NULL) {
        return 1;
    }
    s_nxfrag_blocks[idx] = b;
    return 0;
}

void bench_rust_frag_free(unsigned int idx)
{
    if (s_nxfrag_blocks[idx] != NULL) {
        free(s_nxfrag_blocks[idx]);
        s_nxfrag_blocks[idx] = NULL;
    }
}
#endif

int bench_rust_mutex_create(void)
{
    return (int)port_mutex_create(NULL);
}

int bench_rust_mutex_measure(uint64_t *ticks)
{
    return (int)port_mutex_measure(NULL, ticks);
}

int bench_rust_mutex_destroy(void)
{
    return (int)port_mutex_destroy(NULL);
}

int bench_rust_mutex_ready(void)
{
    return s_m_made;
}

int bench_rust_mutex_reset(void)
{
    s_m_stop = 0;
    return 0;
}

int bench_rust_mutex_req(void)
{
    return (sem_post(&s_m_req) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_held(void)
{
    return (sem_wait(&s_m_held) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_go(void)
{
    return (sem_post(&s_m_go) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_rel(void)
{
    return (sem_post(&s_m_rel) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_lock(void)
{
    return (pthread_mutex_lock(&s_mutex) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_unlock(void)
{
    return (pthread_mutex_unlock(&s_mutex) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_set_stop(void)
{
    s_m_stop = 1;
    return 0;
}

int bench_rust_mutex_done(void)
{
    return (sem_wait(&s_m_done) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_timer_create(void)
{
    return (int)port_timer_create(NULL);
}

int bench_rust_timer_measure(uint64_t *ticks)
{
    return (int)port_timer_measure(NULL, ticks);
}

int bench_rust_timer_destroy(void)
{
    return (int)port_timer_destroy(NULL);
}

int bench_rust_timer_drain(void)
{
    while (sem_trywait(&s_timer_sem) == 0) {
        /* drain any stale signal */
    }
    return 0;
}

int bench_rust_timer_arm(void)
{
    return (wd_start(&s_wdog, TIMER_PERIOD_SYS_TICKS, timer_wd, 0) != 0)
               ? (int)BENCH_PORT
               : 0;
}

int bench_rust_timer_wait(void)
{
    return (sem_wait(&s_timer_sem) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_timer_fire(void)
{
    return s_timer_fire;
}

int bench_rust_sem_create(void)
{
    /* The Rust runner does not call now_ticks() for cases 12-15, so the
     * per-board cycle counter is not enabled by a prior measurement here
     * (nRF/C6/RP2350 DWT or C6 perf counter). Initialise it explicitly. */
    (void)bench_rust_now_ticks();
    return (int)port_sem_create(NULL);
}

int bench_rust_sem_measure(uint64_t *ticks)
{
    return (int)port_sem_measure(NULL, ticks);
}

int bench_rust_sem_destroy(void)
{
    return (int)port_sem_destroy(NULL);
}

/* Per-operation shims so the Rust adapter owns the timed loop (native adapter). */
int bench_rust_sem_post(void)
{
    return sem_post(&s_api_sem);
}

int bench_rust_sem_wait(void)
{
    return sem_wait(&s_api_sem);
}

int bench_rust_mutex_u_create(void)
{
    (void)bench_rust_now_ticks(); /* see bench_rust_sem_create */
    return (int)port_mutex_u_create(NULL);
}

int bench_rust_mutex_u_measure(uint64_t *ticks)
{
    return (int)port_mutex_u_measure(NULL, ticks);
}

int bench_rust_mutex_u_destroy(void)
{
    return (int)port_mutex_u_destroy(NULL);
}

int bench_rust_mutex_u_lock(void)
{
    return pthread_mutex_lock(&s_api_mutex);
}

int bench_rust_mutex_u_unlock(void)
{
    return pthread_mutex_unlock(&s_api_mutex);
}

int bench_rust_alloc_create(void)
{
    (void)bench_rust_now_ticks(); /* see bench_rust_sem_create */
    return (int)port_alloc_create(NULL);
}

int bench_rust_alloc_measure(uint64_t *ticks)
{
    return (int)port_alloc_measure(NULL, ticks);
}

int bench_rust_alloc_destroy(void)
{
    return (int)port_alloc_destroy(NULL);
}

int bench_rust_sleep_create(void)
{
    (void)bench_rust_now_ticks(); /* see bench_rust_sem_create */
    return (int)port_sleep_create(NULL);
}

int bench_rust_sleep_measure(uint64_t *ticks)
{
    return (int)port_sleep_measure(NULL, ticks);
}

int bench_rust_sleep_destroy(void)
{
    return (int)port_sleep_destroy(NULL);
}

/* Thin sleep shim: keeps the NuttX `struct timespec` layout out of Rust. */
int bench_rust_nanosleep_ms(unsigned int ms)
{
    const struct timespec req = { .tv_sec = 0, .tv_nsec = (long)ms * 1000000L };
    return nanosleep(&req, NULL);
}

int bench_rust_sched_create(void)
{
    return (int)port_sched_create(NULL);
}

int bench_rust_sched_measure(uint64_t *ticks)
{
    return (int)port_sched_measure(NULL, ticks);
}

int bench_rust_sched_destroy(void)
{
    return (int)port_sched_destroy(NULL);
}

int bench_rust_notify_create(void)
{
    return (int)port_notify_create(NULL);
}

int bench_rust_notify_measure(uint64_t *ticks)
{
    return (int)port_notify_measure(NULL, ticks);
}

int bench_rust_notify_destroy(void)
{
    return (int)port_notify_destroy(NULL);
}

int bench_rust_notify_drain(void)
{
    while (sem_trywait(&s_notify_done) == 0) {
        /* drain stale tokens */
    }
    return 0;
}

int bench_rust_notify_go(void)
{
    if (!s_notify_active) {
        return (int)BENCH_PORT;
    }
    return (sem_post(&s_notify_go) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_notify_done(void)
{
    return (sem_wait(&s_notify_done) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_notify_signal(void)
{
    return s_notify_signal;
}

int bench_rust_prio_create(void)
{
    return (int)port_prio_create(NULL);
}

int bench_rust_prio_measure(uint64_t *ticks)
{
    return (int)port_prio_measure(NULL, ticks);
}

int bench_rust_prio_destroy(void)
{
    return (int)port_prio_destroy(NULL);
}

int bench_rust_prio_ready(void)
{
    return s_prio_active;
}

unsigned int bench_rust_prio_n(void)
{
    return NUTTX_PRIO_N;
}

int bench_rust_prio_reset(void)
{
    s_prio_n = 0;
    return 0;
}

int bench_rust_prio_release(void)
{
    return (sem_post(&s_prio_release) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_prio_all(void)
{
    return (sem_wait(&s_prio_all) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_prio_check(void)
{
    if (s_prio_n != NUTTX_PRIO_N) {
        return 1;
    }
    for (uint32_t i = 0; i < NUTTX_PRIO_N; i++) {
        if (s_prio_trace[i] != (uint8_t)(NUTTX_PRIO_N - 1 - i)) {
            return 1;
        }
    }
    return 0;
}

int bench_rust_prio_ack(void)
{
    return (sem_post(&s_prio_ack) != 0) ? (int)BENCH_PORT : 0;
}
#endif /* BENCH_LANGUAGE_RUST */
