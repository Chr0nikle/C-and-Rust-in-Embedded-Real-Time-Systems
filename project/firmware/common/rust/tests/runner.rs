use rtos_bench::{run_case, run_suite, Case, Error, Port, Settings, Sink};
#[derive(Default)]
struct Fake {
    clock: u64,
    value: u32,
    sends: u32,
    destroyed: bool,
    corrupt: bool,
    pjit_miss: bool,
    pjit_live: bool,
}
impl Port for Fake {
    fn now_ticks(&mut self) -> Result<u64, Error> {
        self.clock += 1;
        Ok(self.clock)
    }
    fn ticks_per_second(&self) -> u64 {
        1000
    }
    fn queue_create(&mut self) -> Result<(), Error> {
        Ok(())
    }
    fn queue_send(&mut self, v: u32) -> Result<(), Error> {
        self.value = v;
        self.sends += 1;
        Ok(())
    }
    fn queue_receive(&mut self) -> Result<u32, Error> {
        Ok(self.value + u32::from(self.corrupt))
    }
    fn queue_destroy(&mut self) -> Result<(), Error> {
        self.destroyed = true;
        Ok(())
    }
    fn pjit_create(&mut self) -> Result<(), Error> {
        self.pjit_live = true;
        Ok(())
    }
    fn pjit_measure(&mut self) -> Result<u64, Error> {
        if self.pjit_miss {
            Err(Error::DeadlineMiss)
        } else {
            Ok(0)
        }
    }
    fn pjit_destroy(&mut self) -> Result<(), Error> {
        self.pjit_live = false;
        Ok(())
    }
}
#[derive(Default)]
struct Capture {
    samples: Vec<(Case, u32, u64)>,
    flushed: usize,
}
impl Sink for Capture {
    fn record(&mut self, c: Case, i: u32, t: u64, hz: u64) -> Result<(), Error> {
        assert_eq!(hz, 1000);
        self.samples.push((c, i, t));
        Ok(())
    }
    fn flush(&mut self) -> Result<(), Error> {
        self.flushed += 1;
        Ok(())
    }
}
#[test]
fn sequence_excludes_warmup_and_cleans_up() {
    let mut port = Fake::default();
    let mut sink = Capture::default();
    run_suite(
        &mut port,
        &mut sink,
        &[Case::TimerOverhead, Case::QueueRoundtrip],
        Settings {
            warmup: 2,
            repetitions: 3,
        },
    )
    .unwrap();
    assert_eq!(
        sink.samples,
        vec![
            (Case::TimerOverhead, 0, 1),
            (Case::TimerOverhead, 1, 1),
            (Case::TimerOverhead, 2, 1),
            (Case::QueueRoundtrip, 0, 1),
            (Case::QueueRoundtrip, 1, 1),
            (Case::QueueRoundtrip, 2, 1)
        ]
    );
    assert_eq!(port.sends, 5);
    assert!(port.destroyed);
    assert_eq!(sink.flushed, 2);
}
#[test]
fn corrupt_payload_aborts_before_next_case_but_releases_queue() {
    let mut port = Fake {
        corrupt: true,
        ..Fake::default()
    };
    let mut sink = Capture::default();
    assert_eq!(
        run_suite(
            &mut port,
            &mut sink,
            &[Case::QueueRoundtrip, Case::TimerOverhead],
            Settings {
                warmup: 0,
                repetitions: 3
            }
        ),
        Err(Error::Corrupt)
    );
    assert!(port.destroyed);
    assert!(sink.samples.is_empty());
    assert_eq!(sink.flushed, 0);
}
#[test]
fn pjit_deadline_miss_is_recorded_as_sentinel_and_cleans_up() {
    let mut port = Fake {
        pjit_miss: true,
        ..Fake::default()
    };
    let mut sink = Capture::default();
    run_case(
        &mut port,
        &mut sink,
        Case::PeriodicJitterLoaded,
        Settings {
            warmup: 1,
            repetitions: 3,
        },
    )
    .unwrap();
    assert_eq!(
        sink.samples,
        vec![
            (
                Case::PeriodicJitterLoaded,
                0,
                rtos_bench::PJIT_MISS_SENTINEL
            ),
            (
                Case::PeriodicJitterLoaded,
                1,
                rtos_bench::PJIT_MISS_SENTINEL
            ),
            (
                Case::PeriodicJitterLoaded,
                2,
                rtos_bench::PJIT_MISS_SENTINEL
            ),
        ]
    );
    assert!(!port.pjit_live);
}

#[test]
fn invalid_run_cannot_succeed() {
    assert_eq!(
        run_suite(
            &mut Fake::default(),
            &mut Capture::default(),
            &[],
            Settings {
                warmup: 0,
                repetitions: 1
            }
        ),
        Err(Error::Invalid)
    );
}

/// Golden values are shared with the C test `arena_matches_golden` in
/// firmware/common/c/tests/runner.c; both must stay identical.
#[test]
fn arena_matches_c_golden() {
    assert_eq!(rtos_bench::arena::run(false), 937500);
    assert_eq!(rtos_bench::arena::run(true), 0);
}

#[test]
fn common_arena_case_is_runner_native() {
    let mut port = Fake::default();
    let mut sink = Capture::default();
    run_case(
        &mut port,
        &mut sink,
        Case::CommonArenaFragmentation,
        Settings {
            warmup: 0,
            repetitions: 2,
        },
    )
    .unwrap();
    assert_eq!(
        sink.samples,
        vec![
            (Case::CommonArenaFragmentation, 0, 937500),
            (Case::CommonArenaFragmentation, 1, 937500)
        ]
    );
}

#[test]
fn arena_stress_coalesces_and_never_overlaps() {
    use rtos_bench::arena::Arena;
    let mut a = Arena::new();
    a.init();
    let mut rng: u32 = 12345;
    let mut rnd = || {
        rng = rng.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
        rng >> 8
    };
    let mut handles = [0u32; 128];
    let mut spans = [(0u32, 0u32); 128];
    let mut live = 0usize;
    for _ in 0..4000 {
        if live < 128 && (live == 0 || rnd() % 3 != 0) {
            let sz = 8 + (rnd() % 504);
            let h = a.alloc(sz);
            if h != 0 {
                let b0 = h - 8;
                let b1 = b0 + a.block_size(b0);
                for s in spans.iter().take(live) {
                    assert!(b0 >= s.1 || s.0 >= b1, "overlap");
                }
                handles[live] = h;
                spans[live] = (b0, b1);
                live += 1;
            }
        } else if live > 0 {
            let k = (rnd() as usize) % live;
            a.free(handles[k]);
            handles[k] = handles[live - 1];
            spans[k] = spans[live - 1];
            live -= 1;
        }
    }
    while live > 0 {
        live -= 1;
        a.free(handles[live]);
    }
    assert_eq!(a.stats(), (8184, 8184));
}
