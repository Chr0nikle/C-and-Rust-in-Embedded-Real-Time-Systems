/* Workload control flow must stay equivalent to common/rust/src/lib.rs. */
#include "rtos_bench.h"
#include "bench_arena.h"

/* Case 18 payload sizes (bytes), cycled across repetitions so each sample
 * carries its size as the reported variant. */
static const uint32_t k_ipc_payload_sizes[] = {1, 4, 16, 64, 256};
#define K_IPC_PAYLOAD_N (sizeof(k_ipc_payload_sizes) / sizeof(k_ipc_payload_sizes[0]))

/* Case 23 graded worker load levels (percent), cycled so each sample carries its
 * level as the reported variant. Case 22 uses a single 0% level. */
static const uint32_t k_cpu_load_levels[] = {25, 50, 75, 100};
#define K_CPU_LOAD_N (sizeof(k_cpu_load_levels) / sizeof(k_cpu_load_levels[0]))

/* Case 11 deadline-miss sweep: injected-load budget as a percent of the port's
 * calibrated case-11 baseline (0 = load off). Cycled so each sample carries its
 * level as the reported variant. Ports without pjit_set_load run at the fixed
 * load with variant 0 (no sweep). */
static const uint32_t k_pjit_load_levels[] = {0, 100, 150, 200};
#define K_PJIT_LOAD_N (sizeof(k_pjit_load_levels) / sizeof(k_pjit_load_levels[0]))

/* Task-count sweep: runnable peer tasks (including the measured case's own
 * peer) for case 2. Cycled so each sample carries the count as the variant;
 * ports without peers_set run with the fixed single peer and no sweep. */
static const uint32_t k_peer_counts[] = {2, 8, 16, 32};
#define K_PEER_N (sizeof(k_peer_counts) / sizeof(k_peer_counts[0]))

/* Flush the sink every N recorded samples for the slow cases (throughput, CPU
 * share, fragmentation): these run a long per-sample window and flushing only
 * at case end made them look hung for ~550 s on the wire. */
#define RUNNER_FLUSH_EVERY 100u

/* The value each sample carries into the port call: a payload size, a load
 * level, a peer count, or a plain iteration counter for cases with no sweep.
 * The same value is reported back as the sample's variant. */
static uint32_t sample_value(bench_case case_id, const bench_port *port,
                             uint32_t iteration) {
    if (case_id == BENCH_IPC_PAYLOAD_SWEEP)
        return k_ipc_payload_sizes[iteration % K_IPC_PAYLOAD_N];
    if (case_id == BENCH_CPU_OVERHEAD_UNDER_LOAD)
        return k_cpu_load_levels[iteration % K_CPU_LOAD_N];
    if (case_id == BENCH_PERIODIC_JITTER_LOADED && port->pjit_set_load)
        return k_pjit_load_levels[iteration % K_PJIT_LOAD_N];
    if (case_id == BENCH_YIELD_ROUNDTRIP && port->peers_set)
        return k_peer_counts[iteration % K_PEER_N];
    if (case_id == BENCH_IDLE_CPU_OVERHEAD)
        return 0u;
    return iteration;
}

/* Run one timed sample of a single case. The clock is read once before the
 * workload and once after; the workload is expressed only through the port;
 * and the returned value is the difference between the two reads. Cases that
 * time their own operation delegate to a port measure() and return its delta
 * directly, before the closing clock read. */
