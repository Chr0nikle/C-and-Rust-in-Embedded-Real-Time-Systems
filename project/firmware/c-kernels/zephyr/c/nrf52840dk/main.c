/* Zephyr + STM32F4 Discovery benchmark firmware.
 *
 * Implements UART protocol v1 (UART protocol v1) over the
 * Zephyr console UART (USART2 PA2/PA3, 115200 8N1 on stm32f4_disco).
 *
 *   - sends the unsolicited `ready` frame at boot
 *   - answers `hello` while idle with `ready` plus the challenge echo
 *   - accepts `start`, runs bench_run_suite case by case, and emits
 *     started, one `measurement` frame per sample, then `complete`
 *     (or `error` on failure; partial measurements are flushed first)
 *   - logs `cancel` only (best effort)
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <zephyr/kernel.h>
#include <zephyr/console/console.h>
#include <zephyr/sys/mem_stats.h>
#include <sys_malloc.h>
#include "rtos_bench.h"

#ifndef BENCH_BUILD_ID
#define BENCH_BUILD_ID "zephyr-nrf52840dk-bench"
#endif

#define PROTO_BOARD       "nrf52840dk"
#define PROTO_RTOS        "zephyr"
#define PROTO_KERNEL      "zephyr-4.4.2"
#define PROTO_LANGUAGE    "c"
#define PROTO_APPLICATION "zephyr-nrf52840dk-bench"
#define PROTO_PROTOCOL    1

#define FRAME_MAX       1024
#define LINE_BUF_SIZE   (FRAME_MAX + 8)
#define SESSION_STACK   4096
#define SESSION_PRIO    5

#define MAX_CELLS    32
#define MAX_CASES    29 /* highest protocol ID (28) + 1 */
#define MAX_STR_LEN  64
#define MAX_RUN_ID   64
#define SINK_MAX_SAMPLES  4096

/* --------------------------------------------------------------- bench port */

extern bench_status zephyr_bench_port_init(bench_port *);

/* Globals shared between the TX/sink/session code. */
static char s_run_id[MAX_RUN_ID];
static uint32_t g_case_count[MAX_CASES];
static uint64_t g_ticks_per_second;

/* Cases this adapter advertises, in protocol ID order. 5-6 need the raised
 * host run_timeout_ms (the suite is ~600 KB over a slow console). */
#ifdef BENCH_LANGUAGE_RUST
#define PROTO_CASES "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,24,25,26,27,28]"
static const bench_case s_supported_cases[] = {
    BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF,
    BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP,
    BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY,
    BENCH_TIMER_CALLBACK, BENCH_TRIGGER_TO_ISR, BENCH_PERIODIC_JITTER_LOADED,
    BENCH_SEMAPHORE_LATENCY, BENCH_MUTEX_UNCONTENDED, BENCH_ALLOC_LATENCY,
    BENCH_SLEEP_ACCURACY,
    BENCH_SCHEDULER_OVERHEAD, BENCH_NOTIFICATION_LATENCY,
    BENCH_IPC_PAYLOAD_SWEEP,
    BENCH_IPC_THROUGHPUT,
    BENCH_ZERO_COPY_IPC,
    BENCH_NESTED_IRQ_OVERHEAD,
    BENCH_HEAP_FRAGMENTATION,
    BENCH_MAX_INTERRUPT_DISABLED,
    BENCH_PRIORITY_SCHEDULING_CORRECTNESS,
    BENCH_COMMON_ARENA_FRAGMENTATION,
};
#else
/* Cases 16/17/26 (scheduler overhead, notification, priority conformance) plus
 * 25 (max_interrupt_disabled, TIMER4 probe) for both application cores. */
