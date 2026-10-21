/* NuttX benchmark firmware (STM32F4 Discovery / ESP32-C6 DevKitC).
 *
 * Implements UART protocol v1 (UART protocol v1) over the
 * NuttX console (USART2 PA2/PA3 on stm32f4discovery:nsh; USB-Serial-JTAG on
 * esp32c6-devkitc:bench).
 *
 *   - sends the unsolicited `ready` frame at boot
 *   - answers `hello` while idle with `ready` plus the challenge echo
 *   - accepts `start`, runs bench_run_suite case by case, and emits
 *     started, one `measurement` frame per sample, then `complete`
 *     (or `error` on failure; partial measurements are flushed first)
 *   - logs `cancel` only (best effort)
 *
 * Built as a NuttX built-in application (examples/hello pattern).
 * The console is inherited from the nsh shell (CONFIG_USART2_SERIAL_CONSOLE=y)
 * or the USB-Serial-JTAG driver (CONFIG_ESPRESSIF_USBSERIAL=y).
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <termios.h>
#include "rtos_bench.h"

/* Runtime stack/heap reporting needs NuttX headers that the host syntax check
 * does not have, so the includes and their users are gated on the real app
 * build (BENCH_NUTTX_APP), exactly like the pacing delay below. */
#ifdef BENCH_NUTTX_APP
#  include <sched.h>          /* task_create(), nxsched_foreach_t */
#  include <malloc.h>          /* mallinfo() */
#  include <nuttx/sched.h>     /* nxsched_self(), struct tcb_s */
#  if defined(CONFIG_ARCH_CHIP_RP23XX) && defined(CONFIG_CDCACM)
#    include <nuttx/usb/cdcacm.h> /* cdcacm_initialize() */
#  endif
#  ifdef CONFIG_STACK_COLORATION
#    include <nuttx/arch.h>    /* up_check_tcbstack() */
#  endif
#endif

/* usleep() is a POSIX/NuttX call; the host syntax check compiles with strict
 * C11 where it is not declared and does not define BENCH_NUTTX_APP, so the
 * console pacing delay is a no-op there. */
#ifdef BENCH_NUTTX_APP
#  define bench_delay_us(usec) usleep(usec)
#else
#  define bench_delay_us(usec) do { (void)(usec); } while (0)
#endif

#if defined(CONFIG_ARCH_CHIP_ESP32C6)
#define PROTO_BOARD       "esp32c6"
#define PROTO_APPLICATION "nuttx-c6-bench"
#elif defined(CONFIG_ARCH_CHIP_NRF52)
#define PROTO_BOARD       "nrf52840dk"
#define PROTO_APPLICATION "nuttx-nrf52840dk-bench"
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
#define PROTO_BOARD       "pico2"
#define PROTO_APPLICATION "nuttx-pico2-bench"
#elif defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_BOARD       "stm32h723"
#define PROTO_APPLICATION "nuttx-h723-bench"
#else
#define PROTO_BOARD       "stm32f4discovery"
#define PROTO_APPLICATION "nuttx-stm32f4-bench"
#endif

#ifndef BENCH_BUILD_ID
#if defined(CONFIG_ARCH_CHIP_ESP32C6)
#define BENCH_BUILD_ID "nuttx-c6-bench"
#elif defined(CONFIG_ARCH_CHIP_NRF52)
#define BENCH_BUILD_ID "nuttx-nrf52840dk-bench"
#elif defined(CONFIG_ARCH_CHIP_RP23XX)
#define BENCH_BUILD_ID "nuttx-pico2-bench"
#elif defined(CONFIG_ARCH_CHIP_STM32H7)
#define BENCH_BUILD_ID "nuttx-h723-bench"
#else
#define BENCH_BUILD_ID "nuttx-stm32f4-bench"
#endif
#endif

#define PROTO_RTOS        "nuttx"
#define PROTO_KERNEL      "nuttx-master"
#define PROTO_LANGUAGE    "c"
#define PROTO_PROTOCOL    1

/* Cases this adapter advertises. The C6 case-4 harness in isr.c uses a free
 * SYSTIMER alarm (target 1; the tick owns alarm 0) and the C6 performance
 * counter, and is implemented for parity with the other paths. Note the C6
 * board has an intermittent USB-JTAG hang (pre-existing quirk) that can abort
 * a run regardless of the case set; re-flash/retry if it strikes.
 * The RP2350 uses TIMER0/TIMER1 alarms with the Cortex-M33 DWT at 150 MHz, so
 * it also advertises the full 0-11 set. */
/* Case 25 is implemented one chip at a time; advertise it only where the
 * masked-priority probe exists. */
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_MAXDIS_CASE BENCH_MAX_INTERRUPT_DISABLED,
#define PROTO_MAXDIS_ID   ",25"
#else
#define PROTO_MAXDIS_CASE
#define PROTO_MAXDIS_ID
#endif
/* Case 20 is implemented one chip at a time as well. */
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_NESTED_CASE BENCH_NESTED_IRQ_OVERHEAD,
#define PROTO_NESTED_ID   ",20"
#else
#define PROTO_NESTED_CASE
#define PROTO_NESTED_ID
#endif