static bench_status sample(const bench_port *port, bench_case case_id,
                           uint32_t payload, uint64_t *elapsed_ticks) {
    uint64_t start_ticks, end_ticks;
    uint32_t received_payload = payload;
    bench_status status = port->now_ticks(port->ctx, &start_ticks);
    if (status != BENCH_OK) return status;

    if (case_id == BENCH_QUEUE_ROUNDTRIP) {
        status = port->queue_send(port->ctx, payload);
        if (status != BENCH_OK) return status;
        status = port->queue_receive(port->ctx, &received_payload);
        if (status != BENCH_OK) return status;
    } else if (case_id == BENCH_TASK_HANDOFF) {
        /* measure() signals the peer and returns the peer's wake tick. */
        status = port->handoff_measure(port->ctx, &end_ticks);
        if (status != BENCH_OK) return status;
        if (end_ticks < start_ticks) return BENCH_CORRUPT;
        *elapsed_ticks = end_ticks - start_ticks;
        return BENCH_OK;
    } else if (case_id == BENCH_PERIODIC_JITTER) {
        /* measure() returns the wake delay relative to the scheduled release. */
        return port->jitter_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_ISR_TO_TASK) {
        /* measure() returns the ISR-entry to awakened-task delta. */
        return port->isr_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_PREEMPT_LATENCY) {
        /* measure() returns the release to preempted-task delta. */
        return port->preempt_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_IPC_PINGPONG) {
        /* measure() sends, blocks for the matching reply and times the round trip. */
        return port->ipc_measure(port->ctx, payload, elapsed_ticks);
    } else if (case_id == BENCH_MUTEX_PRIORITY) {
        /* measure() returns the blocking-lock to acquisition latency. */
        return port->mutex_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_TIMER_CALLBACK) {
        /* measure() arms the timer and returns the callback deviation from nominal. */
        return port->timer_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_TRIGGER_TO_ISR) {
        /* measure() asserts the interrupt and returns the raw trigger-to-entry delta. */
        return port->trigger_isr_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_PERIODIC_JITTER_LOADED) {
        /* measure() waits for the next hardware-timed release and returns the
         * signed activation error versus the ideal release grid. */
        return port->pjit_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_SEMAPHORE_LATENCY) {
        return port->sem_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_MUTEX_UNCONTENDED) {
        return port->mutex_u_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_ALLOC_LATENCY) {
        return port->alloc_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_SLEEP_ACCURACY) {
        return port->sleep_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_NOTIFICATION_LATENCY) {
        /* measure() releases the peer, blocks on the notification it signals and
         * returns the signal-to-resume delta. */
        return port->notify_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_SCHEDULER_OVERHEAD) {
        /* measure() returns the switch-free scheduler invocation cost. */
        return port->sched_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_PRIORITY_SCHEDULING_CORRECTNESS) {
        /* measure() returns 1 when the run order matches priority order, else 0. */
        return port->prio_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_TICK_OVERHEAD) {
        /* measure() waits for the next tick and returns the tick-ISR cost. */
        return port->tick_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_MAX_INTERRUPT_DISABLED) {
        /* measure() returns the probe's delay beyond its programmed period. */
        return port->maxdis_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_NESTED_IRQ_OVERHEAD) {
        /* measure() returns nested minus the uninterrupted baseline. */
        return port->nested_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_IPC_PAYLOAD_SWEEP) {
        /* payload is the payload size; measure() times the full echo round trip. */
        return port->ipc_payload_measure(port->ctx, payload, elapsed_ticks);
    } else if (case_id == BENCH_IPC_THROUGHPUT) {
        /* measure() times a fixed-volume producer/consumer transfer. */
        return port->tp_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_ZERO_COPY_IPC) {
        /* measure() returns copied-minus-zero-copy elapsed ticks. */
        return port->zcopy_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_IDLE_CPU_OVERHEAD
               || case_id == BENCH_CPU_OVERHEAD_UNDER_LOAD) {
        /* payload is the worker-load percent; measure() returns busy ppm. */
        return port->cpu_measure(port->ctx, payload, elapsed_ticks);
    } else if (case_id == BENCH_HEAP_FRAGMENTATION) {
        /* measure() runs the mixed-size pattern and returns fragmentation ppm. */
        return port->frag_measure(port->ctx, elapsed_ticks);
    } else if (case_id == BENCH_COMMON_ARENA_FRAGMENTATION) {
        /* Portable value from the shared arena; no port call, identical on every
         * board/RTOS (portability control, not an RTOS metric). The arena lives
         * in BSS so it cannot overflow a small RTOS task stack. */
        static bench_arena s_arena;
        *elapsed_ticks = bench_arena_measure(&s_arena, 0u);
        return BENCH_OK;
    } else if (case_id == BENCH_YIELD_ROUNDTRIP) {
        /* One equal-priority scheduler round trip inside the timed region. */
        status = port->yield_now(port->ctx);
        if (status != BENCH_OK) return status;
    }

    status = port->now_ticks(port->ctx, &end_ticks);
    if (status != BENCH_OK) return status;
    if (received_payload != payload || end_ticks < start_ticks) return BENCH_CORRUPT;
    *elapsed_ticks = end_ticks - start_ticks;
    return BENCH_OK;
}

