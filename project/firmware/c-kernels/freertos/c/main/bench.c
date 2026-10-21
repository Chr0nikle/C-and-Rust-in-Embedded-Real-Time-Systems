/* Native benchmark adapter for the ESP32-C6 FreeRTOS bench.
 *
 * Supplies the bench_port (16 MHz SYSTIMER, FreeRTOS queue) and bench_sink
 * (bounded sample buffer) required by rtos_bench.h, and drives bench_run_suite
 * from the protocol v1 `start` frame.
 *
 * Timer: the SYSTIMER unit0 counter runs continuously off the XTAL at 16 MHz.
 * Unlike the 160 MHz CPU cycle counter (CSR 0x7e2), it keeps counting while
 * the CPU is in WFI idle, so every case -- including the sleep cases 3/9/15 --
 * is measured correctly. 16 MHz (62.5 ns/tick) resolves the sub-microsecond
 * cases, and the ticks_per_second reported is 16,000,000, the same base the
 * Zephyr and NuttX C6 ports use, so all three C6 kernels are directly
 * comparable. The 64-bit counter does not wrap within any benchmark duration.
 *
 * Sink: a bounded buffer of (case, index, ticks) samples. record() appends
 * after the timed section and rejects overflow (the spec forbids transmitting
 * inside a timed callback). flush() emits the buffered samples as `measurement`
 * frames and clears the buffer; bench_run_case calls flush() once per case, so
 * samples are flushed between cases as required.
 *
 * The frame builders and the start-validation/run contract mirror the verified
 * STM32F4 adapter (firmware/freertos/c/stm32f4/src/bench.c) so that C on the
 * C6 and C on the F4 produce the same wire contract and the same semantics;
 * only the port (timer source, queue primitive) differs.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/timers.h"
#include "driver/gptimer.h"
#include "esp_intr_alloc.h"
#include "esp_attr.h"
#include "esp_timer.h"
#include "rtos_bench.h"

/* C6 SYSTIMER unit0 timebase: 16 MHz, continuous (idle-safe). */
#define BENCH_HZ        16000000ULL
#define BENCH_FRAME_MAX 1024

/* SYSTIMER unit0 counter read (offsets from esp32c6 systimer_reg.h). The
 * UPDATE bit snapshots the 64-bit count; VALID gates the read. */
#define C6_SYSTIMER_BASE           0x6000A000UL
#define C6_SYSTIMER_UNIT0_OP       (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x04UL))
#define C6_SYSTIMER_UNIT0_HI       (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x40UL))
#define C6_SYSTIMER_UNIT0_LO       (*(volatile uint32_t *)(C6_SYSTIMER_BASE + 0x44UL))
#define C6_SYSTIMER_UNIT0_UPDATE   (1u << 30)
#define C6_SYSTIMER_UNIT0_VALID    (1u << 29)

static inline uint32_t c6_systimer_count(void)
{
    C6_SYSTIMER_UNIT0_OP = C6_SYSTIMER_UNIT0_UPDATE;
    while ((C6_SYSTIMER_UNIT0_OP & C6_SYSTIMER_UNIT0_VALID) == 0u) {
    }
    return C6_SYSTIMER_UNIT0_LO;
}

/* Software-asserted interrupt source register for the ISR cases. */
#define C6_FROM_CPU2_REG (*(volatile uint32_t *)0x600C5098UL)

/* Maximum samples buffered before a flush. One case holds `repetitions`
 * samples; the buffer must hold a full case. Sized for the >=1000-sample
 * campaign (2048 x 16 B = 32 KiB of the C6's 512 KiB SRAM). Requests above
 * this are rejected. */
#define BENCH_MAX_SAMPLES 2048

/* Peer-task stack size. Vanilla FreeRTOS sizes xTaskCreate stacks in words,
 * so 256 is ~1 KiB; ESP-IDF sizes the same argument in bytes, where 256
 * overflows on the peer's first blocking call. Match the RISC-V Rust
 * adapter (PEER_STACK = 4096). See .local/sdk/esp-idf .../task.h. */
#define BENCH_PEER_STACK 4096

/* Max run_id length (protocol.md:20). */
#define BENCH_RUN_ID_MAX 64

/* Case-ID -> bench_case enum map. The protocol uses ordered integer IDs:
 *   0 = timer_overhead, 1 = queue_roundtrip, 2 = task_handoff,
 *   3 = periodic_jitter, 4 = isr_to_task (matching the ready.cases list).
 * Only the C runner path consumes it, so it is omitted for Rust builds where
 * IDF compiles with -Werror=unused-const-variable. */
#ifndef BENCH_LANGUAGE_RUST
static const bench_case s_case_map[] = {
    [0] = BENCH_TIMER_OVERHEAD,
    [1] = BENCH_QUEUE_ROUNDTRIP,
    [2] = BENCH_TASK_HANDOFF,
    [3] = BENCH_PERIODIC_JITTER,
    [4] = BENCH_ISR_TO_TASK,
    [5] = BENCH_YIELD_ROUNDTRIP,
    [6] = BENCH_PREEMPT_LATENCY,
    [7] = BENCH_IPC_PINGPONG,
    [8] = BENCH_MUTEX_PRIORITY,
    [9] = BENCH_TIMER_CALLBACK,
    [10] = BENCH_TRIGGER_TO_ISR,
    [11] = BENCH_PERIODIC_JITTER_LOADED,
    [12] = BENCH_SEMAPHORE_LATENCY,
    [13] = BENCH_MUTEX_UNCONTENDED,
    [14] = BENCH_ALLOC_LATENCY,
    [15] = BENCH_SLEEP_ACCURACY,
    [16] = BENCH_SCHEDULER_OVERHEAD,
    [17] = BENCH_NOTIFICATION_LATENCY,
    [18] = BENCH_IPC_PAYLOAD_SWEEP,
    [19] = BENCH_IPC_THROUGHPUT,
    [20] = BENCH_NESTED_IRQ_OVERHEAD,
    [25] = BENCH_MAX_INTERRUPT_DISABLED,
    [26] = BENCH_PRIORITY_SCHEDULING_CORRECTNESS,
    [27] = BENCH_ZERO_COPY_IPC,
    [28] = BENCH_COMMON_ARENA_FRAGMENTATION,
};
#endif
#define BENCH_KNOWN_CASES 29

/* ------------------------------------------------------------------ TX */

/* Exposed by main.c: transmit a JSON frame plus trailing newline. */
extern void bench_tx(const char *buf, size_t len);

static char s_frame[BENCH_FRAME_MAX + 2];

/* Append a decimal unsigned value into the frame builder. Returns the new
 * length, or BENCH_FRAME_MAX + 1 on overflow. */
static size_t frame_append_u64(char *p, size_t cap, size_t len, uint64_t v)
{
    char tmp[20];
    int n = 0;
    if (v == 0) {
        tmp[n++] = '0';
    } else {
        char rev[20];
        int r = 0;
        while (v > 0) {
            rev[r++] = (char)('0' + (v % 10));
            v /= 10;
        }
        while (r > 0) {
            tmp[n++] = rev[--r];
        }
    }
    if (len + (size_t)n + 1 > cap) {
        return cap + 1;
    }
    memcpy(p + len, tmp, (size_t)n);
    return len + (size_t)n;
}

/* Append a literal. Returns the new length, or BENCH_FRAME_MAX + 1 on
 * overflow. */
static size_t frame_append_str(char *p, size_t cap, size_t len,
                               const char *s, size_t slen)
{
    if (len + slen + 1 > cap) {
        return cap + 1;
    }
    memcpy(p + len, s, slen);
    return len + slen;
}

static size_t frame_append_lit(char *p, size_t cap, size_t len, const char *lit)
{
    return frame_append_str(p, cap, len, lit, strlen(lit));
}

/* Emit `started`: {\"protocol\":1,\"type\":\"started\",\"run_id\":..,\"build_id\":..} */
static int emit_started(const char *run_id, const char *build_id)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;

    len = frame_append_lit(p, cap, len, "{\"protocol\":1,\"type\":\"started\",\"run_id\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, run_id, strlen(run_id));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\",\"build_id\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, build_id, strlen(build_id));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\"}");
    if (len > cap) return -1;
    p[len] = '\n';
    bench_tx(p, len + 1);
    return 0;
}

/* Emit one `measurement` frame for a buffered sample. `variant` is the schema-v2
 * discriminator (payload bytes for case 18); 0 for plain cases. */
static int emit_measurement(const char *run_id, int case_id,
                            uint32_t index, uint64_t ticks, uint64_t hz,
                            uint32_t variant)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;

    len = frame_append_lit(p, cap, len, "{\"protocol\":1,\"type\":\"measurement\",\"run_id\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, run_id, strlen(run_id));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\",\"case\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, (uint64_t)case_id);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"index\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, (uint64_t)index);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"ticks\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, ticks);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"ticks_per_second\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, hz);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"variant\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, (uint64_t)variant);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "}");
    if (len > cap) return -1;
    p[len] = '\n';
    bench_tx(p, len + 1);
    return 0;
}

/* Emit `complete`: {\"protocol\":1,\"type\":\"complete\",\"run_id\":..,
 *                   \"per_case_counts\":[..],\"status\":\"ok\",
 *                   \"stack_high_water_bytes\":..,\"heap_min_free_bytes\":..}
 * ESP-IDF supplies both min-ever-free APIs; NULL asks for this task's own
 * minimum free stack, reported in bytes (the kernel returns words). */
/* Per-allocation heap overhead (heap header + size rounding), measured as
 * (free_before - free_after) - requested for a 1-byte request. */
static uint64_t heap_overhead_bytes(void)
{
    size_t before = xPortGetFreeHeapSize();
    void *p = pvPortMalloc(1);
    if (p == NULL) {
        return 0U;
    }
    size_t after = xPortGetFreeHeapSize();
    vPortFree(p);
    if (after >= before || (before - after) < 1U) {
        return 0U;
    }
    return (uint64_t)(before - after - 1U);
}

/* Entry for the throwaway task used to measure per-task RAM overhead; it only
 * blocks on the delay list, so it never runs user code. */
static void ram_probe_task(void *arg)
{
    (void)arg;
    for (;;) {
        vTaskDelay(portMAX_DELAY);
    }
}

/* Kernel RAM overhead for one added task beyond its stack (TCB + heap block
 * metadata), bytes. The 768 B stack request is a whole number of
 * portBYTE_ALIGNMENT units, so the allocator rounds TCB and stack identically. */
static uint64_t per_task_ram_overhead_bytes(void)
{
    const configSTACK_DEPTH_TYPE stack_words = 192; /* 768 B */
    const size_t stack_bytes = (size_t)stack_words * sizeof(StackType_t);
    size_t before = xPortGetFreeHeapSize();
    TaskHandle_t handle = NULL;

    if (xTaskCreate(ram_probe_task, "memprobe", stack_words, NULL,
                    tskIDLE_PRIORITY + 1, &handle) != pdPASS) {
        return 0U;
    }
    size_t after = xPortGetFreeHeapSize();
    vTaskDelete(handle);
    vTaskDelay(2); /* let the idle task reap and free the probe block */
    if (before < after || (before - after) < stack_bytes) {
        return 0U;
    }
    return (uint64_t)(before - after - stack_bytes);
}

/* Aggregate per-task stack headroom (minimum-ever-free bytes) over the live
 * tasks; caller-owned scratch keeps the array off BSS. */
static void task_stack_aggregate(TaskStatus_t *statuses, UBaseType_t cap,
                                 uint32_t *task_count, uint64_t *worst,
                                 uint64_t *best)
{
    UBaseType_t n = uxTaskGetSystemState(statuses, cap, NULL);
    uint32_t count = 0;
    uint64_t w = 0;
    uint64_t b = 0;

    for (UBaseType_t i = 0; i < n; i++) {
        uint64_t free_bytes = (uint64_t)statuses[i].usStackHighWaterMark *
                              (uint64_t)sizeof(StackType_t);
        if (count == 0 || free_bytes < w) {
            w = free_bytes;
        }
        if (count == 0 || free_bytes > b) {
            b = free_bytes;
        }
        count++;
    }
    *task_count = count;
    *worst = w;
    *best = b;
}