#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_RP23XX) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
/* Case 11 is implemented on the nRF52840, ESP32-C6, STM32F4, STM32H7 and RP2350 paths.
 * Case 18 IPC payload sweep is implemented via variable-length POSIX message
 * queues and advertised on all four: the ESP32-C6 previously ran
 * CONFIG_SCHED_TICKLESS, whose ~1500-cycle wakeup quantization made the C6
 * per-size medians non-monotonic; the C6 now uses the periodic tick that the
 * other NuttX boards use, which is monotonic. */
#if defined(CONFIG_ARCH_CHIP_NRF52) || defined(CONFIG_ARCH_CHIP_RP23XX) || \
    defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_PAYLOAD_CASE BENCH_IPC_PAYLOAD_SWEEP,
#define PROTO_PAYLOAD_ID   ",18"
#else
#define PROTO_PAYLOAD_CASE
#define PROTO_PAYLOAD_ID
#endif
/* Case 19 throughput reuses the POSIX MQ path and is advertised on all four. */
#define PROTO_TP_CASE BENCH_IPC_THROUGHPUT,
#define PROTO_TP_ID   ",19"
/* Case 27 zero-copy IPC uses the shared pointer-transfer POSIX MQ port;
 * advertise it one board at a time as each is hardware-verified. */
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_RP23XX) || defined(CONFIG_ARCH_CHIP_ESP32C6) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_ZCOPY_CASE BENCH_ZERO_COPY_IPC,
#define PROTO_ZCOPY_ID   ",27"
#else
#define PROTO_ZCOPY_CASE
#define PROTO_ZCOPY_ID
#endif
/* Case 24 heap fragmentation uses the kernel mallinfo() query; advertise it
 * one board at a time as each is hardware-verified. */
#if defined(CONFIG_ARCH_CHIP_STM32F4) || defined(CONFIG_ARCH_CHIP_RP23XX) || \
    defined(CONFIG_ARCH_CHIP_ESP32C6) || defined(CONFIG_ARCH_CHIP_NRF52) || \
    defined(CONFIG_ARCH_CHIP_STM32H7)
#define PROTO_FRAG_CASE BENCH_HEAP_FRAGMENTATION,
#define PROTO_FRAG_ID   ",24"
#else
#define PROTO_FRAG_CASE
#define PROTO_FRAG_ID
#endif
/* Case 28 common_arena_fragmentation is runner-native (shared arena in
 * firmware/common); it needs no kernel call and is advertised on every NuttX
 * board in both languages. */
#define PROTO_ARENA_CASE BENCH_COMMON_ARENA_FRAGMENTATION,
#define PROTO_ARENA_ID   ",28"
#ifdef BENCH_LANGUAGE_RUST
#define PROTO_CASES       "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17" PROTO_PAYLOAD_ID PROTO_TP_ID PROTO_ZCOPY_ID PROTO_NESTED_ID PROTO_MAXDIS_ID PROTO_FRAG_ID ",26" PROTO_ARENA_ID "]"
static const bench_case s_supported_cases[] = {
    BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF,
    BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP,
    BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY,
    BENCH_TIMER_CALLBACK,
    BENCH_TRIGGER_TO_ISR, /* case 10: software-asserted interrupt */
    BENCH_PERIODIC_JITTER_LOADED, /* case 11: hardware release + load */
    BENCH_SEMAPHORE_LATENCY, BENCH_MUTEX_UNCONTENDED, BENCH_ALLOC_LATENCY,
    BENCH_SLEEP_ACCURACY,
    BENCH_SCHEDULER_OVERHEAD, BENCH_NOTIFICATION_LATENCY,
    PROTO_PAYLOAD_CASE
    PROTO_TP_CASE
    PROTO_ZCOPY_CASE
    PROTO_NESTED_CASE
    PROTO_MAXDIS_CASE
    PROTO_FRAG_CASE
    BENCH_PRIORITY_SCHEDULING_CORRECTNESS, /* cases 16/17/26 */
    PROTO_ARENA_CASE
};
#else
/* Cases 12-15 (uncontended sync / alloc / delay) are plain POSIX and are
 * implemented on every NuttX C path; cases 16/17/26 use sched_yield/POSIX
 * semaphores/SCHED_FIFO pthreads and are likewise shared. */