bench_status bench_run_case(const bench_port *port, const bench_sink *sink,
                            bench_case case_id, bench_settings settings) {
    if (!port || !sink || !port->now_ticks || !sink->record || !sink->flush
        || !port->ticks_per_second || !settings.repetitions)
        return BENCH_INVALID;
    if (case_id > BENCH_COMMON_ARENA_FRAGMENTATION)
        return BENCH_UNSUPPORTED;

    const int is_queue = case_id == BENCH_QUEUE_ROUNDTRIP;
    const int is_handoff = case_id == BENCH_TASK_HANDOFF;
    const int is_jitter = case_id == BENCH_PERIODIC_JITTER;
    const int is_isr_to_task = case_id == BENCH_ISR_TO_TASK;
    const int is_yield = case_id == BENCH_YIELD_ROUNDTRIP;
    const int is_preempt = case_id == BENCH_PREEMPT_LATENCY;
    const int is_ipc_pingpong = case_id == BENCH_IPC_PINGPONG;
    const int is_mutex_priority = case_id == BENCH_MUTEX_PRIORITY;
    const int is_timer_callback = case_id == BENCH_TIMER_CALLBACK;
    const int is_trigger_to_isr = case_id == BENCH_TRIGGER_TO_ISR;
    const int is_periodic_jitter_loaded = case_id == BENCH_PERIODIC_JITTER_LOADED;
    const int is_semaphore = case_id == BENCH_SEMAPHORE_LATENCY;
    const int is_mutex_uncontended = case_id == BENCH_MUTEX_UNCONTENDED;
    const int is_allocator = case_id == BENCH_ALLOC_LATENCY;
    const int is_sleep_accuracy = case_id == BENCH_SLEEP_ACCURACY;
    const int is_notification = case_id == BENCH_NOTIFICATION_LATENCY;
    const int is_scheduler = case_id == BENCH_SCHEDULER_OVERHEAD;
    const int is_priority_correctness = case_id == BENCH_PRIORITY_SCHEDULING_CORRECTNESS;
    const int is_tick = case_id == BENCH_TICK_OVERHEAD;
    const int is_max_interrupt_disabled = case_id == BENCH_MAX_INTERRUPT_DISABLED;
    const int is_nested_irq = case_id == BENCH_NESTED_IRQ_OVERHEAD;
    const int is_ipc_payload = case_id == BENCH_IPC_PAYLOAD_SWEEP;
    const int is_throughput = case_id == BENCH_IPC_THROUGHPUT;
    const int is_zero_copy = case_id == BENCH_ZERO_COPY_IPC;
    const int is_idle_cpu = case_id == BENCH_IDLE_CPU_OVERHEAD;
    const int is_cpu_under_load = case_id == BENCH_CPU_OVERHEAD_UNDER_LOAD;
    const int is_fragmentation = case_id == BENCH_HEAP_FRAGMENTATION;

    /* Case 11 sweeps the injected load only when the port exposes the setter;
     * otherwise it keeps the historical fixed-load, variant-0 behaviour. */
    const int is_pjit_load_sweep = is_periodic_jitter_loaded && port->pjit_set_load != NULL;
    /* Task-count sweep tags case 5 (yield_roundtrip): extra ready tasks at the
     * session priority make the yield->resume round trip scale with task count. */
    const int is_peer_count_sweep = is_yield && port->peers_set != NULL;

    /* Every case streams incrementally: a single case-end flush is a ~1000-frame
     * burst that wedges the ESP32-C6 USB-Serial-JTAG console TX (intermittent
     * chip-level quirk). The flush happens outside the timed region, so it does
     * not perturb the measured values. */

    if (is_queue && (!port->queue_create || !port->queue_send || !port->queue_receive || !port->queue_destroy))
        return BENCH_INVALID;
    if (is_handoff && (!port->handoff_create || !port->handoff_measure || !port->handoff_destroy))
        return BENCH_INVALID;
    if (is_jitter && (!port->jitter_create || !port->jitter_measure || !port->jitter_destroy))
        return BENCH_INVALID;
    if (is_isr_to_task && (!port->isr_create || !port->isr_measure || !port->isr_destroy))
        return BENCH_INVALID;
    if (is_yield && (!port->yield_create || !port->yield_now || !port->yield_destroy))
        return BENCH_INVALID;
    if (is_preempt && (!port->preempt_create || !port->preempt_measure || !port->preempt_destroy))
        return BENCH_INVALID;
    if (is_ipc_pingpong && (!port->ipc_create || !port->ipc_measure || !port->ipc_destroy))
        return BENCH_INVALID;
    if (is_mutex_priority && (!port->mutex_create || !port->mutex_measure || !port->mutex_destroy))
        return BENCH_INVALID;
    if (is_timer_callback && (!port->timer_create || !port->timer_measure || !port->timer_destroy))
        return BENCH_INVALID;
    if (is_trigger_to_isr && (!port->trigger_isr_create || !port->trigger_isr_measure || !port->trigger_isr_destroy))
        return BENCH_INVALID;
    if (is_periodic_jitter_loaded && (!port->pjit_create || !port->pjit_measure || !port->pjit_destroy))
        return BENCH_INVALID;
    if (is_semaphore && (!port->sem_create || !port->sem_measure || !port->sem_destroy))
        return BENCH_INVALID;
    if (is_mutex_uncontended && (!port->mutex_u_create || !port->mutex_u_measure || !port->mutex_u_destroy))
        return BENCH_INVALID;
    if (is_allocator && (!port->alloc_create || !port->alloc_measure || !port->alloc_destroy))
        return BENCH_INVALID;
    if (is_sleep_accuracy && (!port->sleep_create || !port->sleep_measure || !port->sleep_destroy))
        return BENCH_INVALID;
    if (is_notification && (!port->notify_create || !port->notify_measure || !port->notify_destroy))
        return BENCH_INVALID;
    if (is_scheduler && (!port->sched_create || !port->sched_measure || !port->sched_destroy))
        return BENCH_INVALID;
    if (is_priority_correctness && (!port->prio_create || !port->prio_measure || !port->prio_destroy))
        return BENCH_INVALID;
    if (is_tick && (!port->tick_create || !port->tick_measure || !port->tick_destroy))
        return BENCH_INVALID;
    if (is_max_interrupt_disabled && (!port->maxdis_create || !port->maxdis_measure || !port->maxdis_destroy))
        return BENCH_INVALID;
    if (is_nested_irq && (!port->nested_create || !port->nested_measure || !port->nested_destroy))
        return BENCH_INVALID;
    if (is_ipc_payload && (!port->ipc_payload_create || !port->ipc_payload_measure
                           || !port->ipc_payload_destroy))
        return BENCH_UNSUPPORTED;
    if (is_throughput && (!port->tp_create || !port->tp_measure || !port->tp_destroy))
        return BENCH_UNSUPPORTED;
    if (is_zero_copy && (!port->zcopy_create || !port->zcopy_measure || !port->zcopy_destroy))
        return BENCH_UNSUPPORTED;
    if ((is_idle_cpu || is_cpu_under_load) && (!port->cpu_create || !port->cpu_measure || !port->cpu_destroy))
        return BENCH_UNSUPPORTED;
    if (is_fragmentation && (!port->frag_create || !port->frag_measure || !port->frag_destroy))
        return BENCH_UNSUPPORTED;

    bench_status status = BENCH_OK;
    if (is_queue) { status = port->queue_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_handoff) { status = port->handoff_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_jitter) { status = port->jitter_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_isr_to_task) { status = port->isr_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_yield) { status = port->yield_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_preempt) { status = port->preempt_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_ipc_pingpong) { status = port->ipc_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_mutex_priority) { status = port->mutex_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_timer_callback) { status = port->timer_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_trigger_to_isr) { status = port->trigger_isr_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_periodic_jitter_loaded) { status = port->pjit_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_semaphore) { status = port->sem_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_mutex_uncontended) { status = port->mutex_u_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_allocator) { status = port->alloc_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_sleep_accuracy) { status = port->sleep_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_notification) { status = port->notify_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_scheduler) { status = port->sched_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_priority_correctness) { status = port->prio_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_tick) { status = port->tick_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_max_interrupt_disabled) { status = port->maxdis_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_nested_irq) { status = port->nested_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_ipc_payload) { status = port->ipc_payload_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_throughput) { status = port->tp_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_zero_copy) { status = port->zcopy_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_idle_cpu || is_cpu_under_load) { status = port->cpu_create(port->ctx); if (status != BENCH_OK) return status; }
    if (is_fragmentation) { status = port->frag_create(port->ctx); if (status != BENCH_OK) return status; }

    uint64_t elapsed_ticks = 0;
    for (uint32_t iteration = 0; iteration < settings.warmup && status == BENCH_OK; ++iteration) {
        uint32_t payload = sample_value(case_id, port, iteration);
        if (is_pjit_load_sweep) {
            status = port->pjit_set_load(port->ctx, payload);
            if (status != BENCH_OK) break;
        }
        if (is_peer_count_sweep) {
            status = port->peers_set(port->ctx, payload);
            if (status != BENCH_OK) break;
        }
        status = sample(port, case_id, payload, &elapsed_ticks);
        if (is_periodic_jitter_loaded
            && (status == BENCH_CORRUPT || status == BENCH_DEADLINE_MISS))
            status = BENCH_OK; /* missed release during warmup: skip it */
    }
    for (uint32_t iteration = 0; iteration < settings.repetitions && status == BENCH_OK; ++iteration) {
        uint32_t payload = sample_value(case_id, port, iteration);
        if (is_pjit_load_sweep) {
            status = port->pjit_set_load(port->ctx, payload);
            if (status != BENCH_OK) break;
        }
        if (is_peer_count_sweep) {
            status = port->peers_set(port->ctx, payload);
            if (status != BENCH_OK) break;
        }
        status = sample(port, case_id, payload, &elapsed_ticks);
        if (is_periodic_jitter_loaded
            && (status == BENCH_CORRUPT || status == BENCH_DEADLINE_MISS)) {
            elapsed_ticks = BENCH_PJIT_MISS_SENTINEL; /* high-duty miss: count it */
            status = BENCH_OK;
        }
        if (status == BENCH_OK) {
            if ((is_ipc_payload || is_cpu_under_load || is_pjit_load_sweep || is_peer_count_sweep)
                && sink->record_variant)
                status = sink->record_variant(sink->ctx, case_id, payload, iteration,
                                              elapsed_ticks, port->ticks_per_second);
            else
                status = sink->record(sink->ctx, case_id, iteration,
                                      elapsed_ticks, port->ticks_per_second);
            if (status == BENCH_OK && ((iteration + 1) % RUNNER_FLUSH_EVERY) == 0) {
                status = sink->flush(sink->ctx);
            }
        }
    }

    if (is_queue) {
        bench_status cleanup = port->queue_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_handoff) {
        bench_status cleanup = port->handoff_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_jitter) {
        bench_status cleanup = port->jitter_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_isr_to_task) {
        bench_status cleanup = port->isr_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_yield) {
        bench_status cleanup = port->yield_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_preempt) {
        bench_status cleanup = port->preempt_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_ipc_pingpong) {
        bench_status cleanup = port->ipc_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_mutex_priority) {
        bench_status cleanup = port->mutex_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_timer_callback) {
        bench_status cleanup = port->timer_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_trigger_to_isr) {
        bench_status cleanup = port->trigger_isr_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_periodic_jitter_loaded) {
        bench_status cleanup = port->pjit_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_semaphore) {
        bench_status cleanup = port->sem_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_mutex_uncontended) {
        bench_status cleanup = port->mutex_u_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_allocator) {
        bench_status cleanup = port->alloc_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_sleep_accuracy) {
        bench_status cleanup = port->sleep_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_notification) {
        bench_status cleanup = port->notify_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_scheduler) {
        bench_status cleanup = port->sched_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_priority_correctness) {
        bench_status cleanup = port->prio_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_tick) {
        bench_status cleanup = port->tick_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_max_interrupt_disabled) {
        bench_status cleanup = port->maxdis_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_nested_irq) {
        bench_status cleanup = port->nested_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_ipc_payload) {
        bench_status cleanup = port->ipc_payload_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_throughput) {
        bench_status cleanup = port->tp_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_zero_copy) {
        bench_status cleanup = port->zcopy_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_idle_cpu || is_cpu_under_load) {
        bench_status cleanup = port->cpu_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }
    if (is_fragmentation) {
        bench_status cleanup = port->frag_destroy(port->ctx);
        if (status == BENCH_OK) status = cleanup;
    }

    if (status != BENCH_OK) return status;
    return sink->flush(sink->ctx);
}

bench_status bench_run_suite(const bench_port *port, const bench_sink *sink,
                             const bench_case *cases, size_t count,
                             bench_settings settings) {
    if (!cases || !count) return BENCH_INVALID;
    for (size_t i = 0; i < count; ++i) {
        bench_status status = bench_run_case(port, sink, cases[i], settings);
        if (status != BENCH_OK) return status;
    }
    return BENCH_OK;
}
