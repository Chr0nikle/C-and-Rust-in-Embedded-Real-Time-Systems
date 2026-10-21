//! Allocation-free benchmark sequencing shared by every Rust RTOS application.
//! Native RTOS calls live in Port implementations. See benchmarks/specifications.md.
#![no_std]

/// Portable, RTOS-independent arena allocator for case 28.
pub mod arena;

/// Executable cases; future cases must first receive a cross-language specification.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Case {
    /// Two consecutive timer reads; calibration, not scheduler latency.
    TimerOverhead,
    /// Nonblocking send then receive in a single task on an initially empty queue.
    QueueRoundtrip,
    /// Two tasks and explicit synchronization; peer wake latency from signal.
    TaskHandoff,
    /// Absolute scheduled release versus measured wake delay.
    PeriodicJitter,
    /// ISR entry through the first awakened-task timestamp.
    IsrToTask,
    /// One equal-priority scheduler round trip to a peer task and back.
    YieldRoundtrip,
    /// Lower-priority release through the preempted task's first timestamp.
    PreemptLatency,
    /// Two-task request/response round trip over capacity-one queues.
    IpcPingpong,
    /// Blocking mutex lock latency with a spinning medium-priority task.
    MutexPriority,
    /// One-shot OS timer callback deviation from the nominal period.
    TimerCallback,
    /// Task-asserted minimal interrupt through the ISR-entry timestamp, with no
    /// nominal period subtracted.
    TriggerToIsr,
    /// Hardware-timed periodic release under an injected high-priority ISR load;
    /// the signed activation error against the ideal release grid.
    PeriodicJitterLoaded,
    /// Uncontended semaphore give then take API round trip.
    SemaphoreLatency,
    /// Uncontended mutex lock then unlock API round trip.
    MutexUncontended,
    /// Allocator allocate then free round trip.
    AllocLatency,
    /// One-shot delay: absolute deviation of the actual from the nominal period.
    SleepAccuracy,
    /// A peer signals a direct-to-task notification the benchmark task is blocked
    /// on; value is the signal timestamp to waiter-resume delta.
    NotificationLatency,
    /// One switch-free scheduler invocation with no other ready application task.
    SchedulerOverhead,
    /// Conformance: 1 when N released tasks run in priority order, else 0.
    PrioritySchedulingCorrectness,
    /// Periodic tick-ISR entry-to-exit cost; one sample per tick.
    TickOverhead,
    /// Probe interrupt delay beyond its programmed period; headline is the max.
    MaxInterruptDisabled,
    /// Extra elapsed time of a low-priority ISR when a higher-priority IRQ
    /// preempts it, versus the uninterrupted baseline.
    NestedIrqOverhead,
    /// Case-7 request/echo round trip at cycling payload sizes (bytes), reported
    /// per sample as the variant.
    IpcPayloadSweep,
    /// Producer → consumer transfer of a fixed byte volume; the sample time is
    /// the elapsed ticks for that volume (analysis derives bytes/s).
    IpcThroughput,
    /// One request/echo round trip over a copied path and one over a zero-copy
    /// path where only a buffer reference crosses the kernel; the sample is the
    /// copied-minus-zero-copy tick difference.
    ZeroCopyIpc,
    /// Idle-only system over a fixed wall window; the sample is the kernel
    /// (non-idle) CPU share in parts-per-million.
    IdleCpuOverhead,
    /// Graded synthetic worker load; the sample is the kernel CPU share in ppm
    /// and the load level is the reported variant.
    CpuOverheadUnderLoad,
    /// Repeated mixed-size allocate/free pattern against the kernel allocator;
    /// the sample is 1e6*(1 - largest_free_block/total_free).
    HeapFragmentation,
    /// Portable fragmentation over the shared arena in `arena.rs`; identical on
    /// every board/RTOS. Takes no port calls (portability control, not an RTOS
    /// metric). The sample is 1e6*(1 - largest_free/total_free).
    CommonArenaFragmentation,
}
/// Case 18 payload sizes (bytes), cycled across repetitions; each sample's
/// variant is its payload size.
pub const IPC_PAYLOAD_SIZES: [u32; 5] = [1, 4, 16, 64, 256];
/// Case-23 graded worker-load levels (percent), recorded as the variant.
pub const CPU_LOAD_LEVELS: [u32; 4] = [25, 50, 75, 100];
/// Case 11 deadline-miss sweep: injected-load budget as a percent of the port's
/// calibrated case-11 baseline (0 = load off). Mirrors `k_pjit_load_levels` in
/// the C runner. Ports without a load setter run at the fixed load, variant 0.
pub const PJIT_LOAD_LEVELS: [u32; 4] = [0, 100, 150, 200];
/// Task-count sweep (case 5 `yield_roundtrip`): total runnable tasks at the
/// session priority. Mirrors `k_peer_counts` in the C runner.
pub const PEER_COUNTS: [u32; 4] = [2, 8, 16, 32];
/// Case 11: a release missed while the injected load is high enough to coalesce
/// semaphores is recorded as this sentinel tick value instead of aborting the
/// run, so the miss count is the number of sentinel samples in the result.
pub const PJIT_MISS_SENTINEL: u64 = u64::MAX;
/// `zero_copy_ipc`: a sample whose copied-minus-zero-copy difference is not
/// positive carries no measured benefit, so it is recorded as this sentinel
/// instead of a silent 0. The no-benefit count is the number of sentinel samples
/// in the result, and value statistics use only the positive samples.
pub const ZCOPY_NO_BENEFIT_SENTINEL: u64 = u64::MAX;
/// Flush the sink every N recorded samples for the slow cases (throughput, CPU
/// share, fragmentation): these run a long per-sample window and flushing only
/// at case end made them look hung for ~550 s on the wire.
pub const RUNNER_FLUSH_EVERY: u32 = 100;
/// Explicit failures; an unavailable adapter cannot produce a passing sample.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// Invalid repetition count or clock rate.
    Invalid,
    /// Platform port not implemented or case unsupported.
    Unsupported,
    /// Driver/RTOS/transport failure.
    Port,
    /// Queue returned a different payload, or timer moved backwards.
    Corrupt,
    /// Case 11: a periodic release was coalesced under load (a deadline miss);
    /// recorded as `PJIT_MISS_SENTINEL` rather than aborting the run.
    DeadlineMiss,
}
/// Board and RTOS operations. All calls are bounded; no allocation in measured sections.
pub trait Port {
    /// Monotonic, extended timer value; handle native counter rollover inside the port.
    fn now_ticks(&mut self) -> Result<u64, Error>;
    /// Fixed timer rate for this run; do not assume CPU frequency equals timer frequency.
    fn ticks_per_second(&self) -> u64;
    /// Allocate/reset an empty queue with capacity one and u32 payloads, outside timing.
    fn queue_create(&mut self) -> Result<(), Error>;
    /// Nonblocking send; successful return means the payload was accepted.
    fn queue_send(&mut self, payload: u32) -> Result<(), Error>;
    /// Nonblocking receive; failed/empty receive is an error.
    fn queue_receive(&mut self) -> Result<u32, Error>;
    /// Free queue resources even if a measured operation failed.
    fn queue_destroy(&mut self) -> Result<(), Error>;
    /// Create the two tasks and their synchronization (outside timing).
    fn handoff_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Signal the peer task and return the tick at which it unblocked.
    fn handoff_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release handoff resources even after a failure.
    fn handoff_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the periodic schedule (outside timing).
    fn jitter_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Wait for the next scheduled release and return the nonnegative wake delay.
    fn jitter_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release periodic-jitter resources.
    fn jitter_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the controlled interrupt source (outside timing).
    fn isr_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Return the ISR-entry to awakened-task timestamp delta.
    fn isr_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release interrupt resources.
    fn isr_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the equal-priority peer task (outside timing).
    fn yield_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Perform one scheduler round trip to the peer and back; must not return
    /// before the peer ran, or the sample does not measure a switch.
    fn yield_now(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Release the peer task.
    fn yield_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the preemption case: high-priority task plus release path.
    fn preempt_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Release the high-priority task and return its first timestamp.
    fn preempt_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release preemption resources.
    fn preempt_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the IPC peer task and its two request/response queues (outside timing).
    fn ipc_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Send `payload`, block for the matching reply and return the round-trip ticks.
    fn ipc_measure(&mut self, _payload: u32) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release IPC resources even after a failure.
    fn ipc_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the mutex, the low-priority holder and the medium-priority spinner.
    fn mutex_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Block on the mutex held by L while M spins; return the acquisition ticks.
    fn mutex_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release mutex resources.
    fn mutex_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the one-shot timer and its completion signal.
    fn timer_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Arm the timer and return the callback deviation from the nominal period.
    fn timer_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release timer resources.
    fn timer_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the software-asserted interrupt source (outside timing).
    fn trigger_isr_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Assert the interrupt in the task and return the raw trigger-to-ISR-entry
    /// delta in ticks; no nominal period is subtracted.
    fn trigger_isr_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release trigger-interrupt resources.
    fn trigger_isr_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the hardware release timer and the injected-load ISR (outside
    /// timing).
    fn pjit_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Wait for the next hardware release and return the signed activation error
    /// (wake minus the ideal release instant) in ticks.
    fn pjit_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Case-11 sweep: set the injected-load budget to `load_pct` percent of the
    /// calibrated baseline (0 disables the load). Default: unsupported, so the
    /// case runs at its fixed load with variant 0 and no sweep.
    fn pjit_set_load(&mut self, load_pct: u32) -> Result<(), Error> {
        let _ = load_pct;
        Err(Error::Unsupported)
    }
    /// Task-count sweep (case 5): set the number of runnable peers (including
    /// the case's own peer). Default: unsupported, so case 5 runs with its fixed
    /// single peer.
    fn peers_set(&mut self, count: u32) -> Result<(), Error> {
        let _ = count;
        Err(Error::Unsupported)
    }
    /// Release the release/load timer resources.
    fn pjit_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the semaphore for the uncontended give/take round trip.
    fn sem_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// One give then take; returns the elapsed ticks.
    fn sem_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release semaphore resources.
    fn sem_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the mutex for the uncontended lock/unlock round trip.
    fn mutex_u_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// One lock then unlock; returns the elapsed ticks.
    fn mutex_u_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release uncontended-mutex resources.
    fn mutex_u_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare allocator bookkeeping for the round trip.
    fn alloc_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// One allocate then free; returns the elapsed ticks.
    fn alloc_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release allocator resources.
    fn alloc_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the delay-accuracy case.
    fn sleep_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Request the nominal delay and return the absolute deviation from it.
    fn sleep_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release delay-accuracy resources.
    fn sleep_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the notification peer and target (outside timing).
    fn notify_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Signal the blocked benchmark task and return the signal-to-resume delta.
    fn notify_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release notification resources.
    fn notify_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Prepare the switch-free scheduler-overhead case (outside timing).
    fn sched_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Perform one switch-free scheduler call and return its elapsed ticks.
    fn sched_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release scheduler-overhead resources.
    fn sched_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Create the N priority-ordered tasks (outside timing).
    fn prio_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Release the tasks together; return 1 when the run order matches priority.
    fn prio_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release priority-conformance resources.
    fn prio_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Enable the ISR trace hooks (outside timing).
    fn tick_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Wait for the next tick and return the tick-ISR entry-to-exit ticks.
    fn tick_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Disable the ISR trace hooks.
    fn tick_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Set up the masked-priority probe interrupt (outside timing).
    fn maxdis_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Arm the probe and return its delay beyond the programmed period.
    fn maxdis_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the probe interrupt resources.
    fn maxdis_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Set up the low- and higher-priority IRQ lines (outside timing).
    fn nested_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Return nested minus the uninterrupted baseline elapsed ticks.
    fn nested_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the IRQ line resources.
    fn nested_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Set up the variable-size IPC path for case 18 (outside timing).
    fn ipc_payload_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Time a full request/echo round trip carrying `payload_bytes`.
    fn ipc_payload_measure(&mut self, _payload_bytes: u32) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the variable-size IPC path resources.
    fn ipc_payload_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Set up the fixed-volume throughput transfer for case 19.
    fn tp_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Time a fixed-volume producer/consumer transfer.
    fn tp_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the throughput resources.
    fn tp_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Set up the copied and zero-copy IPC paths for case 27 (outside timing).
    fn zcopy_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Return copied minus zero-copy elapsed ticks, clamped to 0 when a sample
    /// transiently shows no benefit; payload corruption still returns Corrupt.
    fn zcopy_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the zero-copy IPC resources.
    fn zcopy_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Start the CPU-share measurement (cases 22/23).
    fn cpu_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Run `load_pct` of synthetic load over a fixed window and return the
    /// kernel (non-idle) CPU share in parts-per-million.
    fn cpu_measure(&mut self, _load_pct: u32) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Stop the CPU-share measurement.
    fn cpu_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
    /// Start the heap-fragmentation pattern (case 24).
    fn frag_create(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Run the mixed-size pattern and return fragmentation ppm.
    fn frag_measure(&mut self) -> Result<u64, Error> {
        Err(Error::Unsupported)
    }
    /// Release the fragmentation resources.
    fn frag_destroy(&mut self) -> Result<(), Error> {
        Ok(())
    }
}
/// Result consumer. Record into a bounded buffer; flush UART only between cases.
pub trait Sink {
    /// Store a sample outside its timed region. Reject overflow rather than dropping data.
    fn record(&mut self, case: Case, index: u32, ticks: u64, hz: u64) -> Result<(), Error>;
    /// Variant-aware record for cases whose samples carry a discriminator
    /// (18/23). Default ignores the variant and forwards to `record`.
    fn record_variant(
        &mut self,
        case: Case,
        _variant: u32,
        index: u32,
        ticks: u64,
        hz: u64,
    ) -> Result<(), Error> {
        self.record(case, index, ticks, hz)
    }
    /// Emit buffered samples after cleanup; transport errors fail the run.
    fn flush(&mut self) -> Result<(), Error>;
}
/// Per-case run size. Warm-up samples are discarded explicitly.
#[derive(Clone, Copy)]
pub struct Settings {
    /// Unreported warm-up iterations.
    pub warmup: u32,
    /// Reported observations (at least 1,000 for study runs).
    pub repetitions: u32,
}
/// The value each sample carries into the port call: a payload size, a load
/// level, a peer count, or a plain iteration counter for cases with no sweep.
fn sample_payload(
    case: Case,
    is_pjit_load_sweep: bool,
    is_peer_count_sweep: bool,
    iteration: u32,
) -> u32 {
    if case == Case::IpcPayloadSweep {
        IPC_PAYLOAD_SIZES[(iteration as usize) % IPC_PAYLOAD_SIZES.len()]
    } else if case == Case::CpuOverheadUnderLoad {
        CPU_LOAD_LEVELS[(iteration as usize) % CPU_LOAD_LEVELS.len()]
    } else if is_pjit_load_sweep {
        PJIT_LOAD_LEVELS[(iteration as usize) % PJIT_LOAD_LEVELS.len()]
    } else if is_peer_count_sweep {
        PEER_COUNTS[(iteration as usize) % PEER_COUNTS.len()]
    } else if case == Case::IdleCpuOverhead {
        0
    } else {
        iteration
    }
}

/// Run one timed sample of a single case. The clock is read once before the
/// workload and once after; the workload is expressed only through the port;
/// and the returned value is the difference between the two reads. Cases that
/// time their own operation delegate to a port measure() and return its delta
/// directly, before the closing clock read.
fn sample<P: Port>(port: &mut P, case: Case, payload: u32) -> Result<u64, Error> {
    if case == Case::PeriodicJitter {
        return port.jitter_measure();
    }
    if case == Case::IsrToTask {
        return port.isr_measure();
    }
    if case == Case::PreemptLatency {
        return port.preempt_measure();
    }
    if case == Case::IpcPingpong {
        return port.ipc_measure(payload);
    }
    if case == Case::MutexPriority {
        return port.mutex_measure();
    }
    if case == Case::TimerCallback {
        return port.timer_measure();
    }
    if case == Case::TriggerToIsr {
        return port.trigger_isr_measure();
    }
    if case == Case::PeriodicJitterLoaded {
        return port.pjit_measure();
    }
    if case == Case::SemaphoreLatency {
        return port.sem_measure();
    }
    if case == Case::MutexUncontended {
        return port.mutex_u_measure();
    }
    if case == Case::AllocLatency {
        return port.alloc_measure();
    }
    if case == Case::SleepAccuracy {
        return port.sleep_measure();
    }
    if case == Case::NotificationLatency {
        return port.notify_measure();
    }
    if case == Case::SchedulerOverhead {
        return port.sched_measure();
    }
    if case == Case::PrioritySchedulingCorrectness {
        return port.prio_measure();
    }
    if case == Case::TickOverhead {
        return port.tick_measure();
    }
    if case == Case::MaxInterruptDisabled {
        return port.maxdis_measure();
    }
    if case == Case::NestedIrqOverhead {
        return port.nested_measure();
    }
    if case == Case::IpcPayloadSweep {
        return port.ipc_payload_measure(payload);
    }
    if case == Case::IpcThroughput {
        return port.tp_measure();
    }
    if case == Case::ZeroCopyIpc {
        return port.zcopy_measure();
    }
    if case == Case::IdleCpuOverhead {
        return port.cpu_measure(0);
    }
    if case == Case::CpuOverheadUnderLoad {
        return port.cpu_measure(payload);
    }
    if case == Case::HeapFragmentation {
        return port.frag_measure();
    }
    if case == Case::CommonArenaFragmentation {
        return Ok(u64::from(arena::measure_static(false)));
    }
    let start_ticks = port.now_ticks()?;
    if case == Case::TaskHandoff {
        let end_ticks = port.handoff_measure()?;
        return end_ticks.checked_sub(start_ticks).ok_or(Error::Corrupt);
    }
    let received_payload = match case {
        Case::TimerOverhead => payload,
        Case::QueueRoundtrip => {
            port.queue_send(payload)?;
            port.queue_receive()?
        }
        Case::YieldRoundtrip => {
            port.yield_now()?;
            payload
        }
        Case::TaskHandoff | Case::PeriodicJitter | Case::IsrToTask | Case::PreemptLatency => {
            unreachable!()
        }
        Case::IpcPingpong => unreachable!(),
        Case::MutexPriority => unreachable!(),
        Case::TimerCallback => unreachable!(),
        Case::TriggerToIsr => unreachable!(),
        Case::PeriodicJitterLoaded => unreachable!(),
        Case::SemaphoreLatency => unreachable!(),
        Case::MutexUncontended => unreachable!(),
        Case::AllocLatency => unreachable!(),
        Case::SleepAccuracy => unreachable!(),
        Case::NotificationLatency => unreachable!(),
        Case::SchedulerOverhead => unreachable!(),
        Case::PrioritySchedulingCorrectness => unreachable!(),
        Case::TickOverhead => unreachable!(),
        Case::MaxInterruptDisabled => unreachable!(),
        Case::NestedIrqOverhead => unreachable!(),
        Case::IpcPayloadSweep => unreachable!(),
        Case::IpcThroughput => unreachable!(),
        Case::ZeroCopyIpc => unreachable!(),
        Case::IdleCpuOverhead => unreachable!(),
        Case::CpuOverheadUnderLoad => unreachable!(),
        Case::HeapFragmentation => unreachable!(),
        Case::CommonArenaFragmentation => unreachable!(),
    };
    let end_ticks = port.now_ticks()?;
    if received_payload != payload {
        return Err(Error::Corrupt);
    }
    end_ticks.checked_sub(start_ticks).ok_or(Error::Corrupt)
}
/// Run one case with cleanup on failure and UART emission outside timing.
pub fn run_case<P: Port, S: Sink>(
    port: &mut P,
    sink: &mut S,
    case: Case,
    settings: Settings,
) -> Result<(), Error> {
    let ticks_per_second = port.ticks_per_second();
    if settings.repetitions == 0 || ticks_per_second == 0 {
        return Err(Error::Invalid);
    }
    if case == Case::QueueRoundtrip {
        port.queue_create()?;
    }
    if case == Case::TaskHandoff {
        port.handoff_create()?;
    }
    if case == Case::PeriodicJitter {
        port.jitter_create()?;
    }
    if case == Case::IsrToTask {
        port.isr_create()?;
    }
    if case == Case::YieldRoundtrip {
        port.yield_create()?;
    }
    if case == Case::PreemptLatency {
        port.preempt_create()?;
    }
    if case == Case::IpcPingpong {
        port.ipc_create()?;
    }
    if case == Case::MutexPriority {
        port.mutex_create()?;
    }
    if case == Case::TimerCallback {
        port.timer_create()?;
    }
    if case == Case::TriggerToIsr {
        port.trigger_isr_create()?;
    }
    if case == Case::PeriodicJitterLoaded {
        port.pjit_create()?;
    }
    if case == Case::SemaphoreLatency {
        port.sem_create()?;
    }
    if case == Case::MutexUncontended {
        port.mutex_u_create()?;
    }
    if case == Case::AllocLatency {
        port.alloc_create()?;
    }
    if case == Case::SleepAccuracy {
        port.sleep_create()?;
    }
    if case == Case::NotificationLatency {
        port.notify_create()?;
    }
    if case == Case::SchedulerOverhead {
        port.sched_create()?;
    }
    if case == Case::PrioritySchedulingCorrectness {
        port.prio_create()?;
    }
    if case == Case::TickOverhead {
        port.tick_create()?;
    }
    if case == Case::MaxInterruptDisabled {
        port.maxdis_create()?;
    }
    if case == Case::NestedIrqOverhead {
        port.nested_create()?;
    }
    if case == Case::IpcPayloadSweep {
        port.ipc_payload_create()?;
    }
    if case == Case::IpcThroughput {
        port.tp_create()?;
    }
    if case == Case::ZeroCopyIpc {
        port.zcopy_create()?;
    }
    if case == Case::IdleCpuOverhead || case == Case::CpuOverheadUnderLoad {
        port.cpu_create()?;
    }
    if case == Case::HeapFragmentation {
        port.frag_create()?;
    }
    // Case 11 sweeps the injected load only when the port exposes the setter;
    // probing with 100% also establishes the baseline before the first sample.
    let is_pjit_load_sweep = case == Case::PeriodicJitterLoaded && port.pjit_set_load(100).is_ok();
    // Task-count sweep tags case 5; probing with 1 leaves only the case's own peer.
    let is_peer_count_sweep = case == Case::YieldRoundtrip && port.peers_set(1).is_ok();
    // Every case streams incrementally: a single case-end flush is a ~1000-frame
    // burst that wedges the ESP32-C6 USB-Serial-JTAG console TX. The flush is
    // outside the timed region, so it does not perturb the measured values.
    let outcome = (|| {
        for iteration in 0..settings.warmup {
            let payload = sample_payload(case, is_pjit_load_sweep, is_peer_count_sweep, iteration);
            if is_pjit_load_sweep {
                port.pjit_set_load(payload)?;
            }
            if is_peer_count_sweep {
                port.peers_set(payload)?;
            }
            match sample(port, case, payload) {
                Ok(_) => {}
                Err(Error::Corrupt) | Err(Error::DeadlineMiss)
                    if case == Case::PeriodicJitterLoaded => {}
                Err(e) => return Err(e),
            }
        }
        for iteration in 0..settings.repetitions {
            let payload = sample_payload(case, is_pjit_load_sweep, is_peer_count_sweep, iteration);
            if is_pjit_load_sweep {
                port.pjit_set_load(payload)?;
            }
            if is_peer_count_sweep {
                port.peers_set(payload)?;
            }
            let elapsed_ticks = match sample(port, case, payload) {
                Ok(t) => t,
                Err(Error::Corrupt) | Err(Error::DeadlineMiss)
                    if case == Case::PeriodicJitterLoaded =>
                {
                    PJIT_MISS_SENTINEL /* high-duty miss: count it */
                }
                Err(e) => return Err(e),
            };
            if case == Case::IpcPayloadSweep
                || case == Case::CpuOverheadUnderLoad
                || is_pjit_load_sweep
                || is_peer_count_sweep
            {
                sink.record_variant(case, payload, iteration, elapsed_ticks, ticks_per_second)?;
            } else {
                sink.record(case, iteration, elapsed_ticks, ticks_per_second)?;
            }
            if (iteration + 1) % RUNNER_FLUSH_EVERY == 0 {
                sink.flush()?;
            }
        }
        Ok(())
    })();
    let cleanup = match case {
        Case::QueueRoundtrip => port.queue_destroy(),
        Case::TaskHandoff => port.handoff_destroy(),
        Case::PeriodicJitter => port.jitter_destroy(),
        Case::IsrToTask => port.isr_destroy(),
        Case::YieldRoundtrip => port.yield_destroy(),
        Case::PreemptLatency => port.preempt_destroy(),
        Case::IpcPingpong => port.ipc_destroy(),
        Case::MutexPriority => port.mutex_destroy(),
        Case::TimerCallback => port.timer_destroy(),
        Case::TriggerToIsr => port.trigger_isr_destroy(),
        Case::PeriodicJitterLoaded => port.pjit_destroy(),
        Case::SemaphoreLatency => port.sem_destroy(),
        Case::MutexUncontended => port.mutex_u_destroy(),
        Case::AllocLatency => port.alloc_destroy(),
        Case::SleepAccuracy => port.sleep_destroy(),
        Case::NotificationLatency => port.notify_destroy(),
        Case::SchedulerOverhead => port.sched_destroy(),
        Case::PrioritySchedulingCorrectness => port.prio_destroy(),
        Case::TickOverhead => port.tick_destroy(),
        Case::MaxInterruptDisabled => port.maxdis_destroy(),
        Case::NestedIrqOverhead => port.nested_destroy(),
        Case::IpcPayloadSweep => port.ipc_payload_destroy(),
        Case::IpcThroughput => port.tp_destroy(),
        Case::ZeroCopyIpc => port.zcopy_destroy(),
        Case::IdleCpuOverhead | Case::CpuOverheadUnderLoad => port.cpu_destroy(),
        Case::HeapFragmentation => port.frag_destroy(),
        _ => Ok(()),
    };
    // On failure the sink retains buffered partial evidence for the firmware session layer.
    outcome.and(cleanup)?;
    sink.flush()
}
/// Execute the declared sequence, benchmark by benchmark; stop on the first failure.
/// The firmware session emits the final completion/error record, not this pure runner.
pub fn run_suite<P: Port, S: Sink>(
    port: &mut P,
    sink: &mut S,
    cases: &[Case],
    settings: Settings,
) -> Result<(), Error> {
    if cases.is_empty() {
        return Err(Error::Invalid);
    }
    for &case in cases {
        run_case(port, sink, case, settings)?;
    }
    Ok(())
}