#define PROTO_CASES       "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17" PROTO_PAYLOAD_ID PROTO_TP_ID PROTO_ZCOPY_ID PROTO_NESTED_ID PROTO_MAXDIS_ID PROTO_FRAG_ID ",26" PROTO_ARENA_ID "]"
static const bench_case s_supported_cases[] = {
    BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF,
    BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP,
    BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY,
    BENCH_TIMER_CALLBACK,
    BENCH_TRIGGER_TO_ISR, /* case 10: software-asserted interrupt */
    BENCH_PERIODIC_JITTER_LOADED, /* case 11: hardware release + load */
    BENCH_SEMAPHORE_LATENCY, BENCH_MUTEX_UNCONTENDED, BENCH_ALLOC_LATENCY,
    BENCH_SLEEP_ACCURACY,
    BENCH_SCHEDULER_OVERHEAD, BENCH_NOTIFICATION_LATENCY,
    PROTO_PAYLOAD_CASE
    PROTO_TP_CASE
    PROTO_ZCOPY_CASE
    PROTO_NESTED_CASE
    PROTO_MAXDIS_CASE
    PROTO_FRAG_CASE
    BENCH_PRIORITY_SCHEDULING_CORRECTNESS, /* cases 16/17/26 */
    PROTO_ARENA_CASE
};
#endif
#else
#define PROTO_CASES       "[0,1,2,3,4,5,6,7,8,9,10]"
static const bench_case s_supported_cases[] = {
    BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF,
    BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP,
    BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY,
    BENCH_TIMER_CALLBACK,
    BENCH_TRIGGER_TO_ISR, /* case 10: software-asserted interrupt */
};
#endif
#define PROTO_N_SUPPORTED_CASES \
    (sizeof(s_supported_cases) / sizeof(s_supported_cases[0]))

#define FRAME_MAX       1024
#define LINE_BUF_SIZE   (FRAME_MAX + 8)

#define MAX_CELLS    32
#define MAX_CASES    29 /* highest protocol ID (28) + 1 */
#define MAX_STR_LEN  64
#define MAX_RUN_ID   64
#define SINK_MAX_SAMPLES  3072 /* 4096*24B overflowed the STM32F4 SRAM region
                                * once the 8 KiB case-28 arena was added; 3072
                                * still covers the 1000-rep default per case. */

/* --------------------------------------------------------------- bench port */

extern bench_status nuttx_bench_port_init(bench_port *);

/* Globals shared between the TX/sink/session code. */
static char s_run_id[MAX_RUN_ID];
static uint32_t g_case_count[MAX_CASES];
static uint64_t g_ticks_per_second;

/* Requested case list, in protocol order, for `complete` per_case_counts. */
static bench_case s_start_cases[MAX_CASES];
static size_t s_start_n_cases;

/* ---------------------------------------------------------------- TX */

static char s_tx_buf[FRAME_MAX + 2];
static int g_console_fd = -1;

/* Idle gap left after every transmitted frame. The C6 USB-Serial-JTAG
 * lower-half can block inside the tty driver when fed a continuous burst
 * (observed as a stalled run and a lost USB-JTAG); a short gap after each
 * frame lets its FIFO drain between frames. This runs outside every timed
 * region, so it does not affect any measurement. */
#define TX_FRAME_GAP_US 500

static void tx_string(size_t len)
{
    if (len + 1 >= sizeof(s_tx_buf) || g_console_fd < 0) {
        return;
    }
    s_tx_buf[len] = '\n';
    s_tx_buf[len + 1] = '\0';

    /* Non-blocking, paced, bounded write. The console is O_NONBLOCK: a 1000
     * frame flush can fill the C6 USB-Serial-JTAG TX ring, and blocking inside
     * the tty driver there can wedge the console (observed as a stalled run and
     * a lost USB-JTAG). Retry on EAGAIN so the driver drains, and drop the
     * frame after a bounded wait rather than hang the board. Pacing happens
     * after the timed region, so it does not affect any measurement. */
    size_t off = 0;
    const size_t total = len + 1;
    int stalls = 0;
    while (off < total) {
        ssize_t written = write(g_console_fd, s_tx_buf + off, total - off);
        if (written > 0) {
            off += (size_t)written;
            stalls = 0;
        } else if (written < 0 && errno == EAGAIN) {
            if (++stalls > 25000) { /* ~5 s at 200 us */
                return;
            }
            bench_delay_us(200);
        } else {
            return;
        }
    }
    bench_delay_us(TX_FRAME_GAP_US);
}

static void send_ready(const char *challenge)
{
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"ready\",\"board\":\"%s\","
                     "\"rtos\":\"%s\",\"kernel\":\"%s\",\"language\":\"%s\","
                     "\"build_id\":\"%s\",\"application\":\"%s\","
                     "\"cases\":" PROTO_CASES,
                     PROTO_PROTOCOL, PROTO_BOARD, PROTO_RTOS, PROTO_KERNEL,
                     PROTO_LANGUAGE, BENCH_BUILD_ID, PROTO_APPLICATION);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    if (challenge != NULL) {
        n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                      ",\"challenge\":\"%s\"", challenge);
        if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
            return;
        }
    }
    s_tx_buf[n] = '}';
    tx_string((size_t)(n + 1));
}