static int emit_complete(const char *run_id, const uint32_t *counts, size_t n)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;
    int first = 1;

    len = frame_append_lit(p, cap, len, "{\"protocol\":1,\"type\":\"complete\",\"run_id\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, run_id, strlen(run_id));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\",\"per_case_counts\":[");
    if (len > cap) return -1;
    for (size_t i = 0; i < n; i++) {
        if (!first) {
    len = frame_append_lit(p, cap, len, ",");
            if (len > cap) return -1;
        }
        first = 0;
        len = frame_append_u64(p, cap, len, (uint64_t)counts[i]);
        if (len > cap) return -1;
    }
    const uint64_t heap_min_free = (uint64_t)xPortGetMinimumEverFreeHeapSize();
    const uint64_t heap_free = (uint64_t)xPortGetFreeHeapSize();
    const uint64_t heap_over = heap_overhead_bytes();
    const uint64_t per_task_ram = per_task_ram_overhead_bytes();
    TaskStatus_t statuses[16];
    uint32_t task_n = 0;
    uint64_t worst_stack = 0;
    uint64_t best_stack = 0;
    task_stack_aggregate(statuses, (UBaseType_t)16, &task_n,
                         &worst_stack, &best_stack);
    const uint64_t stack_free =
        (uint64_t)uxTaskGetStackHighWaterMark(NULL) * (uint64_t)sizeof(StackType_t);

    len = frame_append_lit(p, cap, len, "],\"status\":\"ok\",\"stack_high_water_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, stack_free);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"heap_min_free_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, heap_min_free);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"heap_free_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, heap_free);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"heap_overhead_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, heap_over);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"per_task_ram_overhead_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, per_task_ram);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"task_count\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, (uint64_t)task_n);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"worst_stack_headroom_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, worst_stack);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, ",\"best_stack_headroom_bytes\":");
    if (len > cap) return -1;
    len = frame_append_u64(p, cap, len, best_stack);
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "}");
    if (len > cap) return -1;
    p[len] = '\n';
    bench_tx(p, len + 1);
    return 0;
}

/* Emit `error`: {\"protocol\":1,\"type\":\"error\",\"run_id\":..,\"code\":..,
 *                \"message\":..} */
static int emit_error(const char *run_id, const char *code, const char *msg)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;

    len = frame_append_lit(p, cap, len, "{\"protocol\":1,\"type\":\"error\",\"run_id\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, run_id, strlen(run_id));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\",\"code\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, code, strlen(code));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\",\"message\":\"");
    if (len > cap) return -1;
    len = frame_append_str(p, cap, len, msg, strlen(msg));
    if (len > cap) return -1;
    len = frame_append_lit(p, cap, len, "\"}");
    if (len > cap) return -1;
    p[len] = '\n';
    bench_tx(p, len + 1);
    return 0;
}

/* ------------------------------------------------------------- timer port */

static bench_status port_now_ticks(void *ctx, uint64_t *out)
{
    (void)ctx;
    if (out == NULL) {
        return BENCH_INVALID;
    }
    *out = c6_systimer_count();
    return BENCH_OK;
}

/* ------------------------------------------------------------ FreeRTOS queue */

/* The bench_port queue is capacity-1, u32 payload, initially empty. It is
 * created outside the timed section (bench_run_case calls queue_create before
 * the warm-up/repetition loops) and destroyed after. */
static QueueHandle_t s_queue;

static bench_status port_queue_create(void *ctx)
{
    (void)ctx;
    if (s_queue != NULL) {
        return BENCH_OK; /* already created (idempotent) */
    }
    s_queue = xQueueCreate(1, sizeof(uint32_t));
    if (s_queue == NULL) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_queue_send(void *ctx, uint32_t v)
{
    (void)ctx;
    if (s_queue == NULL) {
        return BENCH_PORT;
    }
    if (xQueueSend(s_queue, &v, 0) != pdPASS) {
        return BENCH_PORT; /* nonblocking send failed (queue full) */
    }
    return BENCH_OK;
}

static bench_status port_queue_receive(void *ctx, uint32_t *out)
{
    (void)ctx;
    if (s_queue == NULL || out == NULL) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_queue, out, 0) != pdPASS) {
        return BENCH_PORT; /* nonblocking receive failed (queue empty) */
    }
    return BENCH_OK;
}

static bench_status port_queue_destroy(void *ctx)
{
    (void)ctx;
    if (s_queue != NULL) {
        vQueueDelete(s_queue);
        s_queue = NULL;
    }
    return BENCH_OK;
}

/* ------------------------------------------------------------ task handoff */

/* The peer blocks on s_handoff_signal; the benchmark task gives it and waits on
 * s_handoff_done. The peer timestamps its own wake with esp_timer. */
static SemaphoreHandle_t s_handoff_signal;
static SemaphoreHandle_t s_handoff_done;
static TaskHandle_t      s_handoff_peer;
static volatile uint64_t s_handoff_wake;

static void handoff_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (xSemaphoreTake(s_handoff_signal, portMAX_DELAY) != pdPASS) {
            continue;
        }
        s_handoff_wake = c6_systimer_count();
        (void)xSemaphoreGive(s_handoff_done);
    }
}

static bench_status port_handoff_create(void *ctx)
{
    (void)ctx;
    s_handoff_signal = xSemaphoreCreateBinary();
    s_handoff_done   = xSemaphoreCreateBinary();
    if (s_handoff_signal == NULL || s_handoff_done == NULL) {
        return BENCH_PORT;
    }
    /* One priority above the benchmark task so the give preempts immediately. */
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(handoff_peer_entry, "bench_peer", 2048, NULL, priority,
                    &s_handoff_peer) != pdPASS) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_handoff_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_handoff_signal == NULL || s_handoff_done == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    if (xSemaphoreGive(s_handoff_signal) != pdPASS) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_handoff_done, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    *ticks = s_handoff_wake;
    return BENCH_OK;
}

static bench_status port_handoff_destroy(void *ctx)
{
    (void)ctx;
    if (s_handoff_peer != NULL) {
        vTaskDelete(s_handoff_peer);
        s_handoff_peer = NULL;
    }
    if (s_handoff_signal != NULL) {
        vSemaphoreDelete(s_handoff_signal);
        s_handoff_signal = NULL;
    }
    if (s_handoff_done != NULL) {
        vSemaphoreDelete(s_handoff_done);
        s_handoff_done = NULL;
    }
    return BENCH_OK;
}

/* ------------------------------------------------------- periodic jitter */

/* One millisecond at the SYSTIMER 16 MHz base. */
#define JITTER_PERIOD_TICKS (BENCH_HZ / 1000ULL)

static TickType_t s_jitter_last;
static uint64_t   s_jitter_prev;

static bench_status port_jitter_create(void *ctx)
{
    (void)ctx;
    s_jitter_last = xTaskGetTickCount();
    /* Align to the tick grid so every measured interval is one full period. */
    (void)xTaskDelayUntil(&s_jitter_last, pdMS_TO_TICKS(1));
    s_jitter_prev = c6_systimer_count();
    return BENCH_OK;
}

static bench_status port_jitter_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xTaskDelayUntil(&s_jitter_last, pdMS_TO_TICKS(1));
    uint64_t now = c6_systimer_count();
    uint64_t interval = (now >= s_jitter_prev) ? (now - s_jitter_prev) : 0;
    *ticks = (interval >= JITTER_PERIOD_TICKS)
                 ? (interval - JITTER_PERIOD_TICKS)
                 : (JITTER_PERIOD_TICKS - interval);
    s_jitter_prev = now;
    return BENCH_OK;
}

static bench_status port_jitter_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

/* ------------------------------------------------------------ isr_to_task */

/* A GPTimer alarm fires in interrupt context; the handler timestamps the ISR
 * entry and gives the semaphore from the ISR. measure() blocks on it and
 * reports the ISR-entry to awakened-task delta. */
static gptimer_handle_t  s_isr_timer;
static SemaphoreHandle_t s_isr_sem;
static volatile uint64_t s_isr_entry;

static bool IRAM_ATTR isr_on_alarm(gptimer_handle_t timer,
                                   const gptimer_alarm_event_data_t *edata,
                                   void *user_ctx)
{
    (void)timer;
    (void)edata;
    (void)user_ctx;
    BaseType_t woken = pdFALSE;
    s_isr_entry = c6_systimer_count();
    (void)xSemaphoreGiveFromISR(s_isr_sem, &woken);
    return woken == pdTRUE;
}

static bench_status port_isr_create(void *ctx)
{
    (void)ctx;
    if (s_isr_sem == NULL) {
        s_isr_sem = xSemaphoreCreateBinary();
        if (s_isr_sem == NULL) {
            return BENCH_PORT;
        }
    }
    gptimer_config_t config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000, /* 1 MHz -> 1 us per tick */
    };
    if (gptimer_new_timer(&config, &s_isr_timer) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_event_callbacks_t cbs = {
        .on_alarm = isr_on_alarm,
    };
    if (gptimer_register_event_callbacks(s_isr_timer, &cbs, NULL) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_alarm_config_t alarm = {
        .alarm_count = 20, /* ~20 us after start */
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    if (gptimer_set_alarm_action(s_isr_timer, &alarm) != ESP_OK) {
        return BENCH_PORT;
    }
    if (gptimer_enable(s_isr_timer) != ESP_OK) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_isr_timer == NULL || s_isr_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    if (gptimer_set_raw_count(s_isr_timer, 0) != ESP_OK) {
        return BENCH_PORT;
    }
    if (gptimer_start(s_isr_timer) != ESP_OK) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_isr_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    (void)gptimer_stop(s_isr_timer);
    uint64_t wake = c6_systimer_count();
    *ticks = (wake >= s_isr_entry) ? (wake - s_isr_entry) : 0;
    return BENCH_OK;
}

static bench_status port_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_isr_timer != NULL) {
        (void)gptimer_stop(s_isr_timer);
        (void)gptimer_disable(s_isr_timer);
        (void)gptimer_del_timer(s_isr_timer);
        s_isr_timer = NULL;
    }
    if (s_isr_sem != NULL) {
        vSemaphoreDelete(s_isr_sem);
        s_isr_sem = NULL;
    }
    return BENCH_OK;
}

/* ------------------------------------------- trigger to ISR entry (10) */

/* Software-asserted minimal interrupt. The task masks interrupts, reads the
 * 16 MHz SYSTIMER counter, writes bit 0 of INTPRI_CPU_INTR_FROM_CPU_2_REG to
 * assert interrupt-matrix source ETS_FROM_CPU_INTR2, and unmasks, so the
 * exception is taken at the unmask and the handler's first read is the
 * ISR-entry timestamp. Value = trigger -> entry, no nominal subtraction. */

static intr_handle_t     s_trig_handle;
static SemaphoreHandle_t s_trig_sem;
static volatile uint64_t s_trig_entry;
static int               s_trig_active;

static void IRAM_ATTR trig_isr(void *arg)
{
    (void)arg;
    C6_FROM_CPU2_REG = 0; /* deassert the level source before anything else */
    s_trig_entry = c6_systimer_count();
    BaseType_t woken = pdFALSE;
    (void)xSemaphoreGiveFromISR(s_trig_sem, &woken);
    portYIELD_FROM_ISR(woken);
}

static bench_status port_trigger_isr_create(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        return BENCH_OK;
    }
    if (s_trig_sem == NULL) {
        s_trig_sem = xSemaphoreCreateBinary();
        if (s_trig_sem == NULL) {
            return BENCH_PORT;
        }
    }
    C6_FROM_CPU2_REG = 0;
    /* ETS_FROM_CPU_INTR2_SOURCE (24); LEVEL1 keeps it FreeRTOS-ISR-safe. */
    if (esp_intr_alloc(24, ESP_INTR_FLAG_LEVEL1, trig_isr, NULL,
                       &s_trig_handle) != ESP_OK) {
        return BENCH_PORT;
    }
    s_trig_active = 1;
    return BENCH_OK;
}

static bench_status port_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_trig_active || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xSemaphoreTake(s_trig_sem, 0); /* drain any stale signal */
    portDISABLE_INTERRUPTS();
    uint32_t t0 = c6_systimer_count();
    C6_FROM_CPU2_REG = 1u;
    portENABLE_INTERRUPTS(); /* unmask: the exception is taken here */
    if (xSemaphoreTake(s_trig_sem, pdMS_TO_TICKS(50)) != pdPASS) {
        C6_FROM_CPU2_REG = 0;
        return BENCH_PORT;
    }
    *ticks = (uint64_t)(uint32_t)(s_trig_entry - t0); /* 32-bit wrap-safe */
    return BENCH_OK;
}

static bench_status port_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    if (s_trig_active) {
        C6_FROM_CPU2_REG = 0;
        esp_intr_free(s_trig_handle);
        s_trig_active = 0;
    }
    return BENCH_OK;
}

/* ------------------------------------------- nested interrupt (20) */