#define PROTO_CASES "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,24,25,26,27,28]"
static const bench_case s_supported_cases[] = {
    BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF,
    BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP,
    BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY,
    BENCH_TIMER_CALLBACK, BENCH_TRIGGER_TO_ISR, BENCH_PERIODIC_JITTER_LOADED,
    BENCH_SEMAPHORE_LATENCY, BENCH_MUTEX_UNCONTENDED, BENCH_ALLOC_LATENCY,
    BENCH_SLEEP_ACCURACY,
    BENCH_SCHEDULER_OVERHEAD, BENCH_NOTIFICATION_LATENCY,
    BENCH_IPC_PAYLOAD_SWEEP,
    BENCH_IPC_THROUGHPUT,
    BENCH_ZERO_COPY_IPC,
    BENCH_NESTED_IRQ_OVERHEAD,
    BENCH_HEAP_FRAGMENTATION,
    BENCH_MAX_INTERRUPT_DISABLED,
    BENCH_PRIORITY_SCHEDULING_CORRECTNESS,
    BENCH_COMMON_ARENA_FRAGMENTATION,
};
#endif
#define PROTO_N_SUPPORTED_CASES \
    (sizeof(s_supported_cases) / sizeof(s_supported_cases[0]))

/* Requested case list, in protocol order, for `complete` per_case_counts. */
static bench_case s_start_cases[MAX_CASES];
static size_t s_start_n_cases;

/* ---------------------------------------------------------------- TX */

static char s_tx_buf[FRAME_MAX + 2];