static void send_started(const char *run_id)
{
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"started\","
                     "\"run_id\":\"%s\",\"build_id\":\"%s\"}",
                     PROTO_PROTOCOL, run_id, BENCH_BUILD_ID);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    tx_string((size_t)n);
}

static void send_measurement(const char *run_id, int case_id, uint32_t index,
                             uint64_t ticks, uint32_t variant)
{
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"measurement\","
                     "\"run_id\":\"%s\",\"case\":%d,\"index\":%u,"
                     "\"ticks\":%llu,\"ticks_per_second\":%llu,"
                     "\"variant\":%u}",
                     PROTO_PROTOCOL, run_id, case_id, index,
                     (unsigned long long)ticks,
                     (unsigned long long)g_ticks_per_second,
                     (unsigned)variant);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    tx_string((size_t)n);
}

#ifdef BENCH_NUTTX_APP
#  ifdef CONFIG_STACK_COLORATION
/* Accumulator for the per-task stack scan; the callback runs for every TCB. */
struct bench_stack_scan {
    uint32_t count;
    size_t worst;
    size_t best;
};

static void bench_stack_scan_cb(FAR struct tcb_s *tcb, FAR void *arg)
{
    struct bench_stack_scan *scan = (struct bench_stack_scan *)arg;
    size_t size = tcb->adj_stack_size;

    if (size == 0U) {
        return;
    }
    size_t used = up_check_tcbstack(tcb, size);
    size_t free_bytes = (size > used) ? (size - used) : 0U;
    if (scan->count == 0U || free_bytes < scan->worst) {
        scan->worst = free_bytes;
    }
    if (scan->count == 0U || free_bytes > scan->best) {
        scan->best = free_bytes;
    }
    scan->count++;
}
#  endif

/* Entry for the throwaway task used to measure per-task RAM overhead; it only
 * sleeps, so it never runs user code. */
static int ram_probe_task(int argc, FAR char *argv[])
{
    (void)argc;
    (void)argv;
    for (;;) {
        sleep(1);
    }
}

/* Kernel RAM overhead for one added task beyond its stack (TCB + allocator
 * metadata), bytes: the in-use heap delta for a 768-byte task, minus the stack
 * request. Returns 0 when the allocator does not account for the task. */
static unsigned long per_task_ram_overhead_bytes(void)
{
    const int stack_size = 768;
    struct mallinfo before = mallinfo();
    int pid = task_create("memprobe", SCHED_PRIORITY_DEFAULT, stack_size,
                          ram_probe_task, NULL);
    if (pid < 0) {
        return 0UL;
    }
    struct mallinfo after = mallinfo();
    (void)task_delete(pid);
    usleep(20000); /* let the OS reap the task */
    if (after.uordblks <= before.uordblks) {
        return 0UL;
    }
    unsigned long delta = (unsigned long)(after.uordblks - before.uordblks);
    if (delta <= (unsigned long)stack_size) {
        return 0UL;
    }
    return delta - (unsigned long)stack_size;
}
#endif

static void send_complete(const char *run_id)
{
    unsigned long stack_free = 0;
    unsigned long heap_free = 0;
    unsigned long heap_over = 0;
    unsigned long per_task_ram = 0;
    unsigned long task_count = 0;
    unsigned long worst_stack = 0;
    unsigned long best_stack = 0;

#ifdef BENCH_NUTTX_APP
#  ifdef CONFIG_STACK_COLORATION
    /* Free stack remaining, matching FreeRTOS's minimum-ever-free semantics:
     * up_check_tcbstack reports bytes used, so subtract from the stack size. */
    {
        FAR struct tcb_s *tcb = nxsched_self();
        size_t size = tcb->adj_stack_size;
        size_t used = up_check_tcbstack(tcb, size);
        stack_free = (unsigned long)(size > used ? size - used : 0U);
    }
    /* Per-task stack aggregate over every live TCB. */
    {
        struct bench_stack_scan scan = {0U, 0U, 0U};
        nxsched_foreach(bench_stack_scan_cb, &scan);
        task_count = (unsigned long)scan.count;
        worst_stack = (unsigned long)scan.worst;
        best_stack = (unsigned long)scan.best;
    }
#  endif
    {
        struct mallinfo mi = mallinfo();
        heap_free = (unsigned long)(mi.fordblks > 0 ? mi.fordblks : 0);
    }
    /* Per-allocation overhead: in-use byte delta for a 1-byte malloc. */
    {
        struct mallinfo before = mallinfo();
        void *p = malloc(1);
        if (p != NULL) {
            struct mallinfo after = mallinfo();
            free(p);
            if (after.uordblks > before.uordblks &&
                (after.uordblks - before.uordblks) >= 1) {
                heap_over = (unsigned long)(after.uordblks - before.uordblks - 1);
            }
        }
    }
    per_task_ram = per_task_ram_overhead_bytes();
#endif

    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"complete\","
                     "\"run_id\":\"%s\",\"per_case_counts\":[",
                     PROTO_PROTOCOL, run_id);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    for (size_t i = 0; i < s_start_n_cases; i++) {
        unsigned case_id = (unsigned)s_start_cases[i];
        int m = snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                         "%s%u", (i > 0) ? "," : "",
                         (case_id < MAX_CASES) ? g_case_count[case_id] : 0U);
        if (m < 0 || (size_t)n + (size_t)m >= sizeof(s_tx_buf)) {
            return;
        }
        n += m;
    }
    int m = snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                     "],\"status\":\"ok\",\"stack_high_water_bytes\":%lu,"
                     "\"heap_free_bytes\":%lu,\"heap_overhead_bytes\":%lu,"
                     "\"per_task_ram_overhead_bytes\":%lu,\"task_count\":%lu,"
                     "\"worst_stack_headroom_bytes\":%lu,"
                     "\"best_stack_headroom_bytes\":%lu}",
                     stack_free, heap_free, heap_over,
                     per_task_ram, task_count, worst_stack, best_stack);
    if (m < 0 || (size_t)n + (size_t)m >= sizeof(s_tx_buf)) {
        return;
    }
    tx_string((size_t)(n + m));
}

