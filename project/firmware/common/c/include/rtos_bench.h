#ifndef RTOS_BENCH_H
#define RTOS_BENCH_H
/* Same semantics as common/rust; native adapters supply all OS calls.
 * Every function pointer for an advertised case must be non-NULL; the runner
 * rejects a requested case whose pointers are unset. All OS operations must
 * have bounded completion. */
#include <stdint.h>
#include <stddef.h>

/* Elapsed ticks from a free-running 32-bit counter. The subtraction is modular
 * and is valid only while the measured interval is shorter than one counter
 * period; callers must document that bound. */
static inline uint64_t bench_elapsed_u32(uint32_t start, uint32_t end)
{
	return (uint64_t)(uint32_t)(end - start);
}

typedef enum { BENCH_OK, BENCH_INVALID, BENCH_UNSUPPORTED, BENCH_PORT, BENCH_CORRUPT,
               /* Case 11 deadline miss (coalesced release under load): the sample
                * is recorded as BENCH_PJIT_MISS_SENTINEL, distinct from a data
                * corruption that must abort the run. */
               BENCH_DEADLINE_MISS } bench_status;
typedef enum { BENCH_TIMER_OVERHEAD, BENCH_QUEUE_ROUNDTRIP, BENCH_TASK_HANDOFF, BENCH_PERIODIC_JITTER, BENCH_ISR_TO_TASK, BENCH_YIELD_ROUNDTRIP, BENCH_PREEMPT_LATENCY, BENCH_IPC_PINGPONG, BENCH_MUTEX_PRIORITY, BENCH_TIMER_CALLBACK, BENCH_TRIGGER_TO_ISR, BENCH_PERIODIC_JITTER_LOADED, BENCH_SEMAPHORE_LATENCY, BENCH_MUTEX_UNCONTENDED, BENCH_ALLOC_LATENCY, BENCH_SLEEP_ACCURACY, BENCH_NOTIFICATION_LATENCY, BENCH_SCHEDULER_OVERHEAD, BENCH_PRIORITY_SCHEDULING_CORRECTNESS, BENCH_TICK_OVERHEAD, BENCH_MAX_INTERRUPT_DISABLED, BENCH_NESTED_IRQ_OVERHEAD, BENCH_IPC_PAYLOAD_SWEEP,
BENCH_IPC_THROUGHPUT, BENCH_ZERO_COPY_IPC, BENCH_IDLE_CPU_OVERHEAD,
BENCH_CPU_OVERHEAD_UNDER_LOAD, BENCH_HEAP_FRAGMENTATION,
BENCH_COMMON_ARENA_FRAGMENTATION } bench_case;
/* Case 11: a release missed while the injected load is high enough to coalesce
 * semaphores is recorded as this sentinel tick value instead of aborting the
 * run, so the miss count is the number of sentinel samples in the result. */
#define BENCH_PJIT_MISS_SENTINEL UINT64_MAX
/* `zero_copy_ipc`: a sample whose copied-minus-zero-copy difference is not
 * positive carries no measured benefit, so it is recorded as this sentinel
 * instead of a silent 0. The no-benefit count is the number of sentinel
 * samples in the result, and value statistics use only the positive samples. */
