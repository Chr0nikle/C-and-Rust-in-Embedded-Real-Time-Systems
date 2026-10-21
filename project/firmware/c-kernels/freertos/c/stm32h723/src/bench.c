/* Native benchmark adapter for the STM32H723 FreeRTOS bench.
 *
 * Supplies the bench_port (DWT CYCCNT ticks, FreeRTOS queue) and bench_sink
 * (bounded sample buffer) required by rtos_bench.h, and drives bench_run_suite
 * from the protocol v1 `start` frame.
 *
 * Timer: DWT CYCCNT counts at the core clock (520 MHz). A single measurement
 * interval (timer_overhead: a few instructions; queue_roundtrip: sub-microsecond
 * API work) is always far below the 32-bit wrap (~8.3 ms), so end-minus-start is
 * exact within a sample; no software rollover extension is needed. The
 * ticks_per_second reported is the core clock, 520,000,000.
 *
 * Sink: a bounded buffer of (case, index, ticks) samples. record() appends
 * after the timed section and rejects overflow (the spec forbids transmitting
 * inside a timed callback). flush() emits the buffered samples as `measurement`
 * frames and clears the buffer; bench_run_case calls flush() once per case, so
 * samples are flushed between cases as required.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>

#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"
#include "semphr.h"
#include "timers.h"
#include "stm32h7xx_hal.h"
#include "rtos_bench.h"
#include "bench.h"

/* Core clock == DWT CYCCNT rate (AHB /1, 520 MHz). */
#define BENCH_HZ        520000000ULL
#define BENCH_FRAME_MAX 1024

/* Maximum samples buffered before a flush. One case holds `repetitions`
 * samples; the buffer must hold a full case. Sized for the >=1000-sample
 * campaign (2048 x 16 B = 32 KiB), placed in CCMRAM so the 128 KiB main RAM
 * keeps the FreeRTOS heap budget. Requests above this are rejected. */
#define BENCH_MAX_SAMPLES 2048

/* Max run_id length (protocol.md:20). */
#define BENCH_RUN_ID_MAX 64

/* Case-ID -> bench_case enum map. The protocol uses ordered integer IDs:
 *   0 = timer_overhead, 1 = queue_roundtrip, 2 = task_handoff. */
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
    [20] = BENCH_NESTED_IRQ_OVERHEAD, /* IDs 22-24 reserved */
    [21] = BENCH_TICK_OVERHEAD,
    [22] = BENCH_IDLE_CPU_OVERHEAD,
    [23] = BENCH_CPU_OVERHEAD_UNDER_LOAD,
    [24] = BENCH_HEAP_FRAGMENTATION,
    [25] = BENCH_MAX_INTERRUPT_DISABLED,
    [26] = BENCH_PRIORITY_SCHEDULING_CORRECTNESS,
    [27] = BENCH_ZERO_COPY_IPC,
    [28] = BENCH_COMMON_ARENA_FRAGMENTATION,
};
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

/* Emit `started`: {"protocol":1,"type":"started","run_id":..,"build_id":..} */
static int emit_started(const char *run_id, const char *build_id)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;

    len = frame_append_lit(p, cap, len,
                           "{\"protocol\":1,\"type\":\"started\",\"run_id\":\"");
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

    len = frame_append_lit(p, cap, len,
        "{\"protocol\":1,\"type\":\"measurement\",\"run_id\":\"");
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

/* Per-allocation heap overhead: on heap_4 an allocation carries a block header
 * plus size rounding. Measured as (free_before - free_after) - requested for a
 * 1-byte request, so it is the fixed metadata+rounding cost per block. */
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

/* Kernel RAM overhead for one added task beyond its stack (TCB + heap_4 block
 * metadata), bytes. A single probe is enough because the stack request (768 B)
 * is a whole number of portBYTE_ALIGNMENT units, so heap_4 rounds TCB and stack
 * identically. The probe task is deleted and the idle task reaps it before the
 * next frame; the free-heap delta is therefore just the per-task bookkeeping. */
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
 * tasks. `statuses` is caller-owned scratch of `cap` entries (this keeps the
 * array off BSS on SRAM-tight images). Transient peers deleted before
 * completion are not sampled; that is documented as the collector's scope. */
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

/* Emit `complete`: {"protocol":1,"type":"complete","run_id":..,
 *                   "per_case_counts":[..],"status":"ok",
 *                   "stack_high_water_bytes":..,"heap_min_free_bytes":..,
 *                   "heap_overhead_bytes":..}
 * The runtime fields are optional to the host; NULL asks
 * FreeRTOS for this (the session/benchmark) task's own minimum ever free
 * stack, so C and Rust routes report the same task. */
static int emit_complete(const char *run_id, const uint32_t *counts, size_t n)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;
    int first = 1;

    len = frame_append_lit(p, cap, len,
        "{\"protocol\":1,\"type\":\"complete\",\"run_id\":\"");
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
    /* Capture the minimum-ever heap before the RAM probe, which temporarily
     * consumes heap and could otherwise lower the reported minimum. */
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

/* Emit `error`: {"protocol":1,"type":"error","run_id":..,"code":..,
 *                "message":..} */