static void send_error(const char *code, const char *message,
                       const char *run_id)
{
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"error\",",
                     PROTO_PROTOCOL);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    if (run_id != NULL) {
        n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                      "\"run_id\":\"%s\",", run_id);
        if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
            return;
        }
    }
    n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                  "\"code\":\"%s\",\"message\":\"%s\"}", code, message);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    tx_string((size_t)n);
}

/* ------------------------------------------------------- sink adapter */

struct sink_sample {
    uint32_t case_id; /* protocol ID, not the bench_case enum value */
    uint32_t index;
    uint64_t ticks;
    uint32_t variant; /* schema-v2 discriminator; 0 for plain cases */
};

/* The C runner passes bench_case enum values; the wire protocol uses protocol
 * IDs. 0-15 coincide; the sparse phase-2 enums must be translated back. */
static uint32_t protocol_id(bench_case c)
{
    switch (c) {
        case BENCH_SCHEDULER_OVERHEAD: return 16;
        case BENCH_NOTIFICATION_LATENCY: return 17;
        case BENCH_TICK_OVERHEAD: return 21;
        case BENCH_MAX_INTERRUPT_DISABLED: return 25;
        case BENCH_NESTED_IRQ_OVERHEAD: return 20;
        case BENCH_IPC_PAYLOAD_SWEEP: return 18;
        case BENCH_IPC_THROUGHPUT: return 19;
        case BENCH_ZERO_COPY_IPC: return 27;
        case BENCH_HEAP_FRAGMENTATION: return 24;
        case BENCH_COMMON_ARENA_FRAGMENTATION: return 28;
        case BENCH_PRIORITY_SCHEDULING_CORRECTNESS: return 26;
        default: return (uint32_t)c;
    }
}

static struct {
    struct sink_sample samples[SINK_MAX_SAMPLES];
    size_t count;
} s_sink;

static bench_status sink_record_variant_id(void *ctx, uint32_t protocol,
                                           uint32_t variant, uint32_t index,
                                           uint64_t ticks)
{
    (void)ctx;
    if (s_sink.count >= SINK_MAX_SAMPLES) {
        return BENCH_PORT;
    }
    s_sink.samples[s_sink.count].case_id = protocol;
    s_sink.samples[s_sink.count].index = index;
    s_sink.samples[s_sink.count].ticks = ticks;
    s_sink.samples[s_sink.count].variant = variant;
    s_sink.count++;
    return BENCH_OK;
}

static bench_status sink_record_id(void *ctx, uint32_t protocol,
                                   uint32_t index, uint64_t ticks)
{
    return sink_record_variant_id(ctx, protocol, 0, index, ticks);
}

static bench_status sink_record(void *ctx, bench_case case_id, uint32_t index,
                                uint64_t ticks, uint64_t ticks_per_second)
{
    (void)ticks_per_second;
    return sink_record_id(ctx, protocol_id(case_id), index, ticks);
}

static bench_status sink_record_variant(void *ctx, bench_case case_id,
                                        uint32_t variant, uint32_t index,
                                        uint64_t ticks, uint64_t ticks_per_second)
{
    (void)ticks_per_second;
    return sink_record_variant_id(ctx, protocol_id(case_id), variant, index, ticks);
}