/* Two interrupt-matrix FROM_CPU software sources at different levels. FROM_CPU0
 * and FROM_CPU1 are reserved by FreeRTOS on the C6, so the low line reuses
 * FROM_CPU2 (source 24, LEVEL1, shared with case 10 and freed on its destroy)
 * and the higher line uses FROM_CPU3 (source 25, LEVEL3). The low ISR runs a
 * fixed-work spin and pends the higher source ten times; measure() =
 * min(nested) - min(baseline) over three runs each. NESTED_CONTROL=1 disables
 * the higher source and must report 0. Timestamps use the 16 MHz SYSTIMER.
 * C6_FROM_CPU2_REG is defined with case 10. */
#define C6_NESTED_HIGH_REG (*(volatile uint32_t *)0x600C509CUL)
#define NESTED_SPIN  1000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;
static intr_handle_t s_nested_low_handle;
static intr_handle_t s_nested_high_handle;
static int s_nested_active;

static void IRAM_ATTR nested_low_isr(void *arg)
{
    (void)arg;
    C6_FROM_CPU2_REG = 0; /* deassert the level source first */
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            C6_NESTED_HIGH_REG = 1u; /* higher level preempts here */
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
}

static void IRAM_ATTR nested_high_isr(void *arg)
{
    (void)arg;
    C6_NESTED_HIGH_REG = 0;
}

static bench_status port_nested_create(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        return BENCH_OK;
    }
    C6_FROM_CPU2_REG = 0;
    C6_NESTED_HIGH_REG = 0;
    if (esp_intr_alloc(24, ESP_INTR_FLAG_LEVEL1, nested_low_isr, NULL,
                       &s_nested_low_handle) != ESP_OK) {
        return BENCH_PORT;
    }
    if (esp_intr_alloc(25, ESP_INTR_FLAG_LEVEL3, nested_high_isr, NULL,
                       &s_nested_high_handle) != ESP_OK) {
        esp_intr_free(s_nested_low_handle);
        return BENCH_PORT;
    }
    s_nested_active = 1;
    return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_pend_h = pend_h;
    const uint32_t t0 = c6_systimer_count();
    C6_FROM_CPU2_REG = 1u;
    while (s_nested_low_done == 0) {
    }
    return c6_systimer_count() - t0;
}

static bench_status port_nested_measure(void *ctx, uint64_t *ticks)
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

static bench_status port_nested_destroy(void *ctx)
{
    (void)ctx;
    if (s_nested_active) {
        C6_FROM_CPU2_REG = 0;
        C6_NESTED_HIGH_REG = 0;
        esp_intr_free(s_nested_low_handle);
        esp_intr_free(s_nested_high_handle);
        s_nested_active = 0;
    }
    return BENCH_OK;
}
bench_status bench_nested_create(void) { return port_nested_create(NULL); }
bench_status bench_nested_measure(uint64_t *ticks) { return port_nested_measure(NULL, ticks); }
bench_status bench_nested_destroy(void) { return port_nested_destroy(NULL); }

/* ------------------------------------ periodic jitter under load (11) */

/* Case 11 on ESP32-C6 FreeRTOS. The release GPTimer auto-reloads at an exact
 * 750 us (resolution 1 MHz, alarm_count 750) and its alarm callback gives the
 * semaphore; a second GPTimer fires every 137 us (alarm_count 137) and runs a
 * calibrated busy-loop body at a higher interrupt priority, making no RTOS
 * calls. The activation error is the release timer's own raw counter at task
 * wake: auto-reload resets it to 0 at each alarm, so the count is the
 * release-to-wake latency in 1 us ticks, the same base as esp_timer (BENCH_HZ).
 * Measuring in the timer's own domain avoids any CPU-cycle-counter idle stop and
 * any GPTimer/SYSTIMER cross-domain drift. */
#define PJIT_P_TICKS    750U /* 750 us */
#define PJIT_LOAD_TICKS 137U /* 137 us */
#define PJIT_LOAD_TARGET 700U /* ~32% duty over 137 us (700 ticks at 16 MHz) */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static gptimer_handle_t  s_pjit_rel;
static gptimer_handle_t  s_pjit_load;
static SemaphoreHandle_t s_pjit_sem;
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
#if PJIT_LOAD_ENABLE
static volatile uint32_t s_pjit_iters;
#endif
static int s_pjit_active;
static portMUX_TYPE s_pjit_mux = portMUX_INITIALIZER_UNLOCKED;

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

static bool IRAM_ATTR pjit_release_on_alarm(gptimer_handle_t timer,
                                            const gptimer_alarm_event_data_t *edata,
                                            void *user_ctx)
{
    (void)timer;
    (void)edata;
    (void)user_ctx;
    BaseType_t woken = pdFALSE;
    s_pjit_releases++;
    (void)xSemaphoreGiveFromISR(s_pjit_sem, &woken);
    return woken == pdTRUE;
}

#if PJIT_LOAD_ENABLE
static bool IRAM_ATTR pjit_load_on_alarm(gptimer_handle_t timer,
                                         const gptimer_alarm_event_data_t *edata,
                                         void *user_ctx)
{
    (void)timer;
    (void)edata;
    (void)user_ctx;
    pjit_load_body(s_pjit_iters);
    return false;
}
#endif

static bench_status pjit_new_timer(gptimer_handle_t *out, int intr_priority,
                                   gptimer_alarm_cb_t cb, uint64_t alarm_count)
{
    gptimer_config_t config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000, /* 1 MHz -> 1 us per tick */
        .intr_priority = intr_priority,
    };
    if (gptimer_new_timer(&config, out) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_event_callbacks_t cbs = { .on_alarm = cb };
    if (gptimer_register_event_callbacks(*out, &cbs, NULL) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_alarm_config_t alarm = {
        .alarm_count = alarm_count,
        .reload_count = 0,
        .flags.auto_reload_on_alarm = true,
    };
    if (gptimer_set_alarm_action(*out, &alarm) != ESP_OK) {
        return BENCH_PORT;
    }
    if (gptimer_enable(*out) != ESP_OK) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        return BENCH_OK;
    }
    if (s_pjit_sem == NULL) {
        s_pjit_sem = xSemaphoreCreateBinary();
        if (s_pjit_sem == NULL) {
            return BENCH_PORT;
        }
    }
    s_pjit_releases = 0;
    s_pjit_n = 0;

#if PJIT_LOAD_ENABLE
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        portENTER_CRITICAL(&s_pjit_mux);
        uint64_t a = c6_systimer_count();
        pjit_load_body(s_pjit_iters);
        uint64_t d = c6_systimer_count() - a;
        portEXIT_CRITICAL(&s_pjit_mux);
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

    /* Release: driver-chosen (syscall-safe) priority so it may give the
     * semaphore. Load: priority 1 (higher, no RTOS calls). */
    if (pjit_new_timer(&s_pjit_rel, 0, pjit_release_on_alarm,
                       PJIT_P_TICKS) != BENCH_OK) {
        return BENCH_PORT;
    }
#if PJIT_LOAD_ENABLE
    if (pjit_new_timer(&s_pjit_load, 1, pjit_load_on_alarm,
                       PJIT_LOAD_TICKS) != BENCH_OK) {
        return BENCH_PORT;
    }
#endif
    if (gptimer_start(s_pjit_rel) != ESP_OK) {
        return BENCH_PORT;
    }
#if PJIT_LOAD_ENABLE
    if (gptimer_start(s_pjit_load) != ESP_OK) {
        return BENCH_PORT;
    }
#endif
    s_pjit_active = 1;
    return BENCH_OK;
}

static bench_status port_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (!s_pjit_active || ticks == NULL) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_pjit_sem, portMAX_DELAY) != pdPASS) {
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
    uint64_t count = 0;
    if (gptimer_get_raw_count(s_pjit_rel, &count) != ESP_OK) {
        return BENCH_PORT;
    }
    if (count >= PJIT_P_TICKS) {
        return BENCH_CORRUPT;
    }
    /* The GPTimer counts at 1 MHz (microseconds); rescale to the 16 MHz
     * SYSTIMER base so case 11 reports the same unit as every other case. */
    *ticks = count * (BENCH_HZ / 1000000ULL);
    return BENCH_OK;
}

static bench_status port_pjit_destroy(void *ctx)
{
    (void)ctx;
    if (s_pjit_active) {
        if (s_pjit_rel != NULL) {
            (void)gptimer_stop(s_pjit_rel);
            (void)gptimer_disable(s_pjit_rel);
            (void)gptimer_del_timer(s_pjit_rel);
            s_pjit_rel = NULL;
        }
#if PJIT_LOAD_ENABLE
        if (s_pjit_load != NULL) {
            (void)gptimer_stop(s_pjit_load);
            (void)gptimer_disable(s_pjit_load);
            (void)gptimer_del_timer(s_pjit_load);
            s_pjit_load = NULL;
        }
#endif
        s_pjit_active = 0;
    }
    return BENCH_OK;
}

/* ---------------------------------------------- yield / context switch (5) */

/* An equal-priority peer loops on taskYIELD(); the benchmark yields and
 * confirms the peer ran, timing one full round trip. Stacks are in bytes on
 * ESP-IDF FreeRTOS. */
static TaskHandle_t      s_yield_peer;
static volatile uint32_t s_yield_peer_runs;
static volatile uint32_t s_yield_peer_started;

static void yield_peer_entry(void *arg)
{
    (void)arg;
    s_yield_peer_started = 1;
    for (;;) {
        s_yield_peer_runs++;
        taskYIELD();
    }
}

/* Task-count sweep (case 5): extra runnable peers created/deleted between
 * samples. They sit at the session task's priority and yield, so the yield->
 * resume round trip has to rotate past them. Count includes the original yield
 * peer. Stacks are in bytes on ESP-IDF FreeRTOS. */
#define YIELD_MAX_EXTRA 32U
static TaskHandle_t s_yield_extra[YIELD_MAX_EXTRA];
static uint32_t     s_yield_extra_count;

static void yield_extra_entry(void *arg)
{
    (void)arg;
    for (;;) {
        taskYIELD();
    }
}