#define BENCH_ZCOPY_NO_BENEFIT_SENTINEL UINT64_MAX
typedef struct { uint32_t warmup; uint32_t repetitions; } bench_settings;
typedef struct {
    void *ctx;
    /* Monotonic extended ticks; handle native counter rollover in the adapter. */
    bench_status (*now_ticks)(void *, uint64_t *);
    uint64_t ticks_per_second;
    /* Queue capacity one, u32 payload, initially empty; allocate outside timing. */
    bench_status (*queue_create)(void *);
    bench_status (*queue_send)(void *, uint32_t);
    bench_status (*queue_receive)(void *, uint32_t *);
    bench_status (*queue_destroy)(void *);
    /* Two tasks + explicit synchronization. Create/tear down outside timing;
     * measure() signals the peer and returns the tick at which it unblocked. */
    bench_status (*handoff_create)(void *);
    bench_status (*handoff_measure)(void *, uint64_t *ticks);
    bench_status (*handoff_destroy)(void *);
    /* Absolute scheduled release vs measured wake; measure() returns the
     * nonnegative wake delay relative to the expected release (in ticks). */
    bench_status (*jitter_create)(void *);
    bench_status (*jitter_measure)(void *, uint64_t *ticks);
    bench_status (*jitter_destroy)(void *);
    /* Controlled interrupt source; measure() returns the ISR-entry to awakened-
     * task timestamp delta in ticks. */
    bench_status (*isr_create)(void *);
    bench_status (*isr_measure)(void *, uint64_t *ticks);
    bench_status (*isr_destroy)(void *);
    /* Equal-priority peer task for the context-switch case. Create/destroy run
     * outside timing; yield_now() performs one scheduler round trip to the peer
     * and back, so it must not return before the peer has run. */
    bench_status (*yield_create)(void *);
    bench_status (*yield_now)(void *);
    bench_status (*yield_destroy)(void *);
    /* High-priority task blocked on a semaphore for the preemption case.
     * measure() timestamps immediately before the lower-priority task releases
     * it and returns the preempted task's first tick. */
    bench_status (*preempt_create)(void *);
    bench_status (*preempt_measure)(void *, uint64_t *);
    bench_status (*preempt_destroy)(void *);
    /* Inter-task request/response (case 7). A peer task echoes each request over
     * two queues; measure() sends `value`, blocks for the matching reply and
     * returns the elapsed ticks for the full round trip (two context switches).
     * The port must return BENCH_CORRUPT if the reply payload differs. */
    bench_status (*ipc_create)(void *);
    bench_status (*ipc_measure)(void *, uint32_t, uint64_t *);
    bench_status (*ipc_destroy)(void *);
    /* Priority-inheritance mutex (case 8). Three tasks: L holds a mutex, the
     * benchmark task H blocks on it, and a medium task M spins. measure()
     * returns H's lock-attempt to acquisition latency; with inheritance L
     * inherits H's priority and releases promptly, without it H waits out M's
     * bounded spin. */
    bench_status (*mutex_create)(void *);
    bench_status (*mutex_measure)(void *, uint64_t *);
    bench_status (*mutex_destroy)(void *);
    /* OS timer callback dispatch (case 9). measure() arms a one-shot RTOS timer
     * for a nominal period, records the callback's own timestamp and returns the
     * absolute deviation from the nominal period in ticks (period error plus
     * callback dispatch latency). The callback context is RTOS-specific and is
     * documented in the specification. */
    bench_status (*timer_create)(void *);
    bench_status (*timer_measure)(void *, uint64_t *);
    bench_status (*timer_destroy)(void *);
    /* Software-asserted minimal interrupt (case 10). measure() timestamps in the
     * task immediately before asserting a dedicated interrupt and returns the
     * raw trigger-to-ISR-entry delta in ticks. No nominal period is subtracted
     * (the request is asserted, not delayed by a programmed timer); interrupts
     * are masked across the read/assert window so task preemption cannot pollute
     * the sample. An adapter that omits these pointers simply does not advertise
     * case 10. */
    bench_status (*trigger_isr_create)(void *);
    bench_status (*trigger_isr_measure)(void *, uint64_t *ticks);
    bench_status (*trigger_isr_destroy)(void *);
    /* Periodic release under injected high-priority ISR load (case 11). An
     * adapter-owned auto-reload hardware timer releases the benchmark task at an
     * exact integer period P (in port ticks), so the schedule is independent of
     * the RTOS tick; a second adapter timer injects a cycle-calibrated busy-loop
     * ISR at a non-harmonic interval. measure() blocks for the next release and
     * returns the signed activation error (wake - (R0 + n*P)) in fine ticks, with
     * no first-differencing, so constant release latency is retained. An adapter
     * that omits these pointers simply does not advertise case 11. */
    bench_status (*pjit_create)(void *);
    bench_status (*pjit_measure)(void *, uint64_t *ticks);
    bench_status (*pjit_destroy)(void *);
    /* Optional case-11 deadline-miss sweep hook: set the injected-load budget to
     * `load_pct` percent of the calibrated baseline (0 disables the load) before
     * each sample. When NULL, case 11 runs at its fixed load with variant 0. */
    bench_status (*pjit_set_load)(void *, uint32_t load_pct);
    /* Optional task-count sweep hook: set the number of runnable peer tasks
     * (including the measured case's own peer) before each sample. Extra peers
     * must compete on the ready list but never touch the measured primitive, and
     * the measured handoff still targets its original peer. NULL = no sweep. */
    bench_status (*peers_set)(void *, uint32_t count);
    /* Uncontended synchronization and allocator API round trips (cases 12-14).
     * Each measure() times its own operation and returns the elapsed ticks in the
     * same monotonic domain as now_ticks(); create/destroy run outside timing. An
     * adapter that omits a triple simply does not advertise that case. */
    bench_status (*sem_create)(void *);
    bench_status (*sem_measure)(void *, uint64_t *ticks);
    bench_status (*sem_destroy)(void *);
    bench_status (*mutex_u_create)(void *);
    bench_status (*mutex_u_measure)(void *, uint64_t *ticks);
    bench_status (*mutex_u_destroy)(void *);
    bench_status (*alloc_create)(void *);
    bench_status (*alloc_measure)(void *, uint64_t *ticks);
    bench_status (*alloc_destroy)(void *);
    /* One-shot sleep/delay accuracy (case 15). measure() requests a nominal delay,
     * records the actual elapsed ticks and returns the absolute deviation from the
     * nominal period (requested-vs-actual error plus wake latency). */
    bench_status (*sleep_create)(void *);
    bench_status (*sleep_measure)(void *, uint64_t *ticks);
    bench_status (*sleep_destroy)(void *);
    /* Direct-to-task notification / event latency (case 17). Create a peer that
     * waits to be released; measure() releases it, blocks on the notification the
     * peer signals and returns the signal timestamp to waiter-resume delta in
     * ticks. The peer must be unable to signal before the benchmark task blocks,
     * and the adapter must document the native primitive when it is not a direct
     * task notification. An adapter that omits a triple does not advertise 17. */
    bench_status (*notify_create)(void *);
    bench_status (*notify_measure)(void *, uint64_t *ticks);
    bench_status (*notify_destroy)(void *);
    /* Scheduler invocation with no ready peer (case 16). create/destroy run
     * outside timing; measure() performs one switch-free scheduler call and
     * returns the elapsed ticks. The port must reject a sample if the system
     * tick advanced across the call. */
    bench_status (*sched_create)(void *);
    bench_status (*sched_measure)(void *, uint64_t *ticks);
    bench_status (*sched_destroy)(void *);
    /* Priority scheduling conformance (case 26). create builds N tasks at
     * distinct priorities; measure() releases them together and returns 1 when
     * the observed run order matches priority order, else 0. The value is a
     * conformance flag, not a duration; analysis must not normalize it. */
    bench_status (*prio_create)(void *);
    bench_status (*prio_measure)(void *, uint64_t *ticks);
    bench_status (*prio_destroy)(void *);
    /* Periodic tick-ISR cost (case 21). create/destroy enable/disable the port's
     * ISR trace hooks outside timing; measure() waits for the next tick and
     * returns the tick-ISR entry-to-exit ticks (the tick body, excluding the
     * PendSV switch). One sample per tick. A port without entry/exit trace hooks
     * returns BENCH_UNSUPPORTED. */
    bench_status (*tick_create)(void *);
    bench_status (*tick_measure)(void *, uint64_t *ticks);
    bench_status (*tick_destroy)(void *);
    /* Maximum interrupt-disabled time proxy (case 25). create/destroy set up a
     * probe interrupt whose priority is masked by the kernel's critical-
     * section mask; measure() arms it, waits for it and returns the observed
     * delay beyond the programmed period (empty when the probe fired on time).
     * The headline is the maximum sample over the run, not the mean. A port
     * with no such probe returns BENCH_UNSUPPORTED. */
    bench_status (*maxdis_create)(void *);
    bench_status (*maxdis_measure)(void *, uint64_t *ticks);
    bench_status (*maxdis_destroy)(void *);
    /* Nested interrupt overhead (case 20). create/destroy own a low- and a
     * higher-priority IRQ line; measure() runs a calibrated low-priority ISR
     * busy-wait, optionally pends the higher IRQ during it, and returns the
     * extra elapsed time versus the uninterrupted baseline. The adapter owns
     * the two IRQ lines and the calibration; a port without two maskable lines
     * returns BENCH_UNSUPPORTED. */
    bench_status (*nested_create)(void *);
    bench_status (*nested_measure)(void *, uint64_t *ticks);
    bench_status (*nested_destroy)(void *);
    /* IPC payload sweep (case 18): send `payload_bytes` over the case-7
     * request/echo path and time the full round trip. Ports without a
     * variable-size IPC path return BENCH_UNSUPPORTED. */
    bench_status (*ipc_payload_create)(void *);
    bench_status (*ipc_payload_measure)(void *, uint32_t payload_bytes, uint64_t *ticks);
    bench_status (*ipc_payload_destroy)(void *);
    /* IPC throughput (case 19): transfer a fixed byte volume as fast as
     * possible; measure() returns the elapsed ticks for BENCH_TP_BYTES using
     * BENCH_TP_MSG_BYTES messages (the analysis derives bytes/s). */
    bench_status (*tp_create)(void *);
    bench_status (*tp_measure)(void *, uint64_t *ticks);
    bench_status (*tp_destroy)(void *);
    /* Zero-copy IPC (case 27): one request/echo round trip over the copied path
     * and one over a zero-copy path where only a buffer reference crosses the
     * kernel; measure() returns copied minus zero-copy elapsed ticks, clamped to
     * 0 when a sample transiently shows no benefit (payload corruption still
     * returns BENCH_CORRUPT). A port with no pointer-transferring IPC or shared
     * buffer returns BENCH_UNSUPPORTED. */
    bench_status (*zcopy_create)(void *);
    bench_status (*zcopy_measure)(void *, uint64_t *ticks);
    bench_status (*zcopy_destroy)(void *);
    /* CPU share (cases 22/23): measure() runs `load_pct` (0 for case 22) of
     * synthetic worker load over a fixed wall window and returns the kernel
     * (non-idle) CPU share in parts-per-million. Case 23 records the load level
     * as the schema-v2 variant. A port with no CPU accounting returns
     * BENCH_UNSUPPORTED. */
    bench_status (*cpu_create)(void *);
    bench_status (*cpu_measure)(void *, uint32_t load_pct, uint64_t *ticks);
    bench_status (*cpu_destroy)(void *);
    /* Heap fragmentation (case 24): run a repeated mixed-size allocate/free
     * pattern against the kernel allocator and return
     * round(1e6 * (1 - largest_free_block / total_free)). A port with no
     * kernel allocator query returns BENCH_UNSUPPORTED. */
    bench_status (*frag_create)(void *);
    bench_status (*frag_measure)(void *, uint64_t *ticks);
    bench_status (*frag_destroy)(void *);
    /* Portable fragmentation (case 28) is computed entirely inside the shared
     * runner over bench_arena.h; it takes no port pointers and every port that
     * advertises it reports the identical value. No create/destroy needed. */
} bench_port;
typedef struct {
    void *ctx;
    /* Buffer after the timed section, reject overflow; do not transmit UART here. */
    bench_status (*record)(void *, bench_case, uint32_t, uint64_t, uint64_t);
    /* Variant-aware record (schema v2), used for cases whose samples carry a
     * discriminator (18/23). When NULL the runner falls back to record() and the
     * variant is not reported. */
    bench_status (*record_variant)(void *, bench_case, uint32_t variant, uint32_t index, uint64_t, uint64_t);
    bench_status (*flush)(void *);
} bench_sink;
/* On error, release queue and preserve partial buffered evidence for the session layer. */
bench_status bench_run_case(const bench_port *, const bench_sink *, bench_case, bench_settings);
bench_status bench_run_suite(const bench_port *, const bench_sink *, const bench_case *, size_t, bench_settings);
#endif