static bench_status sink_flush(void *ctx)
{
    (void)ctx;
    for (size_t i = 0; i < s_sink.count; i++) {
        send_measurement(s_run_id, (int)s_sink.samples[i].case_id,
                         s_sink.samples[i].index,
                         s_sink.samples[i].ticks,
                         s_sink.samples[i].variant);
        if (s_sink.samples[i].case_id == BENCH_TIMER_OVERHEAD) {
            g_case_count[BENCH_TIMER_OVERHEAD]++;
        } else {
            unsigned case_id = (unsigned)s_sink.samples[i].case_id;
            if (case_id < MAX_CASES) {
                g_case_count[case_id]++;
            }
        }
    }
    s_sink.count = 0;
    return BENCH_OK;
}

/* ------------------------------------------------------------- session */

#ifdef BENCH_LANGUAGE_RUST
/* Bridge the Rust application core (firmware/nuttx/rust) to this session's
 * sink. The C side keeps the UART/JSON protocol layer. */
extern int rust_bench_run_case(uint32_t case_id, uint32_t warmup,
                               uint32_t repetitions, void *ctx,
                               int (*record)(void *, uint32_t, uint32_t,
                                             uint64_t, uint64_t),
                               int (*record_variant)(void *, uint32_t, uint32_t,
                                                     uint32_t, uint64_t, uint64_t),
                               int (*flush)(void *));

static int rust_record_bridge(void *ctx, uint32_t case_id, uint32_t index,
                              uint64_t ticks, uint64_t hz)
{
    (void)hz;
    /* The Rust adapter already passes protocol IDs. */
    return sink_record_id(ctx, case_id, index, ticks) == BENCH_OK ? 0 : -1;
}

static int rust_record_variant_bridge(void *ctx, uint32_t case_id, uint32_t variant,
                                      uint32_t index, uint64_t ticks, uint64_t hz)
{
    (void)hz;
    return sink_record_variant_id(ctx, case_id, variant, index, ticks) == BENCH_OK
               ? 0
               : -1;
}

static int rust_flush_bridge(void *ctx)
{
    return sink_flush(ctx) == BENCH_OK ? 0 : -1;
}
#endif

/* Requested cases arrive as protocol IDs; map the sparse phase-2 IDs to their
 * enum values (0-15 already match the enum order). */
static bench_case map_case(unsigned id)
{
    switch (id) {
        case 16: return BENCH_SCHEDULER_OVERHEAD;
        case 17: return BENCH_NOTIFICATION_LATENCY;
        case 21: return BENCH_TICK_OVERHEAD;
        case 25: return BENCH_MAX_INTERRUPT_DISABLED;
        case 18: return BENCH_IPC_PAYLOAD_SWEEP;
        case 19: return BENCH_IPC_THROUGHPUT;
        case 27: return BENCH_ZERO_COPY_IPC;
        case 24: return BENCH_HEAP_FRAGMENTATION;
        case 20: return BENCH_NESTED_IRQ_OVERHEAD;
        case 26: return BENCH_PRIORITY_SCHEDULING_CORRECTNESS;
        case 28: return BENCH_COMMON_ARENA_FRAGMENTATION;
        default: return (bench_case)id;
    }
}

static void run_session(bench_settings settings, const bench_case *cases,
                        size_t n_cases)
{
#ifdef BENCH_LANGUAGE_RUST
    /* Rust application core: native NuttX queue/timer via FFI shims. */
    send_started(s_run_id);

    int rc = 0;
    for (size_t i = 0; i < n_cases && rc == 0; i++) {
        rc = rust_bench_run_case((uint32_t)cases[i], settings.warmup,
                                 settings.repetitions, NULL,
                                 rust_record_bridge, rust_record_variant_bridge,
                                 rust_flush_bridge);
    }

    if (rc == 0) {
        send_complete(s_run_id);
        return;
    }

    const char *code = "bench_failed";
    const char *msg = "benchmark execution failed";
    if (rc == -1) {
        code = "invalid_args";
        msg = "invalid benchmark arguments";
    } else if (rc == -2) {
        code = "unsupported";
        msg = "case not supported by this firmware";
    } else if (rc == -3) {
        code = "port_error";
        msg = "port operation failed";
    } else if (rc == -4) {
        code = "corrupt";
        msg = "corrupted timing or queue payload";
    }
    send_error(code, msg, s_run_id);
#else
    bench_port port;
    bench_status status = nuttx_bench_port_init(&port);
    if (status != BENCH_OK) {
        send_error("port_init_failed", "bench port unavailable", s_run_id);
        return;
    }

    bench_sink sink = {
        .ctx = NULL,
        .record = sink_record,
        .record_variant = sink_record_variant,
        .flush = sink_flush,
    };

    send_started(s_run_id);

    for (size_t i = 0; i < n_cases && status == BENCH_OK; i++) {
        status = bench_run_case(&port, &sink, map_case((unsigned)cases[i]),
                                settings);
    }

    if (status == BENCH_OK) {
        send_complete(s_run_id);
    } else {
        const char *code = "bench_failed";
        const char *msg = "benchmark execution failed";
        if (status == BENCH_INVALID) {
            code = "invalid_args";
            msg = "invalid benchmark arguments";
        } else if (status == BENCH_UNSUPPORTED) {
            code = "unsupported";
            msg = "case not supported by this firmware";
        } else if (status == BENCH_PORT) {
            code = "port_error";
            msg = "port operation failed";
        } else if (status == BENCH_CORRUPT) {
            code = "corrupt";
            msg = "corrupted timing or queue payload";
        }
        send_error(code, msg, s_run_id);
    }
#endif
}