static int emit_error(const char *run_id, const char *code, const char *msg)
{
    char *p = s_frame;
    size_t cap = sizeof(s_frame);
    size_t len = 0;

    len = frame_append_lit(p, cap, len,
                           "{\"protocol\":1,\"type\":\"error\",\"run_id\":\"");
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

/* -------------------------------------------------------- DWT timer port */

static void dwt_init(void)
{
    /* Enable the Data Watchpoint and Trace unit and the cycle counter.
     * CoreDebug::DEMCR.TRCENA (bit 24) gates the DWT; DWT::CTRL.CYCCNTENA
     * (bit 0) enables CYCCNT. CYCCNT is 32-bit and counts at the core clock. */
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL = 0;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    __DSB();
}

static bench_status port_now_ticks(void *ctx, uint64_t *out)
{
    (void)ctx;
    uint32_t c = DWT->CYCCNT;
    *out = (uint64_t)c;
    return BENCH_OK;
}

#ifdef BENCH_LANGUAGE_RUST
/* Accessors and bridges for the Rust application core
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
    return (uint64_t)DWT->CYCCNT;
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



#endif

/* ----------------------------------------------------------- FreeRTOS queue */

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
 * s_handoff_done. The peer timestamps the moment it unblocks. */
static SemaphoreHandle_t s_handoff_signal;
static SemaphoreHandle_t s_handoff_done;
static TaskHandle_t      s_handoff_peer;
static volatile uint64_t s_handoff_wake;

/* Task-count sweep (case 2): extra runnable peers created/deleted between
 * samples. They sit at the session task's priority and yield, so they compete on
 * the ready list (round-robin with the session under time slicing) but never
 * touch the handoff semaphores; the measured handoff still targets the original
 * peer at priority+1. Count includes the original peer. */
#define HANDOFF_MAX_EXTRA 32U
/* CCM RAM (CPU-only, no DMA) so the handle array does not consume the tight
 * 128 KB main SRAM. */
static TaskHandle_t s_handoff_extra[HANDOFF_MAX_EXTRA]
    __attribute__((section(".ccmram")));
static uint32_t     s_handoff_extra_count
    __attribute__((section(".ccmram")));

static void handoff_extra_entry(void *arg)
{
    (void)arg;
    for (;;) {
        /* Same-priority cooperative peer (case 5): the session stays in the
         * round-robin, so it always regains the CPU to send `complete`, while
         * the yield->resume round trip must rotate past the extra peers. */
        taskYIELD();
    }
}

static bench_status port_peers_set(void *ctx, uint32_t count)
{
    (void)ctx;
    uint32_t want = (count > 0U) ? (count - 1U) : 0U;
    if (want > HANDOFF_MAX_EXTRA) {
        return BENCH_PORT;
    }
    while (s_handoff_extra_count > want) {
        vTaskDelete(s_handoff_extra[--s_handoff_extra_count]);
        s_handoff_extra[s_handoff_extra_count] = NULL;
    }
    UBaseType_t prio = uxTaskPriorityGet(NULL); /* same priority as the session */
    while (s_handoff_extra_count < want) {
        TaskHandle_t handle = NULL;
        if (xTaskCreate(handoff_extra_entry, "bench_peerx", 128, NULL, prio,
                        &handle) != pdPASS) {
            return BENCH_PORT;
        }
        s_handoff_extra[s_handoff_extra_count++] = handle;
    }
    return BENCH_OK;
}

static void handoff_peer_entry(void *arg)
{
    (void)arg;
    for (;;) {
        if (xSemaphoreTake(s_handoff_signal, portMAX_DELAY) != pdPASS) {
            continue;
        }
        s_handoff_wake = (uint64_t)DWT->CYCCNT;
        (void)xSemaphoreGive(s_handoff_done);
    }
}

static bench_status port_handoff_create(void *ctx)
{
    (void)ctx;
    /* .ccmram is not part of the zeroed .bss, so initialise the sweep state. */
    s_handoff_extra_count = 0U;
    s_handoff_signal = xSemaphoreCreateBinary();
    s_handoff_done   = xSemaphoreCreateBinary();
    if (s_handoff_signal == NULL || s_handoff_done == NULL) {
        return BENCH_PORT;
    }
    /* One priority above the benchmark task so the give preempts immediately. */
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(handoff_peer_entry, "bench_peer", 256, NULL, priority,
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
    while (s_handoff_extra_count > 0U) {
        vTaskDelete(s_handoff_extra[--s_handoff_extra_count]);
        s_handoff_extra[s_handoff_extra_count] = NULL;
    }
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

/* One millisecond scheduled period expressed in 520 MHz DWT ticks. */
#define JITTER_PERIOD_TICKS 520000ULL

static TickType_t s_jitter_last;
static uint64_t   s_jitter_prev;

static bench_status port_jitter_create(void *ctx)
{
    (void)ctx;
    s_jitter_last = xTaskGetTickCount();
    /* Align to the tick grid so every measured interval is one full period. */
    (void)xTaskDelayUntil(&s_jitter_last, pdMS_TO_TICKS(1));
    s_jitter_prev = (uint64_t)DWT->CYCCNT;
    return BENCH_OK;
}

static bench_status port_jitter_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xTaskDelayUntil(&s_jitter_last, pdMS_TO_TICKS(1));
    uint64_t now = (uint64_t)DWT->CYCCNT;
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

/* TIM2 fires a one-pulse update interrupt; the ISR timestamps entry and gives a
 * semaphore from interrupt context. measure() blocks on it and reports the
 * ISR-entry to awakened-task delta. The interrupt priority must be at or below
 * configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY to use the FromISR API. */

static SemaphoreHandle_t s_isr_sem;
static volatile uint64_t s_isr_entry;

void TIM2_IRQHandler(void)
{
    BaseType_t woken = pdFALSE;
    if ((TIM2->SR & TIM_SR_UIF) != 0U) {
        TIM2->SR = ~TIM_SR_UIF;
        s_isr_entry = (uint64_t)DWT->CYCCNT;
        (void)xSemaphoreGiveFromISR(s_isr_sem, &woken);
        portYIELD_FROM_ISR(woken);
    }
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
    __HAL_RCC_TIM2_CLK_ENABLE();
    TIM2->CR1  = 0;
    TIM2->PSC  = 0;
    TIM2->ARR  = 999U; /* ~10 us at the 100 MHz H7 APB1 timer clock */
    TIM2->DIER = TIM_DIER_UIE;
    TIM2->SR   = 0;
    HAL_NVIC_SetPriority(TIM2_IRQn, configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY, 0);
    HAL_NVIC_EnableIRQ(TIM2_IRQn);
    return BENCH_OK;
}

static bench_status port_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_isr_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    TIM2->CR1 = 0;
    TIM2->CNT = 0;
    TIM2->SR  = 0;
    TIM2->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
    if (xSemaphoreTake(s_isr_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t wake = (uint64_t)DWT->CYCCNT;
    if (wake < s_isr_entry) {
        return BENCH_CORRUPT;
    }
    *ticks = wake - s_isr_entry;
    return BENCH_OK;
}

static bench_status port_isr_destroy(void *ctx)
{
    (void)ctx;
    HAL_NVIC_DisableIRQ(TIM2_IRQn);
    TIM2->CR1 = 0;
    if (s_isr_sem != NULL) {
        vSemaphoreDelete(s_isr_sem);
        s_isr_sem = NULL;
    }
    return BENCH_OK;
}

/* Exposed to the Rust adapter so both languages measure the same ISR path. */
bench_status bench_isr_create(void) { return port_isr_create(NULL); }
bench_status bench_isr_measure(uint64_t *ticks) { return port_isr_measure(NULL, ticks); }
bench_status bench_isr_destroy(void) { return port_isr_destroy(NULL); }

/* -------------------------------------- periodic jitter under load (11) */

/* Case 11: a hardware timer (TIM3) releases the benchmark task at an exact
 * 750 us period (390000 DWT cycles at 520 MHz, ARR = 97499 on the 130 MHz APB1
 * timer clock), so the schedule does not depend on the RTOS tick. TIM4
 * injects a cycle-calibrated busy-loop ISR at a non-harmonic 137 us (71240
 * cycles, ~35% duty at a 24762-cycle budget). The release ISR (priority 5 = the
 * syscall threshold, so it may give the semaphore) stamps R0 on the first
 * release; measure() reports wake - (R0 + n*P) in fine ticks, the signed
 * activation error against the ideal release grid, with no first-differencing,
 * so constant release latency is retained. The load ISR (priority 1, numerically
 * above the syscall threshold) must not call any RTOS API. TIM3/TIM4 are
 * otherwise unused (TIM2 serves case 4, EXTI0 case 10). */

#define PJIT_P_CYCLES    390000U                    /* 750 us @ 520 MHz DWT */
/* TIM3 is a 16-bit counter. At the 65 MHz timer clock (PSC=1) the 750 us reload
 * is 48750 counts (ARR 48749). The earlier 130 MHz (PSC=0) reload of 97500 does
 * not fit ARR and truncated to a ~246 us period, so case 11 returned only
 * misses. TIM4 (137 us = 17810 counts) still fits with PSC=0. */
#define PJIT_TIMER_PRESC 1U
#define PJIT_TIMER_ARR   (((PJIT_P_CYCLES / 4U) / (PJIT_TIMER_PRESC + 1U)) - 1U)
#define PJIT_LOAD_CYCLES 71240U                     /* 137 us @ 520 MHz DWT */
#define PJIT_LOAD_ARR    ((PJIT_LOAD_CYCLES / 4U) - 1U) /* 17809, fits 16-bit */
#define PJIT_LOAD_TARGET 24762U /* ~35% duty (8000 cycles scaled to 520 MHz, matches Zephyr) */
/* Acceptance control: 0 disables the injected-load timer (load-off baseline).
 * The release schedule and measurement are unchanged. */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static SemaphoreHandle_t s_pjit_sem;
static volatile uint64_t s_pjit_r0;
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_last_releases; /* releases seen at the last set_load */
static volatile uint32_t s_pjit_n;
static volatile uint32_t s_pjit_iters;
/* Achieved load cost in cycles (diagnostic; read over SWD). */
volatile uint32_t g_pjit_cost;
/* Calibrated 100% load budget; case-11 sweep levels scale it via pjit_set_load. */
static volatile uint32_t s_pjit_base_iters = 1U;
/* Bounded interrupt-masked window (cycles) applied every 10 ms by a dedicated
 * high-priority task. A window longer than the 750 us period coalesces releases
 * (a deadline miss) but ends, so the system recovers between windows instead of
 * starving. s_pjit_mask_iters == 0 means no window. */
static volatile uint32_t s_pjit_mask_iters;
static volatile uint32_t s_pjit_mask_base = 1U;
static TaskHandle_t s_pjit_load_task;
#define PJIT_MASK_TARGET 832000U /* 1600 us @ 520 MHz DWT (base = level 100) */
/* Short period so the first spin lands inside the ~ms measurement window (the
 * case completes in a few periods; a 10 ms delay never fired during it). */
#define PJIT_MASK_PERIOD_MS 1U

static inline void pjit_load_body(uint32_t n)
{
    volatile uint32_t a = 0;
    while (n-- > 0U) {
        a += n;
    }
    (void)a;
}

/* Bounded CPU-hog window: spin for s_pjit_mask_iters cycles at a priority above
 * the benchmark task but below the TIM3 release ISR (interrupts still preempt
 * tasks), then wait PJIT_MASK_PERIOD_MS. The release ISR keeps firing and giving
 * the semaphore while the benchmark task cannot run, so releases coalesce (a
 * deadline miss) when the window spans more than one 750 us period. The window
 * is bounded, so the task advances between windows instead of wedging. */
static void pjit_load_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t it = s_pjit_mask_iters;
        if (it != 0U) {
            pjit_load_body(it);
        }
        vTaskDelay(pdMS_TO_TICKS(PJIT_MASK_PERIOD_MS));
    }
}

void TIM3_IRQHandler(void)
{
    if ((TIM3->SR & TIM_SR_UIF) != 0U) {
        TIM3->SR = ~TIM_SR_UIF;
        uint64_t t = (uint64_t)DWT->CYCCNT;
        if (s_pjit_releases == 0U) {
            s_pjit_r0 = t;
        }
        s_pjit_releases++;
        BaseType_t woken = pdFALSE;
        (void)xSemaphoreGiveFromISR(s_pjit_sem, &woken);
        portYIELD_FROM_ISR(woken);
    }
}

void TIM4_IRQHandler(void)
{
    TIM4->SR = ~TIM_SR_UIF;
#if PJIT_LOAD_ENABLE
    pjit_load_body(s_pjit_iters);
#endif
}

static bench_status port_pjit_create(void *ctx)
{
    (void)ctx;
    if (s_pjit_sem == NULL) {
        s_pjit_sem = xSemaphoreCreateBinary();
        if (s_pjit_sem == NULL) {
            return BENCH_PORT;
        }
    }
    s_pjit_r0 = 0;
    s_pjit_releases = 0;
    s_pjit_last_releases = 0;
    s_pjit_n = 0;
#if PJIT_LOAD_ENABLE
    /* Calibrate the load body to PJIT_LOAD_TARGET cycles with interrupts masked
     * (outside any timed section). Bounded to avoid spinning forever. */
    s_pjit_iters = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        uint64_t a = (uint64_t)DWT->CYCCNT;
        pjit_load_body(s_pjit_iters);
        uint64_t d = (uint64_t)DWT->CYCCNT - a;
        __set_PRIMASK(primask);
        g_pjit_cost = (uint32_t)d;
        if (d >= PJIT_LOAD_TARGET && d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_iters++;
        } else {
            uint32_t next = (uint32_t)(((uint64_t)s_pjit_iters * PJIT_LOAD_TARGET) / d);
            s_pjit_iters = (next == 0U) ? 1U : next;
        }
    }
    s_pjit_base_iters = (s_pjit_iters == 0U) ? 1U : s_pjit_iters;
    /* Calibrate the bounded masked-window base to ~1600 us. */
    s_pjit_mask_base = 100U;
    for (uint32_t tries = 0; tries < 64U; ++tries) {
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        uint64_t a = (uint64_t)DWT->CYCCNT;
        pjit_load_body(s_pjit_mask_base);
        uint64_t d = (uint64_t)DWT->CYCCNT - a;
        __set_PRIMASK(primask);
        if (d >= PJIT_MASK_TARGET && d < (PJIT_MASK_TARGET + PJIT_MASK_TARGET / 8U)) {
            break;
        }
        if (d == 0U) {
            s_pjit_mask_base++;
        } else {
            uint32_t next = (uint32_t)(((uint64_t)s_pjit_mask_base * PJIT_MASK_TARGET) / d);
            s_pjit_mask_base = (next == 0U) ? 1U : next;
        }
    }
    s_pjit_mask_iters = 0U;
    if (s_pjit_load_task == NULL) {
        (void)xTaskCreate(pjit_load_task, "pjitL", 256U, NULL,
                          configMAX_PRIORITIES - 1, &s_pjit_load_task);
    }
#endif /* PJIT_LOAD_ENABLE */
    __HAL_RCC_TIM3_CLK_ENABLE();
    TIM3->CR1 = 0;
    TIM3->PSC = PJIT_TIMER_PRESC;
    TIM3->ARR = PJIT_TIMER_ARR;
    TIM3->DIER = TIM_DIER_UIE;
    TIM3->SR = 0;
    HAL_NVIC_SetPriority(TIM3_IRQn, configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY, 0);
    HAL_NVIC_EnableIRQ(TIM3_IRQn);
#if PJIT_LOAD_ENABLE
    __HAL_RCC_TIM4_CLK_ENABLE();
    TIM4->CR1 = 0;
    TIM4->PSC = 0;
    TIM4->ARR = PJIT_LOAD_ARR;
    TIM4->DIER = TIM_DIER_UIE;
    TIM4->SR = 0;
    HAL_NVIC_SetPriority(TIM4_IRQn, 1U, 0);
    HAL_NVIC_EnableIRQ(TIM4_IRQn);
#endif /* PJIT_LOAD_ENABLE */
    TIM3->CR1 = TIM_CR1_CEN;
#if PJIT_LOAD_ENABLE
    TIM4->CR1 = TIM_CR1_CEN;
#endif
    return BENCH_OK;
}

static bench_status port_pjit_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_pjit_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_pjit_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t wake = (uint64_t)DWT->CYCCNT;
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    /* Per-sample miss detection: more than one release since the last set_load
     * (start of this sample) means at least one period was missed. The old
     * cumulative `releases > n+1` check stuck forever after one coalesce. */
    uint32_t rel = s_pjit_releases;
    uint32_t delta = rel - s_pjit_last_releases;
    s_pjit_last_releases = rel;
    if (delta > 1U) {
        return BENCH_DEADLINE_MISS;
    }
    /* Anchor the ideal grid to the *release* index, not the take index, so the
     * activation error does not drift after a missed release. */
    uint64_t ideal = s_pjit_r0 + ((uint64_t)(rel - 1U) * (uint64_t)PJIT_P_CYCLES);
    if (wake < ideal) {
        return BENCH_CORRUPT;
    }
    *ticks = wake - ideal;
    return BENCH_OK;
}