static void tx_string(size_t len)
{
    if (len + 1 > sizeof(s_tx_buf)) {
        return;
    }
    s_tx_buf[len] = '\n';
    (void)puts(s_tx_buf);
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

#ifdef CONFIG_THREAD_MONITOR
/* Accumulator for the per-task stack scan; the callback runs for every
 * thread. Zephyr stages all benchmark threads in BSS, so the per-task RAM
 * overhead below is the k_thread object size, not a heap allocation. */
struct bench_stack_scan {
    uint32_t count;
    size_t worst;
    size_t best;
};

static void bench_stack_scan_cb(const struct k_thread *thread, void *user_data)
{
    struct bench_stack_scan *scan = (struct bench_stack_scan *)user_data;
    size_t unused = 0U;

    if (k_thread_stack_space_get(thread, &unused) != 0) {
        return;
    }
    if (scan->count == 0U || unused < scan->worst) {
        scan->worst = unused;
    }
    if (scan->count == 0U || unused > scan->best) {
        scan->best = unused;
    }
    scan->count++;
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

#ifdef CONFIG_THREAD_STACK_INFO
    /* Unused stack bytes, matching FreeRTOS's minimum-free-stack semantics. */
    {
        size_t unused = 0;
        if (k_thread_stack_space_get(k_current_get(), &unused) == 0) {
            stack_free = (unsigned long)unused;
        }
    }
#endif
#ifdef CONFIG_THREAD_MONITOR
    /* Per-task stack aggregate over every live thread, plus the kernel per-task
     * bookkeeping (the statically allocated k_thread object). */
    {
        struct bench_stack_scan scan = {0U, 0U, 0U};
        k_thread_foreach(bench_stack_scan_cb, &scan);
        task_count = (unsigned long)scan.count;
        worst_stack = (unsigned long)scan.worst;
        best_stack = (unsigned long)scan.best;
    }
    per_task_ram = (unsigned long)sizeof(struct k_thread);
#endif
#ifdef CONFIG_SYS_HEAP_RUNTIME_STATS
    /* Zephyr exposes no minimum-ever heap, so report free bytes at run end. */
    {
        struct sys_memory_stats stats;
        if (malloc_runtime_stats_get(&stats) == 0) {
            heap_free = (unsigned long)stats.free_bytes;
        }
    }
    /* Per-allocation overhead: free-byte delta for a 1-byte malloc. */
    {
        struct sys_memory_stats before;
        if (malloc_runtime_stats_get(&before) == 0) {
            void *p = malloc(1);
            if (p != NULL) {
                struct sys_memory_stats after;
                if (malloc_runtime_stats_get(&after) == 0 &&
                    after.free_bytes < before.free_bytes &&
                    (before.free_bytes - after.free_bytes) >= 1) {
                    heap_over =
                        (unsigned long)(before.free_bytes - after.free_bytes - 1);
                }
                free(p);
            }
        }
    }
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
    uint64_t ticks;
    uint32_t index;
    uint16_t variant; /* schema-v2 discriminator; 0 for plain cases */
    uint8_t case_id;  /* protocol ID, not the bench_case enum value */
    uint8_t reserved;
};

/* The C runner passes bench_case enum values; the wire protocol uses protocol
 * IDs. 0-15 coincide; the sparse phase-2 enums must be translated back. */
static uint32_t protocol_id(bench_case c)
{
    switch (c) {
        case BENCH_SCHEDULER_OVERHEAD: return 16;
        case BENCH_NOTIFICATION_LATENCY: return 17;
        case BENCH_IPC_PAYLOAD_SWEEP: return 18;
        case BENCH_IPC_THROUGHPUT: return 19;
        case BENCH_ZERO_COPY_IPC: return 27;
        case BENCH_NESTED_IRQ_OVERHEAD: return 20;
        case BENCH_HEAP_FRAGMENTATION: return 24;
        case BENCH_TICK_OVERHEAD: return 21;
        case BENCH_MAX_INTERRUPT_DISABLED: return 25;
        case BENCH_PRIORITY_SCHEDULING_CORRECTNESS: return 26;
        case BENCH_COMMON_ARENA_FRAGMENTATION: return 28;
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
    s_sink.samples[s_sink.count].case_id = (uint8_t)protocol;
    s_sink.samples[s_sink.count].index = index;
    s_sink.samples[s_sink.count].ticks = ticks;
    s_sink.samples[s_sink.count].variant = (uint16_t)variant;
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

#ifdef BENCH_LANGUAGE_RUST
/* Bridge the Rust application core (firmware/zephyr/rust) to this session's
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
    return sink_record_id(ctx, case_id, index, ticks) == BENCH_OK
               ? 0
               : -1;
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

/* ------------------------------------------------------------- session */

/* Requested cases arrive as protocol IDs; map the sparse phase-2 IDs to their
 * enum values (0-15 already match the enum order). */
static bench_case map_case(unsigned id)
{
    switch (id) {
        case 16: return BENCH_SCHEDULER_OVERHEAD;
        case 17: return BENCH_NOTIFICATION_LATENCY;
        case 18: return BENCH_IPC_PAYLOAD_SWEEP;
        case 19: return BENCH_IPC_THROUGHPUT;
        case 27: return BENCH_ZERO_COPY_IPC;
        case 20: return BENCH_NESTED_IRQ_OVERHEAD;
        case 24: return BENCH_HEAP_FRAGMENTATION;
        case 21: return BENCH_TICK_OVERHEAD;
        case 25: return BENCH_MAX_INTERRUPT_DISABLED;
        case 26: return BENCH_PRIORITY_SCHEDULING_CORRECTNESS;
        case 28: return BENCH_COMMON_ARENA_FRAGMENTATION;
        default: return (bench_case)id;
    }
}

static void run_session(bench_settings settings, const bench_case *cases,
                        size_t n_cases)
{
#ifdef BENCH_LANGUAGE_RUST
    /* Rust application core: native Zephyr queue/timer via FFI shims. */
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
    bench_status status = zephyr_bench_port_init(&port);
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
        /* Partial evidence already flushed between cases by the runner. */
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
static bench_case s_start_cases[MAX_CASES];
static size_t s_start_n_cases;

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
 * the closing quote (an unbounded return would embed the rest of the frame). */
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

int main(void)
{
    bench_port probe;
    (void)zephyr_bench_port_init(&probe);
    g_ticks_per_second = probe.ticks_per_second;

    send_ready(NULL);

    console_getline_init();

    while (1) {
        char *line = console_getline();
        if (line == NULL) {
            continue;
        }

        if (strstr(line, "\"type\":\"hello\"")) {
            const char *challenge = extract_field(line, "\"challenge\"");
            send_ready(challenge);
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