/* --------------------------------------------------------- start frame */

static uint32_t s_start_warmup;
static uint32_t s_start_repetitions;

static void parse_start(const char *line)
{
    s_run_id[0] = '\0';
    for (size_t i = 0; i < MAX_CASES; i++) {
        g_case_count[i] = 0;
    }

    const char *p = strstr(line, "\"run_id\"");
    if (p) {
        p = strchr(p + 8, ':');
        if (p) {
            p = strchr(p, '"');
            if (p) {
                p++;
                size_t len = strcspn(p, "\"");
                if (len >= MAX_RUN_ID) {
                    len = MAX_RUN_ID - 1;
                }
                memcpy(s_run_id, p, len);
                s_run_id[len] = '\0';
            }
        }
    }

    p = strstr(line, "\"warmup\"");
    if (p) {
        p = strchr(p + 8, ':');
        if (p) {
            s_start_warmup = (uint32_t)atoi(p + 1);
        }
    }

    p = strstr(line, "\"repetitions\"");
    if (p) {
        p = strchr(p + 11, ':');
        if (p) {
            s_start_repetitions = (uint32_t)atoi(p + 1);
        }
    }

    p = strstr(line, "\"cases\"");
    if (p) {
        p = strchr(p + 7, '[');
        if (p) {
            for (size_t i = 0;
                 i < PROTO_N_SUPPORTED_CASES && i < MAX_CASES;
                 i++) {
                s_start_cases[i] = s_supported_cases[i];
            }
            s_start_n_cases = PROTO_N_SUPPORTED_CASES;
            p++;
            int idx = 0;
            long value = 0;
            int have = 0;
            for (; *p && *p != ']'; p++) {
                if (*p >= '0' && *p <= '9') {
                    value = value * 10 + (*p - '0');
                    have = 1;
                } else if (have) {
                    if (idx < MAX_CASES) {
                        s_start_cases[idx++] = (bench_case)value;
                    }
                    value = 0;
                    have = 0;
                }
            }
            if (have && idx < MAX_CASES) {
                s_start_cases[idx++] = (bench_case)value;
            }
            if (idx > 0) {
                s_start_n_cases = (size_t)idx;
            }
        }
    }
}

/* ------------------------------------------------------------ main loop */

/* Return the string value of a `"field":"value"` pair, terminated in place at
 * the closing quote. The previous version returned a pointer into the line
 * without terminating the value, so send_ready(challenge) embedded the whole
 * remainder of the hello frame (the "echo" that corrupted `ready`). */
static char *extract_field(char *line, const char *field)
{
    char *p = strstr(line, field);
    if (!p) {
        return NULL;
    }
    p = strchr(p + strlen(field), ':');
    if (!p) {
        return NULL;
    }
    p = strchr(p, '"');
    if (!p) {
        return NULL;
    }
    p++;
    char *end = strchr(p, '"');
    if (end != NULL) {
        *end = '\0';
    }
    return p;
}

static void configure_console(void)
{
    /* Open the console explicitly: without NSH, the init task's stdio may not
     * be bound to a tty, and the serial layer echoes input by default.
     *
     * Build the termios from scratch (zeroed, then explicit flags) rather than
     * reading it back; set a valid speed so the driver accepts TCSETS. */
#if defined(CONFIG_ARCH_CHIP_RP23XX)
    /* RP2350: the CDC ACM tty is registered by cdcacm_initialize() above, but
     * opening it fails with -ENOTCONN until the host opens the port (see
     * nsh_waitusbready in nshlib/nsh_usbconsole.c). Retry until the host
     * connects; falling back to stdin would leave the protocol bound to a
     * non-USB fd and the board unreachable. O_NONBLOCK avoids waiting for DCD
     * inside open. */
    for (;;) {
        g_console_fd = open("/dev/ttyACM0", O_RDWR | O_NONBLOCK);
        if (g_console_fd >= 0) {
            break;
        }
        g_console_fd = open("/dev/console", O_RDWR | O_NONBLOCK);
        if (g_console_fd >= 0) {
            break;
        }
        usleep(200000);
    }
#else
    g_console_fd = open("/dev/console", O_RDWR);
    if (g_console_fd < 0) {
        g_console_fd = STDIN_FILENO;
    }
#endif

    struct termios tio;
    memset(&tio, 0, sizeof(tio));
    tio.c_cflag = CS8 | CREAD | CLOCAL;
    (void)cfsetispeed(&tio, B115200);
    (void)cfsetospeed(&tio, B115200);
    tio.c_iflag = 0;
    tio.c_oflag = 0;
    tio.c_lflag = 0;
    tio.c_cc[VMIN] = 1;
    tio.c_cc[VTIME] = 0;
    (void)tcsetattr(g_console_fd, TCSANOW, &tio);

    /* Non-blocking so tx_string() can pace a 1000-frame flush and the idle read
     * loop can sleep on EAGAIN instead of blocking inside the tty driver. */
    int flags = fcntl(g_console_fd, F_GETFL, 0);
    if (flags >= 0) {
        (void)fcntl(g_console_fd, F_SETFL, flags | O_NONBLOCK);
    }
}