/* Case-11 sweep: scale the injected-load budget to load_pct% of the calibrated
 * baseline, or disable the load timer at 0%. Called between samples, outside the
 * timed region. */
static bench_status port_pjit_set_load(void *ctx, uint32_t load_pct)
{
    (void)ctx;
    (void)s_pjit_base_iters;
    s_pjit_last_releases = s_pjit_releases;
#if PJIT_LOAD_ENABLE
    /* The sweep uses only the bounded hog task, not the TIM4 body, so level 0
     * is a clean no-load control. TIM4 stays off during the sweep. */
    TIM4->CR1 = 0;
    s_pjit_iters = 0U;
    if (load_pct == 0U) {
        s_pjit_mask_iters = 0U;
    } else {
        uint32_t mw = (uint32_t)(((uint64_t)s_pjit_mask_base * load_pct) / 100U);
        s_pjit_mask_iters = (mw == 0U) ? 1U : mw;
    }
#else
    (void)load_pct;
#endif
    return BENCH_OK;
}

static bench_status port_pjit_destroy(void *ctx)
{
    (void)ctx;
    TIM3->CR1 = 0;
    TIM4->CR1 = 0;
    HAL_NVIC_DisableIRQ(TIM4_IRQn);
    HAL_NVIC_DisableIRQ(TIM3_IRQn);
    s_pjit_mask_iters = 0U;
    if (s_pjit_load_task != NULL) {
        vTaskDelete(s_pjit_load_task);
        s_pjit_load_task = NULL;
    }
    if (s_pjit_sem != NULL) {
        vSemaphoreDelete(s_pjit_sem);
        s_pjit_sem = NULL;
    }
    return BENCH_OK;
}

/* Exposed to the Rust adapter so both languages measure the same path. */
bench_status bench_pjit_create(void) { return port_pjit_create(NULL); }
bench_status bench_pjit_measure(uint64_t *ticks) { return port_pjit_measure(NULL, ticks); }
bench_status bench_pjit_set_load(uint32_t load_pct) { return port_pjit_set_load(NULL, load_pct); }
bench_status bench_pjit_destroy(void) { return port_pjit_destroy(NULL); }

/* ------------------------------------------- trigger to ISR entry (10) */

/* Software-asserted minimal interrupt. The task masks interrupts, timestamps
 * DWT, pends dedicated spare NVIC line EXTI0 and unmasks, so the exception is
 * taken at the unmask and the handler's first instruction is the ISR-entry
 * timestamp. Value = trigger -> entry, including the counter read and the NVIC
 * pend path; unlike case 4 there is no programmed one-shot delay to subtract.
 * EXTI0 is not otherwise used by this build (TIM2 serves case 4); the NVIC
 * pending bit is set directly, so no EXTI peripheral configuration is needed. */

static SemaphoreHandle_t s_trig_sem;
static volatile uint64_t s_trig_entry;
static volatile uint32_t s_trig_state; /* 0 idle, 1 armed, 2 entered */

void EXTI0_IRQHandler(void)
{
    s_trig_entry = (uint64_t)DWT->CYCCNT;
    s_trig_state = 2;
    NVIC_ClearPendingIRQ(EXTI0_IRQn);
    BaseType_t woken = pdFALSE;
    (void)xSemaphoreGiveFromISR(s_trig_sem, &woken);
    portYIELD_FROM_ISR(woken);
}

static bench_status port_trigger_isr_create(void *ctx)
{
    (void)ctx;
    if (s_trig_sem == NULL) {
        s_trig_sem = xSemaphoreCreateBinary();
        if (s_trig_sem == NULL) {
            return BENCH_PORT;
        }
    }
    s_trig_state = 0;
    NVIC_ClearPendingIRQ(EXTI0_IRQn);
    HAL_NVIC_SetPriority(EXTI0_IRQn, configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY, 0);
    HAL_NVIC_EnableIRQ(EXTI0_IRQn);
    return BENCH_OK;
}

static bench_status port_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_trig_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xSemaphoreTake(s_trig_sem, 0); /* drain any stale signal */
    s_trig_state = 1;
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    uint64_t t0 = (uint64_t)DWT->CYCCNT;
    NVIC_SetPendingIRQ(EXTI0_IRQn);
    __set_PRIMASK(primask); /* unmask: the exception is taken here */
    if (xSemaphoreTake(s_trig_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (s_trig_state != 2) {
        return BENCH_CORRUPT; /* a stale signal satisfied the wait */
    }
    uint64_t entry = s_trig_entry;
    if (entry < t0) {
        return BENCH_CORRUPT;
    }
    *ticks = entry - t0;
    return BENCH_OK;
}

static bench_status port_trigger_isr_destroy(void *ctx)
{
    (void)ctx;
    HAL_NVIC_DisableIRQ(EXTI0_IRQn);
    NVIC_ClearPendingIRQ(EXTI0_IRQn);
    if (s_trig_sem != NULL) {
        vSemaphoreDelete(s_trig_sem);
        s_trig_sem = NULL;
    }
    return BENCH_OK;
}

/* ------------------------------------------- nested interrupt (20) */

/* Two spare NVIC lines: EXTI1 (lowest priority) busy-waits a calibrated
 * interval in its handler; EXTI2 (higher priority) is pended during that wait
 * and preempts it. Both runs execute the same low-ISR loops, so
 * measure() = nested_elapsed - baseline_elapsed isolates the cost of nesting
 * the higher ISR. NESTED_CONTROL=1 disables the higher IRQ (both runs pend only
 * the low line) and must report ~0: the positive control. EXTI1/EXTI2 are not
 * otherwise used by this build (TIM2/3/4/5 and EXTI0 serve cases 4/11/25/10). */
#define NESTED_SPIN  2000U /* fixed-iteration low-ISR spin (~8000 cycles) */
#define NESTED_PENDS 10U   /* higher-priority pends per low ISR run */
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_h_count;
static volatile uint32_t s_nested_pend_h;

void EXTI1_IRQHandler(void) /* low priority: fixed-work busy loop */
{
    NVIC_ClearPendingIRQ(EXTI1_IRQn);
    const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
    uint32_t next = step;
    /* Iteration-count bounded, not wall-clock bounded: a higher-priority
     * preemption then adds its full duration to the measured elapsed time. */
    for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
        if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
            NVIC_SetPendingIRQ(EXTI2_IRQn); /* higher priority preempts here */
            next += step;
        }
#endif
    }
    s_nested_low_done = 1;
}

void EXTI2_IRQHandler(void) /* higher priority: minimal */
{
    NVIC_ClearPendingIRQ(EXTI2_IRQn);
    s_nested_h_count++;
}