static bench_status port_yield_create(void *ctx)
{
    (void)ctx;
    s_yield_peer = NULL;
    s_yield_peer_runs = 0;
    s_yield_peer_started = 0;
    UBaseType_t priority = uxTaskPriorityGet(NULL);
    if (xTaskCreate(yield_peer_entry, "bench_yield", 2048, NULL, priority,
                    &s_yield_peer) != pdPASS) {
        return BENCH_PORT;
    }
    /* The peer must be running before timing, else the first yield cannot
     * switch and the sample would be rejected. */
    for (int i = 0; i < 1000 && s_yield_peer_started == 0; i++) {
        taskYIELD();
    }
    if (s_yield_peer_started == 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_yield_now(void *ctx)
{
    (void)ctx;
    if (s_yield_peer == NULL) {
        return BENCH_PORT;
    }
    uint32_t before = s_yield_peer_runs;
    /* One yield normally switches to the equal-priority peer. A tick landing
     * inside the window can rotate the ready list first, so retry a bounded
     * number of times; the check only guarantees the peer actually ran. */
    for (int i = 0; i < 64; i++) {
        taskYIELD();
        if (s_yield_peer_runs != before) {
            return BENCH_OK;
        }
    }
    return BENCH_CORRUPT;
}

static bench_status port_yield_destroy(void *ctx)
{
    (void)ctx;
    while (s_yield_extra_count > 0U) {
        vTaskDelete(s_yield_extra[--s_yield_extra_count]);
        s_yield_extra[s_yield_extra_count] = NULL;
    }
    if (s_yield_peer != NULL) {
        vTaskDelete(s_yield_peer);
        s_yield_peer = NULL;
    }
    return BENCH_OK;
}

static bench_status port_peers_set(void *ctx, uint32_t count)
{
    (void)ctx;
    uint32_t want = (count > 0U) ? (count - 1U) : 0U;
    if (want > YIELD_MAX_EXTRA) {
        return BENCH_PORT;
    }
    while (s_yield_extra_count > want) {
        vTaskDelete(s_yield_extra[--s_yield_extra_count]);
        s_yield_extra[s_yield_extra_count] = NULL;
    }
    UBaseType_t prio = uxTaskPriorityGet(NULL); /* same priority as the session */
    while (s_yield_extra_count < want) {
        TaskHandle_t handle = NULL;
        if (xTaskCreate(yield_extra_entry, "bench_peerx", 2048, NULL, prio,
                        &handle) != pdPASS) {
            return BENCH_PORT;
        }
        s_yield_extra[s_yield_extra_count++] = handle;
    }
    return BENCH_OK;
}

/* --------------------------------------------------- preempt latency (6) */

/* H blocks on s_preempt_sem one priority above the benchmark task (L); L gives
 * it, H is released immediately, timestamps itself and blocks again so L
 * resumes and returns the delta. */
static SemaphoreHandle_t s_preempt_sem;
static TaskHandle_t      s_preempt_task;
static volatile uint64_t s_preempt_wake;
static volatile uint32_t s_preempt_seq;
static volatile uint32_t s_preempt_ready;

static void preempt_high_entry(void *arg)
{
    (void)arg;
    s_preempt_ready = 1;
    for (;;) {
        if (xSemaphoreTake(s_preempt_sem, portMAX_DELAY) != pdPASS) {
            continue;
        }
        s_preempt_wake = c6_systimer_count();
        s_preempt_seq++;
    }
}

static bench_status port_preempt_create(void *ctx)
{
    (void)ctx;
    s_preempt_task = NULL;
    s_preempt_sem = NULL;
    s_preempt_wake = 0;
    s_preempt_seq = 0;
    s_preempt_ready = 0;
    s_preempt_sem = xSemaphoreCreateBinary();
    if (s_preempt_sem == NULL) {
        return BENCH_PORT;
    }
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(preempt_high_entry, "bench_preempt", 2048, NULL, priority,
                    &s_preempt_task) != pdPASS) {
        return BENCH_PORT;
    }
    for (int i = 0; i < 1000 && s_preempt_ready == 0; i++) {
        taskYIELD();
    }
    if (s_preempt_ready == 0) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_preempt_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_preempt_sem == NULL || s_preempt_task == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint32_t seq = s_preempt_seq;
    uint32_t t0 = c6_systimer_count();
    if (xSemaphoreGive(s_preempt_sem) != pdPASS) {
        return BENCH_PORT;
    }
    if (s_preempt_seq == seq) {
        return BENCH_CORRUPT; /* no preemption: non-preemptive or equal priority */
    }
    uint64_t wake = s_preempt_wake;
    *ticks = (wake >= t0) ? (wake - t0) : 0;
    return BENCH_OK;
}

static bench_status port_preempt_destroy(void *ctx)
{
    (void)ctx;
    if (s_preempt_task != NULL) {
        vTaskDelete(s_preempt_task);
        s_preempt_task = NULL;
    }
    if (s_preempt_sem != NULL) {
        vSemaphoreDelete(s_preempt_sem);
        s_preempt_sem = NULL;
    }
    return BENCH_OK;
}

/* ------------------------------------------------------- ipc ping-pong */

/* Two capacity-1 queues carry a request and its echo; the peer runs one priority
 * above the benchmark task. measure() times the full round trip (two context
 * switches) and rejects a mismatched reply. True inter-task IPC, unlike case 1. */

static QueueHandle_t s_ipc_req;
static QueueHandle_t s_ipc_rep;
static TaskHandle_t  s_ipc_peer;

static void ipc_peer_entry(void *arg)
{
    (void)arg;
    uint32_t v;
    for (;;) {
        if (xQueueReceive(s_ipc_req, &v, portMAX_DELAY) == pdPASS) {
            (void)xQueueSend(s_ipc_rep, &v, portMAX_DELAY);
        }
    }
}

static bench_status port_ipc_create(void *ctx)
{
    (void)ctx;
    s_ipc_req = xQueueCreate(1, sizeof(uint32_t));
    s_ipc_rep = xQueueCreate(1, sizeof(uint32_t));
    if (s_ipc_req == NULL || s_ipc_rep == NULL) {
        return BENCH_PORT;
    }
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(ipc_peer_entry, "bench_ipc", 2048, NULL, priority,
                    &s_ipc_peer) != pdPASS) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_ipc_measure(void *ctx, uint32_t value, uint64_t *ticks)
{
    (void)ctx;
    if (s_ipc_req == NULL || s_ipc_rep == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start = c6_systimer_count();
    if (xQueueSend(s_ipc_req, &value, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint32_t received_value = 0;
    if (xQueueReceive(s_ipc_rep, &received_value, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t end = c6_systimer_count();
    if (received_value != value) {
        return BENCH_CORRUPT;
    }
    *ticks = end - start;
    return BENCH_OK;
}

static bench_status port_ipc_destroy(void *ctx)
{
    (void)ctx;
    if (s_ipc_peer != NULL) {
        vTaskDelete(s_ipc_peer);
        s_ipc_peer = NULL;
    }
    if (s_ipc_req != NULL) {
        vQueueDelete(s_ipc_req);
        s_ipc_req = NULL;
    }
    if (s_ipc_rep != NULL) {
        vQueueDelete(s_ipc_rep);
        s_ipc_rep = NULL;
    }
    return BENCH_OK;
}

/* Case 18: IPC payload sweep. Each payload size owns a capacity-1 request/reply
 * queue pair so the kernel's item copy scales with the payload; one echo peer
 * per size runs one priority above the benchmark task. measure() times the full
 * round trip with the 16 MHz SYSTIMER and verifies every payload byte. */
#define IPC_PAYLOAD_N 5
static const uint16_t k_ipc_payload_bytes[IPC_PAYLOAD_N] = {1, 4, 16, 64, 256};
static QueueHandle_t s_ipc_payload_req[IPC_PAYLOAD_N];
static QueueHandle_t s_ipc_payload_rep[IPC_PAYLOAD_N];
static TaskHandle_t  s_ipc_payload_peer[IPC_PAYLOAD_N];
static uint8_t       s_ipc_payload_tx[256];
static uint8_t       s_ipc_payload_rx[256];

static void ipc_payload_peer_entry(void *arg)
{
    const size_t idx = (size_t)arg;
    uint8_t local[256];
    for (;;) {
        if (xQueueReceive(s_ipc_payload_req[idx], local, portMAX_DELAY) == pdPASS) {
            (void)xQueueSend(s_ipc_payload_rep[idx], local, portMAX_DELAY);
        }
    }
}

static int ipc_payload_index(uint32_t payload_bytes)
{
    for (int i = 0; i < IPC_PAYLOAD_N; i++) {
        if (k_ipc_payload_bytes[i] == payload_bytes) {
            return i;
        }
    }
    return -1;
}

static bench_status port_ipc_payload_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < sizeof(s_ipc_payload_tx); i++) {
        s_ipc_payload_tx[i] = (uint8_t)(0xA5u ^ i);
    }
    for (int i = 0; i < IPC_PAYLOAD_N; i++) {
        UBaseType_t item = (UBaseType_t)k_ipc_payload_bytes[i];
        s_ipc_payload_req[i] = xQueueCreate(1, item);
        s_ipc_payload_rep[i] = xQueueCreate(1, item);
        if (s_ipc_payload_req[i] == NULL || s_ipc_payload_rep[i] == NULL) {
            return BENCH_PORT;
        }
        UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
        if (xTaskCreate(ipc_payload_peer_entry, "bench_ipc_payload", 2048,
                        (void *)(size_t)i, priority,
                        &s_ipc_payload_peer[i]) != pdPASS) {
            return BENCH_PORT;
        }
    }
    return BENCH_OK;
}

static bench_status port_ipc_payload_measure(void *ctx, uint32_t payload_bytes, uint64_t *ticks)
{
    (void)ctx;
    int idx = ipc_payload_index(payload_bytes);
    if (idx < 0 || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start = c6_systimer_count();
    if (xQueueSend(s_ipc_payload_req[idx], s_ipc_payload_tx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_ipc_payload_rep[idx], s_ipc_payload_rx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t end = c6_systimer_count();
    for (uint32_t i = 0; i < payload_bytes; i++) {
        if (s_ipc_payload_rx[i] != s_ipc_payload_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    *ticks = (uint64_t)(end - start);
    return BENCH_OK;
}

static bench_status port_ipc_payload_destroy(void *ctx)
{
    (void)ctx;
    for (int i = 0; i < IPC_PAYLOAD_N; i++) {
        if (s_ipc_payload_peer[i] != NULL) {
            vTaskDelete(s_ipc_payload_peer[i]);
            s_ipc_payload_peer[i] = NULL;
        }
        if (s_ipc_payload_req[i] != NULL) {
            vQueueDelete(s_ipc_payload_req[i]);
            s_ipc_payload_req[i] = NULL;
        }
        if (s_ipc_payload_rep[i] != NULL) {
            vQueueDelete(s_ipc_payload_rep[i]);
            s_ipc_payload_rep[i] = NULL;
        }
    }
    return BENCH_OK;
}

/* Case 19: IPC throughput. Producer pushes BENCH_TP_MSGS 64-byte messages through
 * a bounded queue that a higher-priority consumer drains; the producer
 * accumulates the 16 MHz SYSTIMER deltas; the 64-bit counter cannot wrap. */
#define TP_MSG_BYTES 64
#define TP_MSGS      16384 /* 1 MiB / 64 B */
#define TP_QUEUE_DEPTH 8
static QueueHandle_t    s_tp_q;
static SemaphoreHandle_t s_tp_done;
static TaskHandle_t     s_tp_peer;
static uint8_t          s_tp_tx[TP_MSG_BYTES];
static volatile uint32_t s_tp_target;
static volatile uint32_t s_tp_seen;
static volatile int      s_tp_quit;
static int               s_tp_active;

static void tp_peer_entry(void *arg)
{
    uint8_t buf[TP_MSG_BYTES];
    (void)arg;
    for (;;) {
        if (xQueueReceive(s_tp_q, buf, portMAX_DELAY) != pdPASS) {
            continue;
        }
        if (s_tp_quit) {
            break;
        }
        if (++s_tp_seen >= s_tp_target) {
            (void)xSemaphoreGive(s_tp_done);
        }
    }
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
    s_tp_q = xQueueCreate(TP_QUEUE_DEPTH, TP_MSG_BYTES);
    s_tp_done = xSemaphoreCreateBinary();
    if (s_tp_q == NULL || s_tp_done == NULL) {
        return BENCH_PORT;
    }
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(tp_peer_entry, "bench_tp", 2048, NULL, priority,
                    &s_tp_peer) != pdPASS) {
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
    uint64_t previous_ticks = c6_systimer_count();
    for (uint32_t i = 0; i < TP_MSGS; i++) {
        if (xQueueSend(s_tp_q, s_tp_tx, portMAX_DELAY) != pdPASS) {
            return BENCH_PORT;
        }
        uint64_t now = c6_systimer_count();
        total += (uint32_t)(now - previous_ticks);
        previous_ticks = now;
    }
    if (xSemaphoreTake(s_tp_done, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t now = c6_systimer_count();
    total += (uint32_t)(now - previous_ticks);
    *ticks = total;
    return BENCH_OK;
}

static bench_status port_tp_destroy(void *ctx)
{
    (void)ctx;
    if (s_tp_active) {
        s_tp_quit = 1;
        vTaskDelete(s_tp_peer); /* blocked on the empty queue: safe to delete */
        s_tp_peer = NULL;
        vQueueDelete(s_tp_q);
        s_tp_q = NULL;
        vSemaphoreDelete(s_tp_done);
        s_tp_done = NULL;
        s_tp_active = 0;
    }
    return BENCH_OK;
}

/* Case 27: zero-copy IPC. Same 256-byte workload over a copied queue pair and
 * over a queue of buffer pointers plus one shared buffer, so only a reference
 * crosses the kernel in the zero-copy leg (FreeRTOS has no dedicated zero-copy
 * message API). measure() returns copied minus zero-copy at the 16 MHz
 * SYSTIMER, clamped to 0 when a sample transiently shows no benefit.
 * The copied leg matches case 18 at 256 B. */
#define ZCOPY_BYTES 256
static QueueHandle_t s_zcopy_cp_req;
static QueueHandle_t s_zcopy_cp_rep;
static TaskHandle_t  s_zcopy_cp_peer;
static QueueHandle_t s_zcopy_zc_req;
static QueueHandle_t s_zcopy_zc_rep;
static TaskHandle_t  s_zcopy_zc_peer;
static uint8_t       s_zcopy_tx[ZCOPY_BYTES];
static uint8_t       s_zcopy_rx[ZCOPY_BYTES];
static uint8_t       s_zcopy_shared[ZCOPY_BYTES];

static void zcopy_copy_peer_entry(void *arg)
{
    uint8_t local[ZCOPY_BYTES];
    (void)arg;
    for (;;) {
        if (xQueueReceive(s_zcopy_cp_req, local, portMAX_DELAY) == pdPASS) {
            (void)xQueueSend(s_zcopy_cp_rep, local, portMAX_DELAY);
        }
    }
}

static void zcopy_ref_peer_entry(void *arg)
{
    uint8_t *ref;
    (void)arg;
    for (;;) {
        if (xQueueReceive(s_zcopy_zc_req, &ref, portMAX_DELAY) == pdPASS) {
            (void)xQueueSend(s_zcopy_zc_rep, &ref, portMAX_DELAY);
        }
    }
}

static bench_status port_zcopy_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        s_zcopy_tx[i] = (uint8_t)(0x3Cu ^ i);
        s_zcopy_shared[i] = (uint8_t)(0x3Cu ^ i);
    }
    s_zcopy_cp_req = xQueueCreate(1, ZCOPY_BYTES);
    s_zcopy_cp_rep = xQueueCreate(1, ZCOPY_BYTES);
    s_zcopy_zc_req = xQueueCreate(1, sizeof(uint8_t *));
    s_zcopy_zc_rep = xQueueCreate(1, sizeof(uint8_t *));
    if (s_zcopy_cp_req == NULL || s_zcopy_cp_rep == NULL
        || s_zcopy_zc_req == NULL || s_zcopy_zc_rep == NULL) {
        return BENCH_PORT;
    }
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(zcopy_copy_peer_entry, "bench_zccp", 2048, NULL, priority,
                    &s_zcopy_cp_peer) != pdPASS) {
        return BENCH_PORT;
    }
    if (xTaskCreate(zcopy_ref_peer_entry, "bench_zcref", 2048, NULL, priority,
                    &s_zcopy_zc_peer) != pdPASS) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_zcopy_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_zcopy_cp_req == NULL || s_zcopy_zc_req == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start = c6_systimer_count();
    if (xQueueSend(s_zcopy_cp_req, s_zcopy_tx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_zcopy_cp_rep, s_zcopy_rx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t copied = (uint64_t)(c6_systimer_count() - start);
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    uint8_t *ref = s_zcopy_shared;
    uint8_t *back = NULL;
    start = c6_systimer_count();
    if (xQueueSend(s_zcopy_zc_req, &ref, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_zcopy_zc_rep, &back, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t zcopy = (uint64_t)(c6_systimer_count() - start);
    if (back != s_zcopy_shared) {
        return BENCH_CORRUPT;
    }
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (s_zcopy_shared[i] != s_zcopy_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    /* A non-positive difference is recorded as a no-benefit sentinel rather
     * than a silent 0: on a noisy timer a single sample can show no benefit,
     * and the sentinel preserves the count for the analysis. */
    *ticks = (copied > zcopy) ? (copied - zcopy) : BENCH_ZCOPY_NO_BENEFIT_SENTINEL;
    return BENCH_OK;
}

static bench_status port_zcopy_destroy(void *ctx)
{
    (void)ctx;
    if (s_zcopy_cp_peer != NULL) {
        vTaskDelete(s_zcopy_cp_peer);
        s_zcopy_cp_peer = NULL;
    }
    if (s_zcopy_zc_peer != NULL) {
        vTaskDelete(s_zcopy_zc_peer);
        s_zcopy_zc_peer = NULL;
    }
    if (s_zcopy_cp_req != NULL) { vQueueDelete(s_zcopy_cp_req); s_zcopy_cp_req = NULL; }
    if (s_zcopy_cp_rep != NULL) { vQueueDelete(s_zcopy_cp_rep); s_zcopy_cp_rep = NULL; }
    if (s_zcopy_zc_req != NULL) { vQueueDelete(s_zcopy_zc_req); s_zcopy_zc_req = NULL; }
    if (s_zcopy_zc_rep != NULL) { vQueueDelete(s_zcopy_zc_rep); s_zcopy_zc_rep = NULL; }
    return BENCH_OK;
}

/* ------------------------------------------- priority-inheritance mutex */

/* H (the benchmark task) blocks on a mutex held by L while M spins at a priority
 * between them. With inheritance L is boosted and releases promptly; without it
 * H waits out M's bounded spin. measure() returns H's lock-attempt to
 * acquisition ticks. */

#define M_SPIN_US 2000 /* ~2 ms */

static SemaphoreHandle_t s_mutex;
static SemaphoreHandle_t s_m_req;
static SemaphoreHandle_t s_m_held;
static SemaphoreHandle_t s_m_rel;
static SemaphoreHandle_t s_m_go;
static SemaphoreHandle_t s_m_done;
static TaskHandle_t      s_m_task;
static TaskHandle_t      s_l_task;
static volatile int      s_m_stop;

static void mutex_low_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (xSemaphoreTake(s_m_req, portMAX_DELAY) != pdPASS) {
            continue;
        }
        if (xSemaphoreTake(s_mutex, portMAX_DELAY) != pdPASS) {
            continue;
        }
        (void)xSemaphoreGive(s_m_held);
        (void)xSemaphoreTake(s_m_rel, portMAX_DELAY);
        (void)xSemaphoreGive(s_mutex);
    }
}

static void mutex_med_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (xSemaphoreTake(s_m_go, portMAX_DELAY) != pdPASS) {
            continue;
        }
        int64_t start = esp_timer_get_time();
        while (!s_m_stop && (esp_timer_get_time() - start) < M_SPIN_US) {
        }
        (void)xSemaphoreGive(s_m_done);
    }
}

static bench_status port_mutex_create(void *ctx)
{
    (void)ctx;
    if (s_mutex != NULL) {
        return BENCH_OK;
    }
    UBaseType_t h = uxTaskPriorityGet(NULL);
    if (h < 2) {
        return BENCH_PORT;
    }
    s_mutex  = xSemaphoreCreateMutex();
    s_m_req  = xSemaphoreCreateBinary();
    s_m_held = xSemaphoreCreateBinary();
    s_m_rel  = xSemaphoreCreateBinary();
    s_m_go   = xSemaphoreCreateBinary();
    s_m_done = xSemaphoreCreateBinary();
    if (s_mutex == NULL || s_m_req == NULL || s_m_held == NULL ||
        s_m_rel == NULL || s_m_go == NULL || s_m_done == NULL) {
        return BENCH_PORT;
    }
    if (xTaskCreate(mutex_med_entry, "bench_m", 2048, NULL, h - 1,
                    &s_m_task) != pdPASS) {
        return BENCH_PORT;
    }
    if (xTaskCreate(mutex_low_entry, "bench_l", 2048, NULL, h - 2,
                    &s_l_task) != pdPASS) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_mutex_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_mutex == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    s_m_stop = 0;
    if (xSemaphoreGive(s_m_req) != pdPASS) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_m_held, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    (void)xSemaphoreGive(s_m_go);
    uint32_t t0 = c6_systimer_count();
    (void)xSemaphoreGive(s_m_rel);
    if (xSemaphoreTake(s_mutex, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t t1 = c6_systimer_count();
    s_m_stop = 1;
    (void)xSemaphoreTake(s_m_done, portMAX_DELAY);
    (void)xSemaphoreGive(s_mutex);
    *ticks = t1 - t0;
    return BENCH_OK;
}

static bench_status port_mutex_destroy(void *ctx)
{
    (void)ctx;
    if (s_m_task != NULL) {
        vTaskDelete(s_m_task);
        s_m_task = NULL;
    }
    if (s_l_task != NULL) {
        vTaskDelete(s_l_task);
        s_l_task = NULL;
    }
    if (s_mutex != NULL)  { vSemaphoreDelete(s_mutex);  s_mutex = NULL; }
    if (s_m_req != NULL)  { vSemaphoreDelete(s_m_req);  s_m_req = NULL; }
    if (s_m_held != NULL) { vSemaphoreDelete(s_m_held); s_m_held = NULL; }
    if (s_m_rel != NULL)  { vSemaphoreDelete(s_m_rel);  s_m_rel = NULL; }
    if (s_m_go != NULL)   { vSemaphoreDelete(s_m_go);   s_m_go = NULL; }
    if (s_m_done != NULL) { vSemaphoreDelete(s_m_done); s_m_done = NULL; }
    return BENCH_OK;
}

/* ------------------------------------------------- timer callback (9) */

/* One-shot FreeRTOS software timer (daemon task context) armed for 10 ms; the
 * callback timestamps itself and signals the benchmark task. */

#define TIMER_PERIOD_MS 10
#define TIMER_PERIOD_TICKS ((uint64_t)TIMER_PERIOD_MS * (BENCH_HZ / 1000ULL))

static TimerHandle_t     s_timer;
static SemaphoreHandle_t s_timer_sem;
static volatile uint64_t s_timer_fire;

static void timer_cb(TimerHandle_t t)
{
    (void)t;
    s_timer_fire = c6_systimer_count();
    (void)xSemaphoreGive(s_timer_sem);
}

static bench_status port_timer_create(void *ctx)
{
    (void)ctx;
    if (s_timer != NULL) {
        return BENCH_OK;
    }
    s_timer_sem = xSemaphoreCreateBinary();
    if (s_timer_sem == NULL) {
        return BENCH_PORT;
    }
    s_timer = xTimerCreate("bench_t", pdMS_TO_TICKS(TIMER_PERIOD_MS), pdFALSE,
                           NULL, timer_cb);
    if (s_timer == NULL) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_timer_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_timer == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xSemaphoreTake(s_timer_sem, 0);
    uint32_t t0 = c6_systimer_count();
    if (xTimerStart(s_timer, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_timer_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t t1 = s_timer_fire;
    uint64_t interval = (t1 >= t0) ? (t1 - t0) : 0;
    *ticks = (interval >= TIMER_PERIOD_TICKS)
                 ? (interval - TIMER_PERIOD_TICKS)
                 : (TIMER_PERIOD_TICKS - interval);
    return BENCH_OK;
}

static bench_status port_timer_destroy(void *ctx)
{
    (void)ctx;
    if (s_timer != NULL) {
        (void)xTimerDelete(s_timer, portMAX_DELAY);
        s_timer = NULL;
    }
    if (s_timer_sem != NULL) {
        vSemaphoreDelete(s_timer_sem);
        s_timer_sem = NULL;
    }
    return BENCH_OK;
}

/* Case 12: uncontended binary-semaphore give -> take API round trip. */
static SemaphoreHandle_t s_api_sem;
static bench_status port_sem_create(void *ctx)
{
    (void)ctx;
    if (s_api_sem == NULL) {
        s_api_sem = xSemaphoreCreateBinary();
        if (s_api_sem == NULL) {
            return BENCH_PORT;
        }
    }
    (void)xSemaphoreTake(s_api_sem, 0); /* drain: measure()'s give arms the take */
    return BENCH_OK;
}
static bench_status port_sem_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_api_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (xSemaphoreGive(s_api_sem) != pdTRUE) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_api_sem, 0) != pdTRUE) {
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
    if (s_api_sem != NULL) {
        vSemaphoreDelete(s_api_sem);
        s_api_sem = NULL;
    }
    return BENCH_OK;
}

/* Case 13: uncontended mutex lock -> unlock API round trip. */
static SemaphoreHandle_t s_api_mutex;
static bench_status port_mutex_u_create(void *ctx)
{
    (void)ctx;
    if (s_api_mutex == NULL) {
        s_api_mutex = xSemaphoreCreateMutex();
        if (s_api_mutex == NULL) {
            return BENCH_PORT;
        }
    }
    return BENCH_OK;
}
static bench_status port_mutex_u_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_api_mutex == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (xSemaphoreTake(s_api_mutex, portMAX_DELAY) != pdTRUE) {
        return BENCH_PORT;
    }
    if (xSemaphoreGive(s_api_mutex) != pdTRUE) {
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
    if (s_api_mutex != NULL) {
        vSemaphoreDelete(s_api_mutex);
        s_api_mutex = NULL;
    }
    return BENCH_OK;
}

/* Case 14: allocator allocate -> free round trip (64-byte payload). Uses the
 * kernel heap, so it measures the RTOS allocator, not libc. */
static bench_status port_alloc_create(void *ctx) { (void)ctx; return BENCH_OK; }
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
    void *p = pvPortMalloc(64);
    vPortFree(p);
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
static bench_status port_alloc_destroy(void *ctx) { (void)ctx; return BENCH_OK; }

/* Case 15: one-shot delay accuracy. Requests one RTOS tick and reports the
 * absolute deviation of the actual elapsed SYSTIMER ticks from BENCH_HZ/1000
 * (the nominal 1 ms). */
static bench_status port_sleep_create(void *ctx) { (void)ctx; return BENCH_OK; }
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
    vTaskDelay(1);
    status = port_now_ticks(NULL, &end);
    if (status != BENCH_OK) {
        return status;
    }
    const uint64_t nominal = BENCH_HZ / 1000ULL;
    const uint64_t actual = end - start;
    *ticks = (actual >= nominal) ? (actual - nominal) : (nominal - actual);
    return BENCH_OK;
}
static bench_status port_sleep_destroy(void *ctx) { (void)ctx; return BENCH_OK; }

/* Cases 16/17/21/25/26 are implemented on the STM32F4 FreeRTOS board only;
 * these stubs keep the shared Rust adapter linkable on the other boards. */
/* Cases 16/17/26 (portable FreeRTOS primitives). */
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
    for (int a = 0; a < 4; ++a) {
        TickType_t before = xTaskGetTickCount();
        uint64_t start = 0, end = 0;
        if (port_now_ticks(NULL, &start) != BENCH_OK) {
            return BENCH_PORT;
        }
        taskYIELD();
        if (port_now_ticks(NULL, &end) != BENCH_OK) {
            return BENCH_PORT;
        }
        if (xTaskGetTickCount() == before && end >= start) {
            *ticks = end - start;
            return BENCH_OK;
        }
    }
    return BENCH_CORRUPT;
}
static bench_status port_sched_destroy(void *ctx)
{
    (void)ctx;
    return BENCH_OK;
}

static TaskHandle_t      s_notify_peer;
static TaskHandle_t      s_notify_target;
static SemaphoreHandle_t s_notify_go;
static volatile uint64_t s_notify_signal;

static void notify_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (xSemaphoreTake(s_notify_go, portMAX_DELAY) != pdPASS) {
            continue;
        }
        uint64_t t = 0;
        (void)port_now_ticks(NULL, &t);
        s_notify_signal = t;
        (void)xTaskNotify(s_notify_target, 0, eIncrement);
    }
}
static bench_status port_notify_create(void *ctx)
{
    (void)ctx;
    s_notify_go = xSemaphoreCreateBinary();
    if (s_notify_go == NULL) {
        return BENCH_PORT;
    }
    s_notify_target = xTaskGetCurrentTaskHandle();
    UBaseType_t priority = uxTaskPriorityGet(NULL);
    if (priority > (UBaseType_t)tskIDLE_PRIORITY + 1) {
        priority--;
    } else {
        priority = (UBaseType_t)tskIDLE_PRIORITY + 1;
    }
    if (xTaskCreate(notify_peer_entry, "bench_notify", BENCH_PEER_STACK, NULL,
                    priority, &s_notify_peer) != pdPASS) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}
static bench_status port_notify_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_notify_go == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)ulTaskNotifyTake(pdTRUE, 0);
    if (xSemaphoreGive(s_notify_go) != pdPASS) {
        return BENCH_PORT;
    }
    if (ulTaskNotifyTake(pdTRUE, portMAX_DELAY) == 0) {
        return BENCH_PORT;
    }
    uint64_t end = 0;
    if (port_now_ticks(NULL, &end) != BENCH_OK) {
        return BENCH_PORT;
    }
    if (end < s_notify_signal) {
        return BENCH_CORRUPT;
    }
    *ticks = end - s_notify_signal;
    return BENCH_OK;
}
static bench_status port_notify_destroy(void *ctx)
{
    (void)ctx;
    if (s_notify_peer != NULL) {
        vTaskDelete(s_notify_peer);
        s_notify_peer = NULL;
    }
    if (s_notify_go != NULL) {
        vSemaphoreDelete(s_notify_go);
        s_notify_go = NULL;
    }
    s_notify_target = NULL;
    return BENCH_OK;
}

#define C6_PRIO_N 4
static TaskHandle_t      s_prio_task[C6_PRIO_N];
static SemaphoreHandle_t s_prio_release;
static SemaphoreHandle_t s_prio_all;
static SemaphoreHandle_t s_prio_ack;
static volatile uint32_t s_prio_n;
static volatile uint8_t  s_prio_trace[C6_PRIO_N];

static void prio_peer_entry(void *arg)
{
    const uint8_t id = (uint8_t)(uintptr_t)arg;
    for (;;) {
        if (xSemaphoreTake(s_prio_release, portMAX_DELAY) != pdPASS) {
            continue;
        }
        if (s_prio_n < C6_PRIO_N) {
            s_prio_trace[s_prio_n] = id;
            s_prio_n++;
        }
        (void)xSemaphoreGive(s_prio_all);
        (void)xSemaphoreTake(s_prio_ack, portMAX_DELAY);
    }
}
static bench_status port_prio_create(void *ctx)
{
    (void)ctx;
    s_prio_release = xSemaphoreCreateCounting(C6_PRIO_N, 0);
    s_prio_all     = xSemaphoreCreateCounting(C6_PRIO_N, 0);
    s_prio_ack     = xSemaphoreCreateCounting(C6_PRIO_N, 0);
    if (s_prio_release == NULL || s_prio_all == NULL || s_prio_ack == NULL) {
        return BENCH_PORT;
    }
    const UBaseType_t base = uxTaskPriorityGet(NULL);
    for (uint8_t i = 0; i < C6_PRIO_N; ++i) {
        const UBaseType_t prio = base + 1 + i;
        if (prio >= configMAX_PRIORITIES) {
            return BENCH_PORT;
        }
        if (xTaskCreate(prio_peer_entry, "bench_prio", BENCH_PEER_STACK,
                        (void *)(uintptr_t)i, prio,
                        &s_prio_task[i]) != pdPASS) {
            return BENCH_PORT;
        }
    }
    return BENCH_OK;
}
static bench_status port_prio_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL || s_prio_release == NULL) {
        return BENCH_PORT;
    }
    s_prio_n = 0;
    vTaskSuspendAll();
    for (int i = 0; i < C6_PRIO_N; ++i) {
        (void)xSemaphoreGive(s_prio_release);
    }
    (void)xTaskResumeAll();
    for (int i = 0; i < C6_PRIO_N; ++i) {
        if (xSemaphoreTake(s_prio_all, portMAX_DELAY) != pdPASS) {
            return BENCH_PORT;
        }
    }
    int ok = (s_prio_n == C6_PRIO_N);
    for (uint32_t i = 0; ok && i < C6_PRIO_N; ++i) {
        if (s_prio_trace[i] != (uint8_t)(C6_PRIO_N - 1 - i)) {
            ok = 0;
        }
    }
    for (int i = 0; i < C6_PRIO_N; ++i) {
        (void)xSemaphoreGive(s_prio_ack);
    }
    *ticks = (ok != 0) ? 1u : 0u;
    return BENCH_OK;
}
static bench_status port_prio_destroy(void *ctx)
{
    (void)ctx;
    for (uint8_t i = 0; i < C6_PRIO_N; ++i) {
        if (s_prio_task[i] != NULL) {
            vTaskDelete(s_prio_task[i]);
            s_prio_task[i] = NULL;
        }
    }
    if (s_prio_release != NULL) { vSemaphoreDelete(s_prio_release); s_prio_release = NULL; }
    if (s_prio_all != NULL)     { vSemaphoreDelete(s_prio_all);     s_prio_all = NULL; }
    if (s_prio_ack != NULL)     { vSemaphoreDelete(s_prio_ack);     s_prio_ack = NULL; }
    return BENCH_OK;
}
bench_status bench_sched_create(void) { return port_sched_create(NULL); }
bench_status bench_sched_measure(uint64_t *ticks) { return port_sched_measure(NULL, ticks); }
bench_status bench_sched_destroy(void) { return port_sched_destroy(NULL); }
bench_status bench_notify_create(void) { return port_notify_create(NULL); }
bench_status bench_notify_measure(uint64_t *ticks) { return port_notify_measure(NULL, ticks); }
bench_status bench_notify_destroy(void) { return port_notify_destroy(NULL); }
bench_status bench_prio_create(void) { return port_prio_create(NULL); }
bench_status bench_prio_measure(uint64_t *ticks) { return port_prio_measure(NULL, ticks); }
bench_status bench_prio_destroy(void) { return port_prio_destroy(NULL); }

/* Case 25: maximum interrupt-disabled time proxy. A dedicated gptimer one-shot
 * (1 MHz) fires at 50 us; the task busy-waits (the CPU CSR stops in WFI) and
 * timestamps with the 16 MHz SYSTIMER. The gptimer ISR is masked by
 * portENTER_CRITICAL(&mux); MAXDIS_CONTROL=1 holds a ~80 us critical section as
 * the positive control. */
#define MAXDIS_PERIOD_TICKS       800U    /* 50 us @ 16 MHz SYSTIMER */
#define MAXDIS_CONTROL_CRIT_TICKS 1280U   /* 80 us @ 16 MHz */
#define MAXDIS_SPIN_LIMIT         1600000U /* ~100 ms cap */

static gptimer_handle_t  s_maxdis_timer;
static portMUX_TYPE      s_maxdis_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint64_t s_maxdis_isr_ts;
static volatile int      s_maxdis_done;

static bool IRAM_ATTR maxdis_on_alarm(gptimer_handle_t timer,
                                      const gptimer_alarm_event_data_t *edata,
                                      void *user_ctx)
{
    (void)timer;
    (void)edata;
    (void)user_ctx;
    s_maxdis_isr_ts = c6_systimer_count();
    s_maxdis_done = 1;
    return false;
}

static bench_status port_maxdis_create(void *ctx)
{
    (void)ctx;
    gptimer_config_t config = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000, /* 1 MHz -> 1 us per tick */
    };
    if (gptimer_new_timer(&config, &s_maxdis_timer) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_event_callbacks_t cbs = {
        .on_alarm = maxdis_on_alarm,
    };
    if (gptimer_register_event_callbacks(s_maxdis_timer, &cbs, NULL) != ESP_OK) {
        return BENCH_PORT;
    }
    gptimer_alarm_config_t alarm = {
        .alarm_count = 50, /* 50 us one-shot */
        .reload_count = 0,
        .flags.auto_reload_on_alarm = false,
    };
    if (gptimer_set_alarm_action(s_maxdis_timer, &alarm) != ESP_OK) {
        return BENCH_PORT;
    }
    if (gptimer_enable(s_maxdis_timer) != ESP_OK) {
        return BENCH_PORT;
    }
    return BENCH_OK;
}

static bench_status port_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_maxdis_timer == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    s_maxdis_done = 0;
    uint32_t t0 = c6_systimer_count();
    if (gptimer_set_raw_count(s_maxdis_timer, 0) != ESP_OK) {
        return BENCH_PORT;
    }
    if (gptimer_start(s_maxdis_timer) != ESP_OK) {
        return BENCH_PORT;
    }
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
    portENTER_CRITICAL(&s_maxdis_mux);
    {
        uint64_t c0 = c6_systimer_count();
        while ((c6_systimer_count() - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
        }
    }
    portEXIT_CRITICAL(&s_maxdis_mux);
#endif
    while (s_maxdis_done == 0) {
        if ((c6_systimer_count() - t0) > MAXDIS_SPIN_LIMIT) {
            (void)gptimer_stop(s_maxdis_timer);
            return BENCH_PORT;
        }
    }
    (void)gptimer_stop(s_maxdis_timer);
    uint64_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)(elapsed - MAXDIS_PERIOD_TICKS);
    return BENCH_OK;
}

static bench_status port_maxdis_destroy(void *ctx)
{
    (void)ctx;
    if (s_maxdis_timer != NULL) {
        (void)gptimer_stop(s_maxdis_timer);
        (void)gptimer_disable(s_maxdis_timer);
        (void)gptimer_del_timer(s_maxdis_timer);
        s_maxdis_timer = NULL;
    }
    return BENCH_OK;
}
bench_status bench_maxdis_create(void) { return port_maxdis_create(NULL); }
bench_status bench_maxdis_measure(uint64_t *ticks) { return port_maxdis_measure(NULL, ticks); }
bench_status bench_maxdis_destroy(void) { return port_maxdis_destroy(NULL); }

#define BENCH_UNSUPPORTED_TRIPLE(name) \
    static bench_status port_##name##_create(void *c) { (void)c; return BENCH_UNSUPPORTED; } \
    static bench_status port_##name##_measure(void *c, uint64_t *t) { (void)c; (void)t; return BENCH_UNSUPPORTED; } \
    static bench_status port_##name##_destroy(void *c) { (void)c; return BENCH_UNSUPPORTED; } \
    bench_status bench_##name##_create(void) { return port_##name##_create(NULL); } \
    bench_status bench_##name##_measure(uint64_t *t) { return port_##name##_measure(NULL, t); } \
    bench_status bench_##name##_destroy(void) { return port_##name##_destroy(NULL); }
BENCH_UNSUPPORTED_TRIPLE(tick)

/* Rust-adapter harness primitives (ESP32-C6). */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_run(pend_h);
}
int bench_rust_isr_arm(void)
{
    if (gptimer_set_raw_count(s_isr_timer, 0) != ESP_OK) {
        return (int)BENCH_PORT;
    }
    if (gptimer_start(s_isr_timer) != ESP_OK) {
        return (int)BENCH_PORT;
    }
    return 0;
}
int bench_rust_isr_wait(void)
{
    if (xSemaphoreTake(s_isr_sem, portMAX_DELAY) != pdPASS) {
        return (int)BENCH_PORT;
    }
    (void)gptimer_stop(s_isr_timer);
    return 0;
}
uint64_t bench_rust_isr_entry(void)
{
    return (uint64_t)s_isr_entry;
}
int bench_rust_trig_drain(void)
{
    (void)xSemaphoreTake(s_trig_sem, 0);
    return 0;
}
uint64_t bench_rust_trig_fire(void)
{
    portDISABLE_INTERRUPTS();
    uint32_t t0 = c6_systimer_count();
    C6_FROM_CPU2_REG = 1u;
    portENABLE_INTERRUPTS();
    return (uint64_t)t0;
}
int bench_rust_trig_wait(void)
{
    if (xSemaphoreTake(s_trig_sem, pdMS_TO_TICKS(50)) != pdPASS) {
        C6_FROM_CPU2_REG = 0;
        return (int)BENCH_PORT;
    }
    return 0;
}
uint64_t bench_rust_trig_entry(void)
{
    return (uint64_t)s_trig_entry;
}
uint64_t bench_rust_maxdis_begin(void)
{
    s_maxdis_done = 0;
    return (uint64_t)c6_systimer_count();
}
int bench_rust_maxdis_arm(void)
{
    if (gptimer_set_raw_count(s_maxdis_timer, 0) != ESP_OK) {
        return (int)BENCH_PORT;
    }
    if (gptimer_start(s_maxdis_timer) != ESP_OK) {
        return (int)BENCH_PORT;
    }
    return 0;
}
int bench_rust_maxdis_wait(uint64_t t0)
{
    while (s_maxdis_done == 0) {
        if ((c6_systimer_count() - (uint32_t)t0) > MAXDIS_SPIN_LIMIT) {
            (void)gptimer_stop(s_maxdis_timer);
            return (int)BENCH_PORT;
        }
    }
    (void)gptimer_stop(s_maxdis_timer);
    return 0;
}
uint64_t bench_rust_maxdis_isr_ts(void)
{
    return (uint64_t)s_maxdis_isr_ts;
}
int bench_rust_pjit_wait(void)
{
    return (xSemaphoreTake(s_pjit_sem, portMAX_DELAY) != pdPASS) ? (int)BENCH_PORT : 0;
}
int bench_rust_pjit_read(uint64_t *ticks)
{
    if (!s_pjit_active || ticks == NULL) {
        return (int)BENCH_PORT;
    }
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    if (s_pjit_releases > (n + 1U)) {
        /* A release was coalesced: resync the sample index so one transient
         * backlog does not poison every following sample of the case. */
        s_pjit_n = s_pjit_releases;
        return (int)BENCH_CORRUPT;
    }
    uint64_t count = 0;
    if (gptimer_get_raw_count(s_pjit_rel, &count) != ESP_OK) {
        return (int)BENCH_PORT;
    }
    if (count >= PJIT_P_TICKS) {
        return (int)BENCH_CORRUPT;
    }
    *ticks = count * (BENCH_HZ / 1000000ULL);
    return 0;
}


/* Rust-adapter tick-cost and heap-probe primitives (no tick trace on this
 * board; the heap probe needs no malloc-failed latch here). */
int bench_rust_tick_take(uint64_t *out)
{
    (void)out;
    return 0;
}
void bench_rust_frag_probe(int active)
{
    (void)active;
}

void **bench_rust_frag_blocks(void)
{
    return NULL;
}

/* Rust-adapter CPU-share primitives (this board does not advertise the CPU
 * cases, so these are never called). */
int bench_rust_cpu_start(uint32_t load_pct) { (void)load_pct; return (int)BENCH_UNSUPPORTED; }
void bench_rust_cpu_arm(void) {}
uint32_t bench_rust_cpu_idle(void) { return 0u; }
unsigned long long bench_rust_cpu_accum(void) { return 0ull; }
void bench_rust_cpu_disarm(void) {}
void bench_rust_cpu_stop(void) {}

/* ------------------------------------------------------------- sample sink */

typedef struct {
    int      case_id;    /* protocol case ID (0..4) */
    uint32_t index;      /* sample index within the case */
    uint64_t ticks;      /* measured ticks */
    uint32_t variant;    /* schema-v2 discriminator; 0 for plain cases */
} sample_t;

static sample_t s_samples[BENCH_MAX_SAMPLES];
static size_t   s_sample_count = 0;
static char     s_run_id[BENCH_RUN_ID_MAX + 1];
static int      s_flush_failed = 0;

/* record(): called after the timed section, once per valid sample. Appends to
 * the bounded buffer and rejects overflow (the spec forbids UART inside a
 * timed callback). */
/* The C runner passes bench_case enum values; the wire protocol uses protocol
 * IDs. 0-15 coincide; the sparse phase-2 enums must be translated back. */
static uint32_t protocol_id(bench_case c)
{
    switch (c) {
        case BENCH_SCHEDULER_OVERHEAD: return 16;
        case BENCH_NOTIFICATION_LATENCY: return 17;
        case BENCH_TICK_OVERHEAD: return 21;
        case BENCH_MAX_INTERRUPT_DISABLED: return 25;
        case BENCH_PRIORITY_SCHEDULING_CORRECTNESS: return 26;
        case BENCH_NESTED_IRQ_OVERHEAD: return 20;
        case BENCH_IPC_PAYLOAD_SWEEP: return 18;
        case BENCH_IPC_THROUGHPUT: return 19;
        case BENCH_ZERO_COPY_IPC: return 27;
        case BENCH_COMMON_ARENA_FRAGMENTATION: return 28;
        default: return (uint32_t)c;
    }
}

static bench_status sink_record_impl(void *ctx, bench_case c, uint32_t variant,
                                     uint32_t index, uint64_t ticks, uint64_t hz)
{
    (void)ctx;
    (void)hz;
    if (s_sample_count >= BENCH_MAX_SAMPLES) {
        return BENCH_PORT; /* overflow: reject, do not silently drop */
    }
    s_samples[s_sample_count].case_id = (int)protocol_id(c);
    s_samples[s_sample_count].index   = index;
    s_samples[s_sample_count].ticks   = ticks;
    s_samples[s_sample_count].variant = variant;
    s_sample_count++;
    return BENCH_OK;
}

static bench_status sink_record(void *ctx, bench_case c, uint32_t index,
                                uint64_t ticks, uint64_t hz)
{
    return sink_record_impl(ctx, c, 0, index, ticks, hz);
}

static bench_status sink_record_variant(void *ctx, bench_case c, uint32_t variant,
                                        uint32_t index, uint64_t ticks, uint64_t hz)
{
    return sink_record_impl(ctx, c, variant, index, ticks, hz);
}

/* flush(): called once per case at the end of bench_run_case. Emits the
 * buffered samples as measurement frames and clears the buffer. */
static bench_status sink_flush(void *ctx)
{
    (void)ctx;
    if (s_flush_failed) {
        return BENCH_PORT; /* a prior flush failed; keep the error state */
    }
    for (size_t i = 0; i < s_sample_count; i++) {
        /* Every case timestamps the 16 MHz SYSTIMER, so every frame carries
         * the uniform BENCH_HZ rate. */
        uint64_t hz = BENCH_HZ;
        if (emit_measurement(s_run_id, s_samples[i].case_id,
                             s_samples[i].index, s_samples[i].ticks,
                             hz, s_samples[i].variant) != 0) {
            s_flush_failed = 1;
            return BENCH_PORT;
        }
    }
    s_sample_count = 0;
    return BENCH_OK;
}

#ifdef BENCH_LANGUAGE_RUST
/* Bridges and accessors for the Rust application core
 * (firmware/freertos/rust). The C side keeps the UART/JSON session. */
extern int rust_bench_run_case(uint32_t case_id, uint32_t warmup,
                               uint32_t repetitions, void *ctx,
                               int (*record)(void *, uint32_t, uint32_t,
                                             uint64_t, uint64_t),
                               int (*record_variant)(void *, uint32_t, uint32_t,
                                                     uint32_t, uint64_t, uint64_t),
                               int (*flush)(void *));

uint64_t bench_rust_now_ticks(void)
{
    return c6_systimer_count();
}

uint64_t bench_rust_ticks_per_second(void)
{
    return (uint64_t)BENCH_HZ;
}

/* Rust-adapter primitives: taskYIELD and the tick rate are macros in FreeRTOS,
 * so the Rust core calls these minimal wrappers. */
uint32_t bench_rust_tick_hz(void)
{
    return (uint32_t)configTICK_RATE_HZ;
}

void bench_rust_yield(void)
{
    taskYIELD();
}
/* Macro-backed kernel ops (xTimerStart/Delete and the indexed notify API have
 * no portable symbol); the Rust core calls these wrappers. */
uint32_t bench_rust_notify_take(uint32_t clear_on_exit, uint32_t ticks_to_wait)
{
    return (uint32_t)ulTaskNotifyTake((BaseType_t)clear_on_exit,
                                      (TickType_t)ticks_to_wait);
}

void bench_rust_notify(void *task, uint32_t action)
{
    (void)xTaskNotify((TaskHandle_t)task, 0U, (eNotifyAction)action);
}

int bench_rust_timer_start(void *timer)
{
    return (int)xTimerStart((TimerHandle_t)timer, portMAX_DELAY);
}

int bench_rust_timer_delete(void *timer)
{
    return (int)xTimerDelete((TimerHandle_t)timer, portMAX_DELAY);
}




/* The Rust adapter reaches the GPTimer ISR harness through these names. */
int bench_isr_create(void) { return (int)port_isr_create(NULL); }
int bench_isr_measure(uint64_t *ticks)
{
    return (int)port_isr_measure(NULL, ticks);
}
int bench_isr_destroy(void) { return (int)port_isr_destroy(NULL); }
int bench_trigger_isr_create(void) { return (int)port_trigger_isr_create(NULL); }
int bench_trigger_isr_measure(uint64_t *ticks) { return (int)port_trigger_isr_measure(NULL, ticks); }
int bench_trigger_isr_destroy(void) { return (int)port_trigger_isr_destroy(NULL); }
int bench_pjit_create(void) { return (int)port_pjit_create(NULL); }
int bench_pjit_measure(uint64_t *ticks) { return (int)port_pjit_measure(NULL, ticks); }
int bench_pjit_set_load(uint32_t load_pct) { (void)load_pct; return 2; /* BENCH_UNSUPPORTED */ }
int bench_pjit_destroy(void) { return (int)port_pjit_destroy(NULL); }

/* Same for the context-switch and preemption task paths. */
int bench_yield_create(void) { return (int)port_yield_create(NULL); }
int bench_yield_now(void) { return (int)port_yield_now(NULL); }
int bench_yield_destroy(void) { return (int)port_yield_destroy(NULL); }
int bench_peers_set(uint32_t count) { return (int)port_peers_set(NULL, count); }
int bench_preempt_create(void) { return (int)port_preempt_create(NULL); }
int bench_preempt_measure(uint64_t *ticks)
{
    return (int)port_preempt_measure(NULL, ticks);
}
int bench_preempt_destroy(void) { return (int)port_preempt_destroy(NULL); }
int bench_ipc_create(void) { return (int)port_ipc_create(NULL); }
int bench_ipc_measure(uint32_t value, uint64_t *ticks)
{
    return (int)port_ipc_measure(NULL, value, ticks);
}
int bench_ipc_destroy(void) { return (int)port_ipc_destroy(NULL); }
int bench_ipc_payload_create(void) { return (int)port_ipc_payload_create(NULL); }
int bench_ipc_payload_measure(uint32_t payload_bytes, uint64_t *ticks)
{
    return (int)port_ipc_payload_measure(NULL, payload_bytes, ticks);
}
int bench_ipc_payload_destroy(void) { return (int)port_ipc_payload_destroy(NULL); }
int bench_tp_create(void) { return (int)port_tp_create(NULL); }
int bench_tp_measure(uint64_t *ticks) { return (int)port_tp_measure(NULL, ticks); }
int bench_tp_destroy(void) { return (int)port_tp_destroy(NULL); }
int bench_zcopy_create(void) { return (int)port_zcopy_create(NULL); }
int bench_zcopy_measure(uint64_t *ticks) { return (int)port_zcopy_measure(NULL, ticks); }
int bench_zcopy_destroy(void) { return (int)port_zcopy_destroy(NULL); }
/* Case 22 (ESP-IDF FreeRTOS) has no truthful SysTick trace hook here. */
int bench_cpu_create(void) { return (int)BENCH_UNSUPPORTED; }
int bench_cpu_measure(uint32_t load_pct, uint64_t *ticks)
{
    (void)load_pct;
    (void)ticks;
    return (int)BENCH_UNSUPPORTED;
}
int bench_cpu_destroy(void) { return (int)BENCH_OK; }
int bench_frag_create(void) { return (int)BENCH_UNSUPPORTED; }
int bench_frag_measure(uint64_t *ticks)
{
    (void)ticks;
    return (int)BENCH_UNSUPPORTED;
}
int bench_frag_destroy(void) { return (int)BENCH_OK; }

int bench_mutex_create(void) { return (int)port_mutex_create(NULL); }
int bench_mutex_measure(uint64_t *ticks)
{
    return (int)port_mutex_measure(NULL, ticks);
}
int bench_mutex_destroy(void) { return (int)port_mutex_destroy(NULL); }
int bench_timer_create(void) { return (int)port_timer_create(NULL); }
int bench_timer_measure(uint64_t *ticks)
{
    return (int)port_timer_measure(NULL, ticks);
}
int bench_timer_destroy(void) { return (int)port_timer_destroy(NULL); }

/* Cases 12-15 (uncontended sync / alloc / delay) share the C port paths. */
int bench_sem_create(void) { return (int)port_sem_create(NULL); }
int bench_sem_measure(uint64_t *ticks) { return (int)port_sem_measure(NULL, ticks); }
int bench_sem_destroy(void) { return (int)port_sem_destroy(NULL); }
int bench_mutex_u_create(void) { return (int)port_mutex_u_create(NULL); }
int bench_mutex_u_measure(uint64_t *ticks) { return (int)port_mutex_u_measure(NULL, ticks); }
int bench_mutex_u_destroy(void) { return (int)port_mutex_u_destroy(NULL); }
int bench_alloc_create(void) { return (int)port_alloc_create(NULL); }
int bench_alloc_measure(uint64_t *ticks) { return (int)port_alloc_measure(NULL, ticks); }
int bench_alloc_destroy(void) { return (int)port_alloc_destroy(NULL); }
int bench_sleep_create(void) { return (int)port_sleep_create(NULL); }
int bench_sleep_measure(uint64_t *ticks) { return (int)port_sleep_measure(NULL, ticks); }
int bench_sleep_destroy(void) { return (int)port_sleep_destroy(NULL); }

static bench_case rust_case_from_id(uint32_t case_id)
{
    switch (case_id) {
        case 1:  return BENCH_QUEUE_ROUNDTRIP;
        case 2:  return BENCH_TASK_HANDOFF;
        case 3:  return BENCH_PERIODIC_JITTER;
        case 4:  return BENCH_ISR_TO_TASK;
        case 5:  return BENCH_YIELD_ROUNDTRIP;
        case 6:  return BENCH_PREEMPT_LATENCY;
        case 7:  return BENCH_IPC_PINGPONG;
        case 8:  return BENCH_MUTEX_PRIORITY;
        case 9:  return BENCH_TIMER_CALLBACK;
        case 10: return BENCH_TRIGGER_TO_ISR;
        case 11: return BENCH_PERIODIC_JITTER_LOADED;
        case 12: return BENCH_SEMAPHORE_LATENCY;
        case 13: return BENCH_MUTEX_UNCONTENDED;
        case 14: return BENCH_ALLOC_LATENCY;
        case 15: return BENCH_SLEEP_ACCURACY;
        case 16: return BENCH_SCHEDULER_OVERHEAD;
        case 17: return BENCH_NOTIFICATION_LATENCY;
        case 18: return BENCH_IPC_PAYLOAD_SWEEP;
        case 19: return BENCH_IPC_THROUGHPUT;
        case 27: return BENCH_ZERO_COPY_IPC;
        case 20: return BENCH_NESTED_IRQ_OVERHEAD;
        case 25: return BENCH_MAX_INTERRUPT_DISABLED;
        case 26: return BENCH_PRIORITY_SCHEDULING_CORRECTNESS;
        case 28: return BENCH_COMMON_ARENA_FRAGMENTATION;
        default: return BENCH_TIMER_OVERHEAD;
    }
}

static int rust_record_bridge(void *ctx, uint32_t case_id, uint32_t index,
                              uint64_t ticks, uint64_t hz)
{
    return sink_record(ctx, rust_case_from_id(case_id), index, ticks, hz) == BENCH_OK ? 0 : -1;
}

static int rust_record_variant_bridge(void *ctx, uint32_t case_id, uint32_t variant,
                                      uint32_t index, uint64_t ticks, uint64_t hz)
{
    return sink_record_variant(ctx, rust_case_from_id(case_id), variant, index, ticks, hz)
                   == BENCH_OK
               ? 0
               : -1;
}

static int rust_flush_bridge(void *ctx)
{
    return sink_flush(ctx) == BENCH_OK ? 0 : -1;
}
#endif

/* ------------------------------------------------------------- validation */

static int run_id_valid(const char *id)
{
    if (id == NULL || id[0] == '\0') {
        return 0;
    }
    size_t n = strlen(id);
    if (n > BENCH_RUN_ID_MAX) {
        return 0;
    }
    for (size_t i = 0; i < n; i++) {
        char c = id[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-' || c == '_')) {
            return 0;
        }
    }
    return 1;
}

int bench_validate_start(const char *run_id, const char *build_id,
                         const uint32_t *cases, size_t n_cases,
                         uint32_t reps, uint32_t warmup,
                         const char **err_code, const char **err_msg)
{
    (void)warmup;
    if (!run_id_valid(run_id)) {
        *err_code = "bad_run_id";
        *err_msg = "run_id must be 1-64 ASCII [A-Za-z0-9_-]";
        return -1;
    }
    if (build_id == NULL || strcmp(build_id, BENCH_BUILD_ID) != 0) {
        *err_code = "wrong_build";
        *err_msg = "build_id does not match this firmware";
        return -1;
    }
    if (n_cases == 0 || n_cases > BENCH_KNOWN_CASES) {
        *err_code = "bad_cases";
        *err_msg = "cases must be 1..5 supported IDs";
        return -1;
    }
    for (size_t i = 0; i < n_cases; i++) {
        if (cases[i] >= BENCH_KNOWN_CASES) {
            *err_code = "unsupported_case";
            *err_msg = "case ID not supported by this firmware";
            return -1;
        }
    }
    if (reps == 0) {
        *err_code = "bad_repetitions";
        *err_msg = "repetitions must be nonzero";
        return -1;
    }
    if (reps > BENCH_MAX_SAMPLES) {
        *err_code = "too_many_samples";
        *err_msg = "repetitions exceed the sample buffer capacity";
        return -1;
    }
    return 0;
}

/* ------------------------------------------------------------------ run */

/* Drive one benchmark run. main.c validates first (emit error if invalid),
 * then calls this. Emits `started`, per-sample measurements (flushed between
 * cases), and `complete` on success. On a suite failure emits an `error`
 * frame and returns -1. Returns 0 on success, -1 on failure. */
int bench_run(const char *run_id, const char *build_id,
              const uint32_t *cases, size_t n_cases,
              uint32_t reps, uint32_t warmup)
{
    (void)build_id; /* validated by bench_validate_start before this is called */

    /* Copy the run_id into the adapter-local buffer used by the sink. */
    size_t id_len = strlen(run_id);
    if (id_len > BENCH_RUN_ID_MAX) {
        return -1;
    }
    memcpy(s_run_id, run_id, id_len + 1);
    s_sample_count = 0;
    s_flush_failed = 0;

    /* Reset the queue to a known state before the suite. */
    s_queue = NULL;

    /* Emit `started` before running so the host observes the run begin. */
    if (emit_started(run_id, build_id) != 0) {
        return -1;
    }

#ifdef BENCH_LANGUAGE_RUST
    /* Rust application core: native FreeRTOS queue + esp_timer via FFI. */
    int rc = 0;
    for (size_t i = 0; i < n_cases && rc == 0; i++) {
        rc = rust_bench_run_case(cases[i], warmup, reps, NULL,
                                 rust_record_bridge, rust_record_variant_bridge,
                                 rust_flush_bridge);
    }
    if (rc != 0) {
        emit_error(run_id, "bench_failed",
                   "rust rust_bench_run_case reported a failure");
        return -1;
    }
#else
    /* Map protocol case IDs (uint32) to the runner's bench_case enum array. */
    static bench_case s_bench_cases[BENCH_KNOWN_CASES];
    for (size_t i = 0; i < n_cases; i++) {
        s_bench_cases[i] = s_case_map[cases[i]];
    }

    bench_settings settings = {
        .warmup      = warmup,
        .repetitions = reps,
    };

    /* Wire the port and sink. */
    bench_port port = {
        .ctx = NULL,
        .now_ticks = port_now_ticks,
        .ticks_per_second = BENCH_HZ,
        .queue_create = port_queue_create,
        .queue_send = port_queue_send,
        .queue_receive = port_queue_receive,
        .queue_destroy = port_queue_destroy,
        .handoff_create = port_handoff_create,
        .handoff_measure = port_handoff_measure,
        .handoff_destroy = port_handoff_destroy,
        .jitter_create = port_jitter_create,
        .jitter_measure = port_jitter_measure,
        .jitter_destroy = port_jitter_destroy,
        .isr_create = port_isr_create,
        .isr_measure = port_isr_measure,
        .isr_destroy = port_isr_destroy,
        .yield_create = port_yield_create,
        .yield_now = port_yield_now,
        .yield_destroy = port_yield_destroy,
        .peers_set = port_peers_set,
        .preempt_create = port_preempt_create,
        .preempt_measure = port_preempt_measure,
        .preempt_destroy = port_preempt_destroy,
        .ipc_create = port_ipc_create,
        .ipc_measure = port_ipc_measure,
        .ipc_destroy = port_ipc_destroy,
        .ipc_payload_create = port_ipc_payload_create,
        .ipc_payload_measure = port_ipc_payload_measure,
        .ipc_payload_destroy = port_ipc_payload_destroy,
        .tp_create = port_tp_create,
        .tp_measure = port_tp_measure,
        .tp_destroy = port_tp_destroy,
        .mutex_create = port_mutex_create,
        .mutex_measure = port_mutex_measure,
        .mutex_destroy = port_mutex_destroy,
        .timer_create = port_timer_create,
        .timer_measure = port_timer_measure,
        .timer_destroy = port_timer_destroy,
        .trigger_isr_create = port_trigger_isr_create,
        .trigger_isr_measure = port_trigger_isr_measure,
        .trigger_isr_destroy = port_trigger_isr_destroy,
        .pjit_create = port_pjit_create,
        .pjit_measure = port_pjit_measure,
        .pjit_destroy = port_pjit_destroy,
        .sem_create = port_sem_create,
        .sem_measure = port_sem_measure,
        .sem_destroy = port_sem_destroy,
        .mutex_u_create = port_mutex_u_create,
        .mutex_u_measure = port_mutex_u_measure,
        .mutex_u_destroy = port_mutex_u_destroy,
        .alloc_create = port_alloc_create,
        .alloc_measure = port_alloc_measure,
        .alloc_destroy = port_alloc_destroy,
        .sleep_create = port_sleep_create,
        .sleep_measure = port_sleep_measure,
        .sleep_destroy = port_sleep_destroy,
        .sched_create = port_sched_create,
        .sched_measure = port_sched_measure,
        .sched_destroy = port_sched_destroy,
        .notify_create = port_notify_create,
        .notify_measure = port_notify_measure,
        .notify_destroy = port_notify_destroy,
        .prio_create = port_prio_create,
        .prio_measure = port_prio_measure,
        .prio_destroy = port_prio_destroy,
        .maxdis_create = port_maxdis_create,
        .maxdis_measure = port_maxdis_measure,
        .maxdis_destroy = port_maxdis_destroy,
        .nested_create = port_nested_create,
        .nested_measure = port_nested_measure,
        .nested_destroy = port_nested_destroy,
        .zcopy_create = port_zcopy_create,
        .zcopy_measure = port_zcopy_measure,
        .zcopy_destroy = port_zcopy_destroy,
    };
    bench_sink sink = {
        .ctx = NULL,
        .record = sink_record,
        .record_variant = sink_record_variant,
        .flush = sink_flush,
    };

    /* Run case by case so a failure names the case and its runner status
     * (each case's flush has already emitted its retained samples). */
    for (size_t i = 0; i < n_cases; i++) {
        bench_status rc = bench_run_case(&port, &sink, s_bench_cases[i], settings);
        if (rc != BENCH_OK) {
            char msg[64];
            snprintf(msg, sizeof(msg), "case %u failed (status %d)",
                     (unsigned)cases[i], (int)rc);
            emit_error(run_id, "bench_failed", msg);
            return -1;
        }
    }
#endif

    /* On success every case produced exactly `reps` samples. Emit the
     * per-case counts (one entry per case in the requested order). */
    static uint32_t s_counts[BENCH_KNOWN_CASES];
    for (size_t i = 0; i < n_cases; i++) {
        s_counts[i] = reps;
    }
    if (emit_complete(run_id, s_counts, n_cases) != 0) {
        return -1;
    }
    return 0;
}