#if defined(CONFIG_ARCH_CHIP_RP23XX)
/* Host-requested reboot into the RP2350 USB bootloader (BOOTSEL) so the next
 * UF2 flash needs no physical button. RP2350 has no ROM_FUNC_RESET_USB_BOOT; it
 * exposes ROM_FUNC_REBOOT, exactly as pico-sdk's rom_reset_usb_boot does for
 * this chip. The bootrom table-lookup entry is the 16-bit pointer at 0x16
 * (BOOTROM_FUNC_TABLE_OFFSET 0x14 + 2), the secure ARM function flag is 0x4,
 * and ROM_FUNC_REBOOT is 'R' | ('B' << 8). The reboot flags select the BOOTSEL
 * reboot type (0x2) with no return on success (0x100). This never returns. */
static void pico2_reset_usb_boot(void)
{
    typedef void *(*rom_table_lookup_fn)(uint32_t code, uint32_t mask);
    typedef void (*rom_reboot_fn)(uint32_t flags, uint32_t delay_ms,
                                  uint32_t p0, uint32_t p1);
    rom_table_lookup_fn lookup =
        (rom_table_lookup_fn)(uintptr_t) * (volatile uint16_t *)0x16u;
    void *fn = lookup(0x4252u, 0x0004u);
    if (fn != NULL) {
        ((rom_reboot_fn)fn)(0x102u, 10u, 0u, 0u);
    }
    for (;;) {
        bench_delay_us(1000);
    }
}
#endif

int main(int argc, char *argv[])
{
    (void)argc;
    (void)argv;

    bench_port probe;
    (void)nuttx_bench_port_init(&probe);
    g_ticks_per_second = probe.ticks_per_second;

#if defined(CONFIG_ARCH_CHIP_RP23XX) && defined(CONFIG_CDCACM)
    /* The RP2350 arch USB driver only enables the controller and asserts the
     * D+ pull-up from usbdev_register(); the board bring-up does not register a
     * class driver, so register CDC ACM here before opening /dev/console. */
    (void)cdcacm_initialize(0, NULL);
#endif
    configure_console();
    send_ready(NULL);

    /* Static (not stack): the 1032-byte line buffer stays live across the
     * run_session call below, and the C runner's bench_port/bench_sink locals
     * overflow the 2048-byte C6 init task stack when it sits on the stack. */
    static char line[LINE_BUF_SIZE];
    while (1) {
        line[0] = '\0';
        size_t pos = 0;
        while (pos < sizeof(line) - 1) {
            char c;
            ssize_t n = read(g_console_fd, &c, 1);
            if (n == 0) {
                break;
            }
            if (n < 0) {
                if (errno == EAGAIN) {
                    /* No byte yet: yield so the USB driver can run. */
                    bench_delay_us(500);
                    continue;
                }
                break;
            }
            if (c == '\n' || c == '\r') {
                if (pos > 0) {
                    break;
                }
                continue;
            }
            line[pos++] = c;
        }
        line[pos] = '\0';
        if (pos == 0) {
            continue;
        }

        if (strstr(line, "\"type\":\"hello\"")) {
            const char *challenge = extract_field(line, "\"challenge\"");
            send_ready(challenge);
        } else if (strstr(line, "\"type\":\"bootsel\"")) {
#if defined(CONFIG_ARCH_CHIP_RP23XX)
            pico2_reset_usb_boot();
#endif
        } else if (strstr(line, "\"type\":\"start\"")) {
            parse_start(line);
            if (s_start_repetitions == 0) {
                send_error("bad_repetitions",
                           "repetitions must be nonzero", s_run_id);
            } else if (s_start_repetitions > SINK_MAX_SAMPLES) {
                send_error("too_many_samples",
                           "repetitions exceed the sample buffer capacity",
                           s_run_id);
            } else {
                bench_settings settings = {
                    .warmup = s_start_warmup,
                    .repetitions = s_start_repetitions,
                };
                run_session(settings, s_start_cases, s_start_n_cases);
            }
        } else if (strstr(line, "\"type\":\"cancel\"")) {
            /* best effort: log only */
        }
    }

    return 0;
}