static bench_status port_nested_create(void *ctx)
{
    (void)ctx;
    s_nested_low_done = 0;
    s_nested_h_count = 0;
    s_nested_pend_h = 0;
    NVIC_ClearPendingIRQ(EXTI1_IRQn);
    NVIC_ClearPendingIRQ(EXTI2_IRQn);
    HAL_NVIC_SetPriority(EXTI1_IRQn, 15U, 0); /* lowest */
    HAL_NVIC_SetPriority(EXTI2_IRQn, 5U, 0);  /* above EXTI1 */
    HAL_NVIC_EnableIRQ(EXTI1_IRQn);
    HAL_NVIC_EnableIRQ(EXTI2_IRQn);
    return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
    s_nested_low_done = 0;
    s_nested_h_count = 0;
    s_nested_pend_h = pend_h;
    const uint32_t t0 = DWT->CYCCNT;
    NVIC_SetPendingIRQ(EXTI1_IRQn);
    while (s_nested_low_done == 0) {
    }
    return DWT->CYCCNT - t0;
}

static bench_status port_nested_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    /* Min of three runs each: the least-interfered run is the cleanest estimate
     * of the fixed work, so the difference isolates the higher-ISR nesting. */
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
    HAL_NVIC_DisableIRQ(EXTI1_IRQn);
    HAL_NVIC_DisableIRQ(EXTI2_IRQn);
    NVIC_ClearPendingIRQ(EXTI1_IRQn);
    NVIC_ClearPendingIRQ(EXTI2_IRQn);
    return BENCH_OK;
}
bench_status bench_nested_create(void) { return port_nested_create(NULL); }
bench_status bench_nested_measure(uint64_t *ticks) { return port_nested_measure(NULL, ticks); }
bench_status bench_nested_destroy(void) { return port_nested_destroy(NULL); }

/* Exposed to the Rust adapter so both languages measure the same path. */
bench_status bench_trigger_isr_create(void) { return port_trigger_isr_create(NULL); }
bench_status bench_trigger_isr_measure(uint64_t *ticks) { return port_trigger_isr_measure(NULL, ticks); }
bench_status bench_trigger_isr_destroy(void) { return port_trigger_isr_destroy(NULL); }

/* ---------------------------------------------- yield / context switch (5) */

/* A peer task at the benchmark task's own priority loops on taskYIELD(). The
 * benchmark yields and confirms the peer ran, so one full equal-priority round
 * trip (out, peer yield, back in) is timed. */
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

static bench_status port_yield_create(void *ctx)
{
    (void)ctx;
    s_yield_peer = NULL;
    s_yield_peer_runs = 0;
    s_yield_peer_started = 0;
    /* Same priority as the benchmark task: an equal-priority peer is what makes
     * the yield a real switch rather than a no-op. */
    UBaseType_t priority = uxTaskPriorityGet(NULL);
    if (xTaskCreate(yield_peer_entry, "bench_yield", 256, NULL, priority,
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
    return BENCH_CORRUPT; /* no switch: sample would not measure a switch */
}

static bench_status port_yield_destroy(void *ctx)
{
    (void)ctx;
    while (s_handoff_extra_count > 0U) {
        vTaskDelete(s_handoff_extra[--s_handoff_extra_count]);
        s_handoff_extra[s_handoff_extra_count] = NULL;
    }
    if (s_yield_peer != NULL) {
        vTaskDelete(s_yield_peer);
        s_yield_peer = NULL;
    }
    return BENCH_OK;
}

/* --------------------------------------------------- preempt latency (6) */

/* H blocks on s_preempt_sem at one priority above the benchmark task (L). L
 * gives the semaphore, H is released immediately, timestamps itself and blocks
 * again, so L resumes and returns the delta. */
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
        s_preempt_wake = (uint64_t)DWT->CYCCNT;
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
    /* One priority above the benchmark task so the give preempts immediately. */
    UBaseType_t priority = uxTaskPriorityGet(NULL) + 1;
    if (xTaskCreate(preempt_high_entry, "bench_preempt", 256, NULL, priority,
                    &s_preempt_task) != pdPASS) {
        return BENCH_PORT;
    }
    /* Let H start and block on the semaphore before the first measurement, else
     * the first give would not preempt and the sample would be rejected. */
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
    uint32_t t0 = DWT->CYCCNT;
    if (xSemaphoreGive(s_preempt_sem) != pdPASS) {
        return BENCH_PORT;
    }
    /* H preempts L inside the give, records, and blocks again; L resumes here. */
    if (s_preempt_seq == seq) {
        return BENCH_CORRUPT; /* no preemption: non-preemptive or equal priority */
    }
    /* Wrap-safe 32-bit delta between the release and H's first timestamp. */
    *ticks = (uint64_t)((uint32_t)s_preempt_wake - t0);
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

/* Two capacity-1 queues carry a request and its echo. The peer runs one
 * priority above the benchmark task, so each send preempts it immediately;
 * measure() times the full request/response round trip (two context switches)
 * and rejects a mismatched reply. This is inter-task IPC, unlike case 1 which
 * only exercises the queue API inside a single task. */

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
    if (xTaskCreate(ipc_peer_entry, "bench_ipc", 256, NULL, priority,
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
    uint64_t start = (uint64_t)DWT->CYCCNT;
    if (xQueueSend(s_ipc_req, &value, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint32_t received_value = 0;
    if (xQueueReceive(s_ipc_rep, &received_value, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t end = (uint64_t)DWT->CYCCNT;
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
        vTaskDelete(s_ipc_peer); /* blocked on the request queue; removed safely */
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

/* Case 18: IPC payload sweep. Unlike case 7 (fixed 4-byte items) each payload
 * size owns a capacity-1 request/reply queue pair, so the kernel's own item copy
 * scales with the payload. Five peers, one per size, echo immediately at a
 * priority above the benchmark task. measure() times the full round trip and
 * verifies every payload byte. */
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
        if (xTaskCreate(ipc_payload_peer_entry, "bench_ipc_payload",
                        configMINIMAL_STACK_SIZE + 128, (void *)(size_t)i, priority,
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
    uint64_t start = (uint64_t)DWT->CYCCNT;
    if (xQueueSend(s_ipc_payload_req[idx], s_ipc_payload_tx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_ipc_payload_rep[idx], s_ipc_payload_rx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t end = (uint64_t)DWT->CYCCNT;
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

/* Case 19: IPC throughput. A producer pushes BENCH_TP_MSGS 64-byte messages
 * through a bounded queue that a higher-priority consumer drains; measure()
 * times the whole transfer. The producer samples DWT->CYCCNT after each send and
 * accumulates deltas, so the 32-bit counter's ~25 ms wrap cannot corrupt a
 * multi-hundred-ms 1 MiB transfer. */
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
    if (xTaskCreate(tp_peer_entry, "bench_tp", configMINIMAL_STACK_SIZE + 128,
                    NULL, priority, &s_tp_peer) != pdPASS) {
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
    uint32_t previous_ticks = DWT->CYCCNT;
    for (uint32_t i = 0; i < TP_MSGS; i++) {
        if (xQueueSend(s_tp_q, s_tp_tx, portMAX_DELAY) != pdPASS) {
            return BENCH_PORT;
        }
        uint32_t now = DWT->CYCCNT;
        total += (uint32_t)(now - previous_ticks);
        previous_ticks = now;
    }
    if (xSemaphoreTake(s_tp_done, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint32_t now = DWT->CYCCNT;
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

/* Case 27: zero-copy IPC. Two request/echo round trips at the same 256-byte
 * payload: the copied leg sends the payload through a capacity-1 queue (the
 * kernel copies it on send and receive, and the peer copies it on its echo), the
 * zero-copy leg sends only a pointer to a shared buffer so no payload byte is
 * copied by the kernel. FreeRTOS has no dedicated zero-copy message API, so the
 * zero-copy mechanism is a queue of buffer references plus one shared buffer;
 * that buffer stays valid for the whole round trip because the producer blocks
 * until the echo returns. measure() returns copied minus zero-copy ticks
 * (clamped to 0 when a sample transiently shows no benefit). The copied leg at
 * 256 B is the same workload as case 18's largest size and must match it. */
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
    if (xTaskCreate(zcopy_copy_peer_entry, "bench_zccp",
                    configMINIMAL_STACK_SIZE + 128, NULL, priority,
                    &s_zcopy_cp_peer) != pdPASS) {
        return BENCH_PORT;
    }
    if (xTaskCreate(zcopy_ref_peer_entry, "bench_zcref",
                    configMINIMAL_STACK_SIZE + 32, NULL, priority,
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
    uint64_t start = (uint64_t)DWT->CYCCNT;
    if (xQueueSend(s_zcopy_cp_req, s_zcopy_tx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_zcopy_cp_rep, s_zcopy_rx, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t copied = (uint64_t)DWT->CYCCNT - start;
    for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
        if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
            return BENCH_CORRUPT;
        }
    }
    uint8_t *ref = s_zcopy_shared;
    uint8_t *back = NULL;
    start = (uint64_t)DWT->CYCCNT;
    if (xQueueSend(s_zcopy_zc_req, &ref, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    if (xQueueReceive(s_zcopy_zc_rep, &back, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t zcopy = (uint64_t)DWT->CYCCNT - start;
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

#define M_SPIN_TICKS 1040000ULL /* ~2 ms at 520 MHz */

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
        uint64_t start = (uint64_t)DWT->CYCCNT;
        while (!s_m_stop && ((uint64_t)DWT->CYCCNT - start) < M_SPIN_TICKS) {
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
    if (xTaskCreate(mutex_med_entry, "bench_m", 256, NULL, h - 1,
                    &s_m_task) != pdPASS) {
        return BENCH_PORT;
    }
    if (xTaskCreate(mutex_low_entry, "bench_l", 256, NULL, h - 2,
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
    uint64_t t0 = (uint64_t)DWT->CYCCNT;
    (void)xSemaphoreGive(s_m_rel);
    if (xSemaphoreTake(s_mutex, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    uint64_t t1 = (uint64_t)DWT->CYCCNT;
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

/* A one-shot FreeRTOS software timer (daemon task context) is armed for a
 * nominal 10 ms; the callback timestamps itself and signals the benchmark task,
 * which returns the absolute deviation from the nominal period. */

#define TIMER_PERIOD_MS    10
#define TIMER_PERIOD_TICKS 5200000ULL /* 10 ms at 520 MHz */

static TimerHandle_t     s_timer;
static SemaphoreHandle_t s_timer_sem;
static volatile uint64_t s_timer_fire;

static void timer_cb(TimerHandle_t t)
{
    (void)t;
    s_timer_fire = (uint64_t)DWT->CYCCNT;
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
    (void)xSemaphoreTake(s_timer_sem, 0); /* drain any stale signal */
    uint64_t t0 = (uint64_t)DWT->CYCCNT;
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

/* Exposed to the Rust adapter so both languages time the same task paths. */
bench_status bench_yield_create(void) { return port_yield_create(NULL); }
bench_status bench_yield_now(void) { return port_yield_now(NULL); }
bench_status bench_yield_destroy(void) { return port_yield_destroy(NULL); }
bench_status bench_peers_set(uint32_t count) { return port_peers_set(NULL, count); }
bench_status bench_preempt_create(void) { return port_preempt_create(NULL); }
bench_status bench_preempt_measure(uint64_t *ticks) { return port_preempt_measure(NULL, ticks); }
bench_status bench_preempt_destroy(void) { return port_preempt_destroy(NULL); }
bench_status bench_ipc_create(void) { return port_ipc_create(NULL); }
bench_status bench_ipc_measure(uint32_t value, uint64_t *ticks) { return port_ipc_measure(NULL, value, ticks); }
bench_status bench_ipc_destroy(void) { return port_ipc_destroy(NULL); }
bench_status bench_ipc_payload_create(void) { return port_ipc_payload_create(NULL); }
bench_status bench_ipc_payload_measure(uint32_t payload_bytes, uint64_t *ticks) {
    return port_ipc_payload_measure(NULL, payload_bytes, ticks);
}
bench_status bench_ipc_payload_destroy(void) { return port_ipc_payload_destroy(NULL); }
bench_status bench_tp_create(void) { return port_tp_create(NULL); }
bench_status bench_tp_measure(uint64_t *ticks) { return port_tp_measure(NULL, ticks); }
bench_status bench_tp_destroy(void) { return port_tp_destroy(NULL); }
bench_status bench_zcopy_create(void) { return port_zcopy_create(NULL); }
bench_status bench_zcopy_measure(uint64_t *ticks) { return port_zcopy_measure(NULL, ticks); }
bench_status bench_zcopy_destroy(void) { return port_zcopy_destroy(NULL); }
bench_status bench_mutex_create(void) { return port_mutex_create(NULL); }
bench_status bench_mutex_measure(uint64_t *ticks) { return port_mutex_measure(NULL, ticks); }
bench_status bench_mutex_destroy(void) { return port_mutex_destroy(NULL); }
bench_status bench_timer_create(void) { return port_timer_create(NULL); }
bench_status bench_timer_measure(uint64_t *ticks) { return port_timer_measure(NULL, ticks); }
bench_status bench_timer_destroy(void) { return port_timer_destroy(NULL); }

/* ------------------------------- uncontended sync / alloc / delay (12-15) */

/* Case 12: uncontended binary-semaphore give -> take API round trip. */
static SemaphoreHandle_t s_sem;
static bench_status port_sem_create(void *ctx)
{
    (void)ctx;
    if (s_sem == NULL) {
        s_sem = xSemaphoreCreateBinary();
        if (s_sem == NULL) {
            return BENCH_PORT;
        }
    }
    (void)xSemaphoreTake(s_sem, 0); /* drain: measure()'s give arms the take */
    return BENCH_OK;
}
static bench_status port_sem_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (xSemaphoreGive(s_sem) != pdTRUE) {
        return BENCH_PORT;
    }
    if (xSemaphoreTake(s_sem, 0) != pdTRUE) {
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
    if (s_sem != NULL) {
        vSemaphoreDelete(s_sem);
        s_sem = NULL;
    }
    return BENCH_OK;
}
bench_status bench_sem_create(void) { return port_sem_create(NULL); }
bench_status bench_sem_measure(uint64_t *ticks) { return port_sem_measure(NULL, ticks); }
bench_status bench_sem_destroy(void) { return port_sem_destroy(NULL); }

/* Case 13: uncontended mutex lock -> unlock API round trip. */
static SemaphoreHandle_t s_mutex_u;
static bench_status port_mutex_u_create(void *ctx)
{
    (void)ctx;
    if (s_mutex_u == NULL) {
        s_mutex_u = xSemaphoreCreateMutex();
        if (s_mutex_u == NULL) {
            return BENCH_PORT;
        }
    }
    return BENCH_OK;
}
static bench_status port_mutex_u_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_mutex_u == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    uint64_t start, end;
    bench_status status = port_now_ticks(NULL, &start);
    if (status != BENCH_OK) {
        return status;
    }
    if (xSemaphoreTake(s_mutex_u, portMAX_DELAY) != pdTRUE) {
        return BENCH_PORT;
    }
    if (xSemaphoreGive(s_mutex_u) != pdTRUE) {
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
    if (s_mutex_u != NULL) {
        vSemaphoreDelete(s_mutex_u);
        s_mutex_u = NULL;
    }
    return BENCH_OK;
}
bench_status bench_mutex_u_create(void) { return port_mutex_u_create(NULL); }
bench_status bench_mutex_u_measure(uint64_t *ticks) { return port_mutex_u_measure(NULL, ticks); }
bench_status bench_mutex_u_destroy(void) { return port_mutex_u_destroy(NULL); }

/* Case 14: allocator allocate -> free round trip (64-byte payload). Uses the
 * kernel heap (heap_4 here), so it measures the RTOS allocator, not libc. */
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
bench_status bench_alloc_create(void) { return port_alloc_create(NULL); }
bench_status bench_alloc_measure(uint64_t *ticks) { return port_alloc_measure(NULL, ticks); }
bench_status bench_alloc_destroy(void) { return port_alloc_destroy(NULL); }

/* Case 15: one-shot delay accuracy. Requests one RTOS tick and reports the
 * absolute deviation of the actual elapsed DWT cycles from BENCH_HZ/1000 (the
 * nominal 1 ms), so it captures requested-vs-actual error plus wake latency. */
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
bench_status bench_sleep_create(void) { return port_sleep_create(NULL); }
bench_status bench_sleep_measure(uint64_t *ticks) { return port_sleep_measure(NULL, ticks); }
bench_status bench_sleep_destroy(void) { return port_sleep_destroy(NULL); }

/* Case 17: direct-to-task notification latency. The peer runs one priority
 * below the benchmark task, so it cannot signal before the benchmark blocks;
 * it timestamps the signal and notifies, and measure() returns the
 * signal-to-resume delta. */
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
        s_notify_signal = (uint64_t)DWT->CYCCNT;
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
    if (priority > 0) {
        priority--; /* lower: the peer must not run before we block */
    }
    if (xTaskCreate(notify_peer_entry, "bench_notify", 256, NULL, priority,
                    &s_notify_peer) != pdPASS) {
        vSemaphoreDelete(s_notify_go);
        s_notify_go = NULL;
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
    (void)ulTaskNotifyTake(pdTRUE, 0); /* drain any stale notification */
    if (xSemaphoreGive(s_notify_go) != pdPASS) {
        return BENCH_PORT;
    }
    if (ulTaskNotifyTake(pdTRUE, portMAX_DELAY) == 0) {
        return BENCH_PORT;
    }
    const uint64_t end = (uint64_t)DWT->CYCCNT;
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
bench_status bench_notify_create(void) { return port_notify_create(NULL); }
bench_status bench_notify_measure(uint64_t *ticks) { return port_notify_measure(NULL, ticks); }
bench_status bench_notify_destroy(void) { return port_notify_destroy(NULL); }

/* Case 16: one scheduler invocation with no other ready application task.
 * taskYIELD still enters the scheduler and re-selects this task, so it measures
 * scheduler bookkeeping without a stack switch. A tick that advances across the
 * window adds tick processing, so retry a few times and reject if all attempts
 * are polluted. */
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
    for (int attempt = 0; attempt < 4; ++attempt) {
        TickType_t before = xTaskGetTickCount();
        uint64_t start = (uint64_t)DWT->CYCCNT;
        taskYIELD();
        uint64_t end = (uint64_t)DWT->CYCCNT;
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
bench_status bench_sched_create(void) { return port_sched_create(NULL); }
bench_status bench_sched_measure(uint64_t *ticks) { return port_sched_measure(NULL, ticks); }
bench_status bench_sched_destroy(void) { return port_sched_destroy(NULL); }

/* Case 26: priority scheduling conformance. Four peers at base+1..base+4 all
 * block on one counting semaphore; the benchmark gives all tokens while the
 * scheduler is suspended, then resumes, so they run strictly highest first.
 * Each peer records its id and blocks on a private ack; the benchmark compares
 * the trace to priority order and returns 1 (match) or 0 (violation). Build
 * with -DPRIO_INVERT_CONTROL for the deliberate-inversion control (must report
 * 0). */
#define PRIO_N 4
static TaskHandle_t     s_prio_task[PRIO_N];
static SemaphoreHandle_t s_prio_release;
static SemaphoreHandle_t s_prio_all;
static SemaphoreHandle_t s_prio_ack;
static volatile uint32_t s_prio_n;
static volatile uint8_t  s_prio_trace[PRIO_N];

static void prio_peer_entry(void *arg)
{
    const uint8_t id = (uint8_t)(uintptr_t)arg;
    for (;;) {
        if (xSemaphoreTake(s_prio_release, portMAX_DELAY) != pdPASS) {
            continue;
        }
        if (s_prio_n < PRIO_N) {
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
    s_prio_release = xSemaphoreCreateCounting(PRIO_N, 0);
    s_prio_all     = xSemaphoreCreateCounting(PRIO_N, 0);
    s_prio_ack     = xSemaphoreCreateCounting(PRIO_N, 0);
    if (s_prio_release == NULL || s_prio_all == NULL || s_prio_ack == NULL) {
        return BENCH_PORT;
    }
    const UBaseType_t base = uxTaskPriorityGet(NULL);
    for (uint8_t i = 0; i < PRIO_N; ++i) {
#if PRIO_INVERT
        /* Control: id 0 gets the highest priority, so the observed order cannot
         * match the priority order the checker expects. */
        const UBaseType_t prio = base + 1 + (PRIO_N - 1 - i);
#else
        const UBaseType_t prio = base + 1 + i;
#endif
        if (prio >= configMAX_PRIORITIES) {
            return BENCH_PORT;
        }
        if (xTaskCreate(prio_peer_entry, "bench_prio", 256, (void *)(uintptr_t)i,
                        prio, &s_prio_task[i]) != pdPASS) {
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
    for (int i = 0; i < PRIO_N; ++i) {
        (void)xSemaphoreGive(s_prio_release);
    }
    (void)xTaskResumeAll();
    for (int i = 0; i < PRIO_N; ++i) {
        if (xSemaphoreTake(s_prio_all, portMAX_DELAY) != pdPASS) {
            return BENCH_PORT;
        }
    }
    int ok = (s_prio_n == PRIO_N);
    for (uint32_t i = 0; ok && i < PRIO_N; ++i) {
        if (s_prio_trace[i] != (uint8_t)(PRIO_N - 1 - i)) {
            ok = 0;
        }
    }
    for (int i = 0; i < PRIO_N; ++i) {
        (void)xSemaphoreGive(s_prio_ack);
    }
    *ticks = (ok != 0) ? 1u : 0u;
    return BENCH_OK;
}

static bench_status port_prio_destroy(void *ctx)
{
    (void)ctx;
    for (uint8_t i = 0; i < PRIO_N; ++i) {
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
bench_status bench_prio_create(void) { return port_prio_create(NULL); }
bench_status bench_prio_measure(uint64_t *ticks) { return port_prio_measure(NULL, ticks); }
bench_status bench_prio_destroy(void) { return port_prio_destroy(NULL); }

/* Case 21: periodic tick-ISR cost, bracketed by the port's traceISR_* hooks
 * (FreeRTOSConfig.h) around the xPortSysTickHandler body. One sample per tick,
 * so warmup+reps ticks; the active flag keeps the hooks inert otherwise. */
volatile unsigned int g_tick_entry;
volatile unsigned int g_tick_last;
volatile unsigned int g_tick_ready;
volatile unsigned int g_tick_trace_active;

static bench_status port_tick_create(void *ctx)
{
    (void)ctx;
    g_tick_ready = 0u;
    g_tick_trace_active = 1u;
    return BENCH_OK;
}

static bench_status port_tick_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    for (int i = 0; i < 3; ++i) {
        if (g_tick_ready != 0u) {
            *ticks = (uint64_t)g_tick_last;
            g_tick_ready = 0u;
            return BENCH_OK;
        }
        vTaskDelay(1); /* wait for the next SysTick */
    }
    return BENCH_PORT;
}

static bench_status port_tick_destroy(void *ctx)
{
    (void)ctx;
    g_tick_trace_active = 0u;
    return BENCH_OK;
}
bench_status bench_tick_create(void) { return port_tick_create(NULL); }
bench_status bench_tick_measure(uint64_t *ticks) { return port_tick_measure(NULL, ticks); }
bench_status bench_tick_destroy(void) { return port_tick_destroy(NULL); }

/* Case 22/23: kernel (non-idle) CPU share in parts-per-million.
 * Case 22 (load 0) accumulates the SysTick ISR cycles via the case-21 trace
 * hooks over a fixed window; while idle that is the whole kernel share.
 * Case 23 (load 25/50/75/100) runs a graded busy-spin worker and reads the
 * idle task's accounted run time: busy = window - (idle_task_time - isr_time)
 * = worker + kernel, reducing to the case-22 value at load 0. An idle hook is
 * not used because its inter-call delta counts worker spin as idle. */
#define CPU_WINDOW_MS  500u
#define CPU_PPM_SCALE  1000000ULL
#define CPU_F_HZ       520000000u
#define CPU_WORKER_PERIOD_MS 10u
volatile unsigned int g_cpu_isr_active;
volatile unsigned long long g_cpu_isr_accum;
static TaskHandle_t s_cpu_worker;
static SemaphoreHandle_t s_cpu_worker_sem;
static volatile uint32_t s_cpu_load_pct;
static volatile int s_cpu_worker_stop;
static volatile int s_cpu_worker_running;

static uint32_t cpu_idle_counter(void)
{
    static TaskStatus_t status[12];
    UBaseType_t n = uxTaskGetSystemState(status, 12, NULL);
    TaskHandle_t idle = xTaskGetIdleTaskHandle();
    for (UBaseType_t i = 0; i < n; ++i) {
        if (status[i].xHandle == idle) {
            return (uint32_t)status[i].ulRunTimeCounter;
        }
    }
    return 0u;
}

static void cpu_worker_entry(void *arg)
{
    (void)arg;
    const uint32_t period_cycles = (CPU_F_HZ / 1000u) * CPU_WORKER_PERIOD_MS;
    for (;;) {
        xSemaphoreTake(s_cpu_worker_sem, portMAX_DELAY);
        s_cpu_worker_running = 1;
        while (!s_cpu_worker_stop) {
            uint32_t pct = s_cpu_load_pct;
            if (pct > 100u) {
                pct = 100u;
            }
            uint32_t spin = (uint32_t)(((uint64_t)period_cycles * pct) / 100u);
            uint32_t t0 = *(volatile unsigned int *)0xE0001004u;
            while (!s_cpu_worker_stop
                   && (uint32_t)(*(volatile unsigned int *)0xE0001004u - t0) < spin) {
            }
            uint32_t off_ms = CPU_WORKER_PERIOD_MS
                              - (CPU_WORKER_PERIOD_MS * pct) / 100u;
            if (off_ms > 0u) {
                vTaskDelay(pdMS_TO_TICKS(off_ms));
            } else {
                taskYIELD();
            }
        }
        s_cpu_worker_running = 0;
    }
}

static bench_status cpu_worker_start(uint32_t load_pct)
{
    s_cpu_load_pct = load_pct;
    s_cpu_worker_stop = 0;
    if (s_cpu_worker == NULL) {
        s_cpu_worker_sem = xSemaphoreCreateBinary();
        if (s_cpu_worker_sem == NULL) {
            return BENCH_PORT;
        }
        if (xTaskCreate(cpu_worker_entry, "bench_cpuw", 256, NULL,
                        tskIDLE_PRIORITY + 1, &s_cpu_worker) != pdPASS) {
            return BENCH_PORT;
        }
    }
    xSemaphoreGive(s_cpu_worker_sem);
    return BENCH_OK;
}

static void cpu_worker_stop(void)
{
    if (s_cpu_worker == NULL) {
        return;
    }
    s_cpu_worker_stop = 1;
    for (int i = 0; i < 1000 && s_cpu_worker_running; ++i) {
        vTaskDelay(1);
    }
}

static bench_status port_cpu_create(void *ctx)
{
    (void)ctx;
    g_cpu_isr_active = 0u;
    g_cpu_isr_accum = 0u;
    return BENCH_OK;
}

static bench_status port_cpu_measure(void *ctx, uint32_t load_pct, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    if (load_pct > 0u && cpu_worker_start(load_pct) != BENCH_OK) {
        return BENCH_PORT;
    }
    g_cpu_isr_accum = 0u;
    g_tick_trace_active = 1u;
    g_cpu_isr_active = 1u;
    uint32_t idle0 = (load_pct > 0u) ? cpu_idle_counter() : 0u;
    unsigned int start = *(volatile unsigned int *)0xE0001004u;
    vTaskDelay(pdMS_TO_TICKS(CPU_WINDOW_MS));
    unsigned int total = *(volatile unsigned int *)0xE0001004u - start;
    uint32_t idle1 = (load_pct > 0u) ? cpu_idle_counter() : 0u;
    g_cpu_isr_active = 0u;
    g_tick_trace_active = 0u;
    if (load_pct > 0u) {
        cpu_worker_stop();
    }
    if (total == 0u) {
        return BENCH_PORT;
    }
    if (load_pct == 0u) {
        *ticks = (uint64_t)((g_cpu_isr_accum * CPU_PPM_SCALE)
                            / (unsigned long long)total);
        return BENCH_OK;
    }
    uint32_t idle_delta = idle1 - idle0;
    unsigned long long true_idle = idle_delta > g_cpu_isr_accum
                                   ? (unsigned long long)idle_delta - g_cpu_isr_accum
                                   : 0u;
    unsigned long long busy = (unsigned long long)total > true_idle
                              ? (unsigned long long)total - true_idle : 0u;
    *ticks = (busy * CPU_PPM_SCALE) / (unsigned long long)total;
    return BENCH_OK;
}

static bench_status port_cpu_destroy(void *ctx)
{
    (void)ctx;
    if (s_cpu_worker != NULL) {
        cpu_worker_stop();
        vTaskDelete(s_cpu_worker);
        s_cpu_worker = NULL;
    }
    if (s_cpu_worker_sem != NULL) {
        vSemaphoreDelete(s_cpu_worker_sem);
        s_cpu_worker_sem = NULL;
    }
    g_cpu_isr_active = 0u;
    g_tick_trace_active = 0u;
    return BENCH_OK;
}
bench_status bench_cpu_create(void) { return port_cpu_create(NULL); }
bench_status bench_cpu_measure(uint32_t load_pct, uint64_t *ticks)
{
    return port_cpu_measure(NULL, load_pct, ticks);
}
bench_status bench_cpu_destroy(void) { return port_cpu_destroy(NULL); }

/* Case 24: heap fragmentation against the FreeRTOS heap_4 allocator.
 * Allocate 256-byte blocks until only ~8 KB of free heap remains (the malloc-
 * failed hook latches a fault, so never exhaust it), free every other block to
 * leave non-adjacent holes that cannot coalesce, then probe the largest single
 * allocation with a pvPortMalloc binary search. value =
 * round(1e6*(1 - largest_free_block/total_free)). Freeing all blocks coalesces
 * them, so the same probe then reads ~0 (control). */
#define FRAG_N 1024
#define FRAG_BYTES 256u
#define FRAG_MARGIN 16384u
static void *s_frag_blocks[FRAG_N];
/* Set while case 24 probes sizes that are expected to fail, so the malloc-
 * failed hook does not latch a fault on an intentional failed probe. */
volatile int g_frag_probe_active;

static bench_status port_frag_create(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < FRAG_N; ++i) {
        s_frag_blocks[i] = NULL;
    }
    return BENCH_OK;
}

static bench_status port_frag_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (ticks == NULL) {
        return BENCH_PORT;
    }
    g_frag_probe_active = 1;
    uint32_t allocated = 0;
    while (allocated < FRAG_N && xPortGetFreeHeapSize() > FRAG_MARGIN) {
        void *b = pvPortMalloc(FRAG_BYTES);
        if (b == NULL) {
            break;
        }
        s_frag_blocks[allocated++] = b;
    }
    if (allocated < 8u) {
        for (uint32_t i = 0; i < allocated; ++i) {
            vPortFree(s_frag_blocks[i]);
            s_frag_blocks[i] = NULL;
        }
        g_frag_probe_active = 0;
        return BENCH_PORT;
    }
    for (uint32_t i = 0; i < allocated; i += 2) {
        vPortFree(s_frag_blocks[i]);
        s_frag_blocks[i] = NULL;
    }
    size_t total = xPortGetFreeHeapSize();
    size_t lo = 0, hi = total;
    while (lo < hi) {
        size_t mid = lo + (hi - lo + 1) / 2;
        void *probe = pvPortMalloc(mid);
        if (probe != NULL) {
            vPortFree(probe);
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    size_t largest = lo;
    g_frag_probe_active = 0;
    for (uint32_t i = 1; i < allocated; i += 2) {
        vPortFree(s_frag_blocks[i]);
        s_frag_blocks[i] = NULL;
    }
    if (total == 0u) {
        return BENCH_PORT;
    }
    unsigned long long frag = (largest < total)
                              ? (unsigned long long)(total - largest) : 0u;
    *ticks = (frag * 1000000ULL) / (unsigned long long)total;
    return BENCH_OK;
}

static bench_status port_frag_destroy(void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < FRAG_N; ++i) {
        if (s_frag_blocks[i] != NULL) {
            vPortFree(s_frag_blocks[i]);
            s_frag_blocks[i] = NULL;
        }
    }
    return BENCH_OK;
}
bench_status bench_frag_create(void) { return port_frag_create(NULL); }
bench_status bench_frag_measure(uint64_t *ticks) { return port_frag_measure(NULL, ticks); }
bench_status bench_frag_destroy(void) { return port_frag_destroy(NULL); }

/* Case 25: maximum interrupt-disabled time proxy. TIM5 is a one-pulse probe at
 * NVIC preempt priority 6, one step below configLIBRARY_MAX_SYSCALL_INTERRUPT_
 * PRIORITY (5), so the kernel's BASEPRI critical sections mask it while a
 * priority-4 probe would not be. measure() arms it for ~50 us and returns the
 * delay beyond that period; the reported headline is the maximum sample.
 * MAXDIS_CONTROL=1 adds a calibrated ~80 us critical section as the positive
 * control, which must raise the delay to ~30 us. */
#define MAXDIS_PERIOD_TICKS 26000u       /* 50 us at 520 MHz DWT */
#define MAXDIS_CONTROL_CRIT_TICKS 41600u /* 80 us at 520 MHz */

static volatile uint32_t s_maxdis_isr_ts;
static SemaphoreHandle_t s_maxdis_sem;

void TIM5_IRQHandler(void)
{
    if ((TIM5->SR & TIM_SR_UIF) != 0u) {
        TIM5->SR = ~TIM_SR_UIF;
        s_maxdis_isr_ts = DWT->CYCCNT;
        BaseType_t woken = pdFALSE;
        (void)xSemaphoreGiveFromISR(s_maxdis_sem, &woken);
        portYIELD_FROM_ISR(woken);
    }
}

static bench_status port_maxdis_create(void *ctx)
{
    (void)ctx;
    s_maxdis_sem = xSemaphoreCreateBinary();
    if (s_maxdis_sem == NULL) {
        return BENCH_PORT;
    }
    __HAL_RCC_TIM5_CLK_ENABLE();
    TIM5->CR1  = 0;
    TIM5->PSC  = 0;
    TIM5->ARR  = (MAXDIS_PERIOD_TICKS / 4u) - 1u; /* 4999 -> 5000 counts = 50 us */
    TIM5->DIER = TIM_DIER_UIE;
    TIM5->SR   = 0;
    HAL_NVIC_SetPriority(TIM5_IRQn, 6, 0);
    HAL_NVIC_EnableIRQ(TIM5_IRQn);
    return BENCH_OK;
}

static bench_status port_maxdis_measure(void *ctx, uint64_t *ticks)
{
    (void)ctx;
    if (s_maxdis_sem == NULL || ticks == NULL) {
        return BENCH_PORT;
    }
    (void)xSemaphoreTake(s_maxdis_sem, 0); /* drain a stale give */
    const uint32_t t0 = DWT->CYCCNT;
    TIM5->CNT = 0;
    TIM5->SR  = 0;
    TIM5->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
#if MAXDIS_CONTROL
    taskENTER_CRITICAL();
    const uint32_t c0 = DWT->CYCCNT;
    while ((DWT->CYCCNT - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
    }
    taskEXIT_CRITICAL();
#endif
    if (xSemaphoreTake(s_maxdis_sem, portMAX_DELAY) != pdPASS) {
        return BENCH_PORT;
    }
    const uint32_t elapsed = s_maxdis_isr_ts - t0;
    if (elapsed < MAXDIS_PERIOD_TICKS) {
        return BENCH_CORRUPT;
    }
    *ticks = (uint64_t)(elapsed - MAXDIS_PERIOD_TICKS);
    return BENCH_OK;
}

static bench_status port_maxdis_destroy(void *ctx)
{
    (void)ctx;
    HAL_NVIC_DisableIRQ(TIM5_IRQn);
    TIM5->CR1 = 0;
    TIM5->DIER = 0;
    if (s_maxdis_sem != NULL) {
        vSemaphoreDelete(s_maxdis_sem);
        s_maxdis_sem = NULL;
    }
    return BENCH_OK;
}
bench_status bench_maxdis_create(void) { return port_maxdis_create(NULL); }
bench_status bench_maxdis_measure(uint64_t *ticks) { return port_maxdis_measure(NULL, ticks); }
bench_status bench_maxdis_destroy(void) { return port_maxdis_destroy(NULL); }

/* Rust-adapter harness primitives: the Rust core owns the timed sequence; each
 * shim keeps one kernel/hardware call. */
uint32_t bench_rust_nested_run(uint32_t pend_h)
{
    return nested_run(pend_h);
}
int bench_rust_isr_arm(void)
{
    TIM2->CR1 = 0;
    TIM2->CNT = 0;
    TIM2->SR  = 0;
    TIM2->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
    return 0;
}
int bench_rust_isr_wait(void)
{
    return (xSemaphoreTake(s_isr_sem, portMAX_DELAY) != pdPASS) ? (int)BENCH_PORT : 0;
}
uint64_t bench_rust_isr_entry(void)
{
    return s_isr_entry;
}
int bench_rust_trig_drain(void)
{
    (void)xSemaphoreTake(s_trig_sem, 0);
    return 0;
}
uint64_t bench_rust_trig_fire(void)
{
    s_trig_state = 1;
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    uint64_t t0 = (uint64_t)DWT->CYCCNT;
    NVIC_SetPendingIRQ(EXTI0_IRQn);
    __set_PRIMASK(primask);
    return t0;
}
int bench_rust_trig_wait(void)
{
    if (xSemaphoreTake(s_trig_sem, portMAX_DELAY) != pdPASS) {
        return (int)BENCH_PORT;
    }
    return (s_trig_state != 2) ? (int)BENCH_CORRUPT : 0;
}
uint64_t bench_rust_trig_entry(void)
{
    return s_trig_entry;
}
uint64_t bench_rust_maxdis_begin(void)
{
    (void)xSemaphoreTake(s_maxdis_sem, 0);
    return (uint64_t)DWT->CYCCNT;
}
int bench_rust_maxdis_arm(void)
{
    TIM5->CNT = 0;
    TIM5->SR  = 0;
    TIM5->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
    return 0;
}
int bench_rust_maxdis_wait(uint64_t t0)
{
    (void)t0;
    return (xSemaphoreTake(s_maxdis_sem, portMAX_DELAY) != pdPASS) ? (int)BENCH_PORT : 0;
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
    if (s_pjit_sem == NULL || ticks == NULL) {
        return (int)BENCH_PORT;
    }
    uint64_t wake = (uint64_t)DWT->CYCCNT;
    uint32_t n = s_pjit_n;
    s_pjit_n = n + 1U;
    uint32_t rel = s_pjit_releases;
    uint32_t delta = rel - s_pjit_last_releases;
    s_pjit_last_releases = rel;
    if (delta > 1U) {
        return (int)BENCH_DEADLINE_MISS;
    }
    uint64_t ideal = s_pjit_r0 + ((uint64_t)(rel - 1U) * (uint64_t)PJIT_P_CYCLES);
    if (wake < ideal) {
        return (int)BENCH_CORRUPT;
    }
    *ticks = wake - ideal;
    return 0;
}


/* Rust-adapter tick-cost and heap-probe primitives. */
int bench_rust_tick_take(uint64_t *out)
{
    if (g_tick_ready != 0u && out != NULL) {
        *out = (uint64_t)g_tick_last;
        g_tick_ready = 0u;
        return 1;
    }
    return 0;
}
void bench_rust_frag_probe(int active)
{
    g_frag_probe_active = active;
}

void **bench_rust_frag_blocks(void)
{
    return s_frag_blocks;
}

/* Rust-adapter CPU-share primitives: the Rust core owns the window loop and
 * the parts-per-million arithmetic; these keep one kernel call each. */
int bench_rust_cpu_start(uint32_t load_pct)
{
    return (int)cpu_worker_start(load_pct);
}
void bench_rust_cpu_arm(void)
{
    g_cpu_isr_accum = 0u;
    g_tick_trace_active = 1u;
    g_cpu_isr_active = 1u;
}
uint32_t bench_rust_cpu_idle(void)
{
    return cpu_idle_counter();
}
unsigned long long bench_rust_cpu_accum(void)
{
    return g_cpu_isr_accum;
}
void bench_rust_cpu_disarm(void)
{
    g_cpu_isr_active = 0u;
    g_tick_trace_active = 0u;
}
void bench_rust_cpu_stop(void)
{
    cpu_worker_stop();
}

/* ------------------------------------------------------------- sample sink */

typedef struct {
    int      case_id;    /* protocol case ID (0 or 1) */
    uint32_t index;      /* sample index within the case */
    uint64_t ticks;      /* measured ticks */
    uint32_t variant;    /* schema-v2 discriminator; 0 for plain cases */
} sample_t;

static sample_t s_samples[BENCH_MAX_SAMPLES] __attribute__((section(".ccmram")));
static size_t   s_sample_count = 0;
static char     s_run_id[BENCH_RUN_ID_MAX + 1];
static int      s_flush_failed = 0;

/* Map a bench_case to its wire protocol ID. */
static int sink_case_id(bench_case c)
{
    switch (c) {
        case BENCH_QUEUE_ROUNDTRIP: return 1;
        case BENCH_TASK_HANDOFF:    return 2;
        case BENCH_PERIODIC_JITTER: return 3;
        case BENCH_ISR_TO_TASK:     return 4;
        case BENCH_YIELD_ROUNDTRIP: return 5;
        case BENCH_PREEMPT_LATENCY: return 6;
        case BENCH_IPC_PINGPONG:    return 7;
        case BENCH_MUTEX_PRIORITY:  return 8;
        case BENCH_TIMER_CALLBACK:  return 9;
        case BENCH_TRIGGER_TO_ISR:  return 10;
        case BENCH_PERIODIC_JITTER_LOADED: return 11;
        case BENCH_SEMAPHORE_LATENCY: return 12;
        case BENCH_MUTEX_UNCONTENDED: return 13;
        case BENCH_ALLOC_LATENCY:     return 14;
        case BENCH_SLEEP_ACCURACY:    return 15;
        case BENCH_NOTIFICATION_LATENCY: return 17;
        case BENCH_SCHEDULER_OVERHEAD: return 16;
        case BENCH_PRIORITY_SCHEDULING_CORRECTNESS: return 26;
        case BENCH_TICK_OVERHEAD:     return 21;
        case BENCH_IDLE_CPU_OVERHEAD:        return 22;
        case BENCH_CPU_OVERHEAD_UNDER_LOAD:  return 23;
        case BENCH_HEAP_FRAGMENTATION:       return 24;
        case BENCH_MAX_INTERRUPT_DISABLED: return 25;
        case BENCH_NESTED_IRQ_OVERHEAD:   return 20;
        case BENCH_IPC_PAYLOAD_SWEEP:     return 18;
        case BENCH_IPC_THROUGHPUT:        return 19;
        case BENCH_ZERO_COPY_IPC:         return 27;
        case BENCH_COMMON_ARENA_FRAGMENTATION: return 28;
        default:                    return 0;
    }
}

/* record(): called after the timed section, once per valid sample. Appends to
 * the bounded buffer and rejects overflow (the spec forbids UART inside a
 * timed callback). */
static bench_status sink_record_impl(void *ctx, bench_case c, uint32_t variant,
                                     uint32_t index, uint64_t ticks, uint64_t hz)
{
    (void)ctx;
    (void)hz;
    if (s_sample_count >= BENCH_MAX_SAMPLES) {
        return BENCH_PORT; /* overflow: reject, do not silently drop */
    }
    s_samples[s_sample_count].case_id = sink_case_id(c);
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
        if (emit_measurement(s_run_id, s_samples[i].case_id,
                             s_samples[i].index, s_samples[i].ticks,
                             BENCH_HZ, s_samples[i].variant) != 0) {
            s_flush_failed = 1;
            return BENCH_PORT;
        }
    }
    s_sample_count = 0;
    return BENCH_OK;
}

#ifdef BENCH_LANGUAGE_RUST
/* Inverse of sink_case_id: map a wire protocol ID back to its bench_case. */
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
        case 17: return BENCH_NOTIFICATION_LATENCY;
        case 16: return BENCH_SCHEDULER_OVERHEAD;
        case 26: return BENCH_PRIORITY_SCHEDULING_CORRECTNESS;
        case 21: return BENCH_TICK_OVERHEAD;
        case 22: return BENCH_IDLE_CPU_OVERHEAD;
        case 23: return BENCH_CPU_OVERHEAD_UNDER_LOAD;
        case 24: return BENCH_HEAP_FRAGMENTATION;
        case 25: return BENCH_MAX_INTERRUPT_DISABLED;
        case 20: return BENCH_NESTED_IRQ_OVERHEAD;
        case 27: return BENCH_ZERO_COPY_IPC;
        case 18: return BENCH_IPC_PAYLOAD_SWEEP;
        case 19: return BENCH_IPC_THROUGHPUT;
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

/* ---------------------------------------------------------------- init */

static int s_initialized = 0;

void bench_init(void)
{
    if (s_initialized) {
        return;
    }
    dwt_init();
    s_initialized = 1;
}

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
        *err_msg = "cases must be 1..supported IDs";
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

    bench_sink sink = {
        .ctx = NULL,
        .record = sink_record,
        .record_variant = sink_record_variant,
        .flush = sink_flush,
    };

    /* Emit `started` before running so the host observes the run begin. */
    if (emit_started(run_id, build_id) != 0) {
        return -1;
    }

#ifdef BENCH_LANGUAGE_RUST
    /* Rust application core: native FreeRTOS queue + DWT timer via FFI. */
    int rc = 0;
    for (size_t i = 0; i < n_cases && rc == 0; i++) {
        rc = rust_bench_run_case(cases[i], warmup, reps, &sink,
                                 rust_record_bridge, rust_record_variant_bridge,
                                 rust_flush_bridge);
    }
    if (rc != 0) {
        const char *code = (rc == -1) ? "rust_invalid"
                         : (rc == -2) ? "rust_unsupported"
                         : (rc == -3) ? "rust_port"
                         : (rc == -4) ? "rust_corrupt" : "rust_failed";
        emit_error(run_id, code,
                   "rust rust_bench_run_case reported a failure");
        return -1;
    }
#else
    /* Map protocol case IDs (uint32) to the runner's bench_case enum array. */
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
        .peers_set = port_peers_set,
        .jitter_create = port_jitter_create,
        .jitter_measure = port_jitter_measure,
        .jitter_destroy = port_jitter_destroy,
        .isr_create = port_isr_create,
        .isr_measure = port_isr_measure,
        .isr_destroy = port_isr_destroy,
        .yield_create = port_yield_create,
        .yield_now = port_yield_now,
        .yield_destroy = port_yield_destroy,
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
        .pjit_set_load = port_pjit_set_load,
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
        .notify_create = port_notify_create,
        .notify_measure = port_notify_measure,
        .notify_destroy = port_notify_destroy,
        .sched_create = port_sched_create,
        .sched_measure = port_sched_measure,
        .sched_destroy = port_sched_destroy,
        .prio_create = port_prio_create,
        .prio_measure = port_prio_measure,
        .prio_destroy = port_prio_destroy,
        .tick_create = port_tick_create,
        .tick_measure = port_tick_measure,
        .tick_destroy = port_tick_destroy,
        .cpu_create = port_cpu_create,
        .cpu_measure = port_cpu_measure,
        .cpu_destroy = port_cpu_destroy,
        .frag_create = port_frag_create,
        .frag_measure = port_frag_measure,
        .frag_destroy = port_frag_destroy,
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

    /* Run case by case so a failure names the case and its runner status. */
    for (size_t i = 0; i < n_cases; i++) {
        bench_status rc = bench_run_case(&port, &sink, s_case_map[cases[i]],
                                         settings);
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
