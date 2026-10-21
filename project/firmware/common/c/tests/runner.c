/* Native fake-port contract tests; these values are not hardware measurements. */
#include "rtos_bench.h"
#include "bench_arena.h"
#include <assert.h>
typedef struct {
    uint64_t ticks;
    uint32_t value, sends, records, flushes, handoffs, jitters, isrs;
    int destroyed, hdestroyed, jdestroyed, idestroyed, corrupt;
} fake;
static bench_status now(void *ctx, uint64_t *v) { *v = ++((fake *)ctx)->ticks; return BENCH_OK; }
static bench_status create(void *ctx) { (void)ctx; return BENCH_OK; }
static bench_status send(void *ctx, uint32_t v) { fake *f=ctx; f->value=v; ++f->sends; return BENCH_OK; }
static bench_status receive(void *ctx, uint32_t *v) { fake *f=ctx; *v=f->value+(uint32_t)f->corrupt; return BENCH_OK; }
static bench_status destroy(void *ctx) { ((fake *)ctx)->destroyed=1; return BENCH_OK; }
static bench_status hcreate(void *ctx) { (void)ctx; return BENCH_OK; }
static bench_status hmeasure(void *ctx, uint64_t *t) { fake *f=ctx; ++f->handoffs; *t=++f->ticks; return BENCH_OK; }
static bench_status hdestroy(void *ctx) { ((fake *)ctx)->hdestroyed=1; return BENCH_OK; }
static bench_status jcreate(void *ctx) { (void)ctx; return BENCH_OK; }
static bench_status jmeasure(void *ctx, uint64_t *t) { fake *f=ctx; ++f->jitters; *t=0; return BENCH_OK; }
static bench_status jdestroy(void *ctx) { ((fake *)ctx)->jdestroyed=1; return BENCH_OK; }
static bench_status icreate(void *ctx) { (void)ctx; return BENCH_OK; }
static bench_status imeasure(void *ctx, uint64_t *t) { fake *f=ctx; ++f->isrs; *t=0; return BENCH_OK; }
static bench_status idestroy(void *ctx) { ((fake *)ctx)->idestroyed=1; return BENCH_OK; }
static bench_status record(void *ctx, bench_case c, uint32_t i, uint64_t t, uint64_t hz) {
    fake *f=ctx;
    assert(i == f->records%3);
    assert((c == BENCH_PERIODIC_JITTER || c == BENCH_ISR_TO_TASK) ? (t==0) : (t==1));
    assert(hz==1000);
    ++f->records;
    return BENCH_OK;
}
static bench_status flush(void *ctx) { ++((fake *)ctx)->flushes; return BENCH_OK; }
/* Golden values are shared with the Rust test `arena_matches_c_golden`; the two
 * implementations must stay byte-identical. */
static uint64_t s_arena_case_value;
static uint32_t s_arena_case_records;
static bench_status record_arena(void *ctx, bench_case c, uint32_t i, uint64_t t, uint64_t hz) {
    (void)ctx;
    (void)i;
    (void)hz;
    assert(c == BENCH_COMMON_ARENA_FRAGMENTATION);
    s_arena_case_value = t;
    ++s_arena_case_records;
    return BENCH_OK;
}
static uint32_t s_rng = 12345u;
static uint32_t arena_rnd(void) {
    s_rng = s_rng * 1664525u + 1013904223u;
    return s_rng >> 8;
}
static void test_counter_wrap(void) {
    assert(bench_elapsed_u32(0x00000010u, 0x00000030u) == 32u);
    assert(bench_elapsed_u32(0xFFFFFFF0u, 0x00000010u) == 32u);
    assert(bench_elapsed_u32(0xFFFFFFFFu, 0x00000000u) == 1u);
}

static void test_arena(void) {
    assert(bench_arena_run(0u) == 937500u);
    assert(bench_arena_run(1u) == 0u);
    bench_arena a;
    bench_arena_init(&a);
    uint32_t h[128];
    uint32_t sp[128][2];
    uint32_t live = 0u;
    for (int op = 0; op < 4000; ++op) {
        if (live < 128u && (live == 0u || (arena_rnd() % 3u) != 0u)) {
            uint32_t sz = 8u + (arena_rnd() % 504u);
            uint32_t x = bench_arena_alloc(&a, sz);
            if (x != 0u) {
                uint32_t b0 = x - BENCH_ARENA_HDR;
                uint32_t b1 = b0 + bench_arena_size(&a, b0);
                for (uint32_t j = 0; j < live; ++j) {
                    assert(b0 >= sp[j][1] || sp[j][0] >= b1);
                }
                h[live] = x;
                sp[live][0] = b0;
                sp[live][1] = b1;
                ++live;
            }
        } else if (live > 0u) {
            uint32_t k = arena_rnd() % live;
            bench_arena_free(&a, h[k]);
            h[k] = h[live - 1u];
            sp[k][0] = sp[live - 1u][0];
            sp[k][1] = sp[live - 1u][1];
            --live;
        }
    }
    while (live > 0u) {
        --live;
        bench_arena_free(&a, h[live]);
    }
    uint32_t total;
    uint32_t largest;
    bench_arena_stats(&a, &total, &largest);
    assert(total == 8184u && largest == 8184u);
    /* Case 28 is runner-native: a port with no allocator hooks still runs it and
     * reports the shared arena's golden value. */
    fake g = {0};
    bench_port native_only = {.ctx = &g, .now_ticks = now, .ticks_per_second = 1000};
    bench_sink as = {.ctx = &g, .record = record_arena, .flush = flush};
    assert(bench_run_case(&native_only, &as, BENCH_COMMON_ARENA_FRAGMENTATION,
                          (bench_settings){0, 2}) == BENCH_OK);
    assert(s_arena_case_records == 2u && s_arena_case_value == 937500u);
}
int main(void) {
    test_counter_wrap();
    test_arena();
    fake f={0};
    bench_port p={
        .ctx=&f, .now_ticks=now, .ticks_per_second=1000,
        .queue_create=create, .queue_send=send, .queue_receive=receive, .queue_destroy=destroy,
        .handoff_create=hcreate, .handoff_measure=hmeasure, .handoff_destroy=hdestroy,
        .jitter_create=jcreate, .jitter_measure=jmeasure, .jitter_destroy=jdestroy,
        .isr_create=icreate, .isr_measure=imeasure, .isr_destroy=idestroy,
    };
    bench_sink s={.ctx=&f, .record=record, .flush=flush};
    const bench_case cases[]={BENCH_TIMER_OVERHEAD,BENCH_QUEUE_ROUNDTRIP,
                              BENCH_TASK_HANDOFF,BENCH_PERIODIC_JITTER,BENCH_ISR_TO_TASK};
    assert(bench_run_suite(&p,&s,cases,5,(bench_settings){2,3})==BENCH_OK);
    assert(f.sends==5 && f.records==15 && f.flushes==5);
    assert(f.destroyed && f.hdestroyed && f.jdestroyed && f.idestroyed);
    assert(f.handoffs==5 && f.jitters==5 && f.isrs==5);
    f=(fake){.corrupt=1};
    assert(bench_run_suite(&p,&s,cases+1,1,(bench_settings){0,3})==BENCH_CORRUPT);
    assert(f.destroyed && f.records==0 && f.flushes==0);
    assert(bench_run_suite(&p,&s,cases,0,(bench_settings){0,1})==BENCH_INVALID);
    /* A port without the optional functions reports invalid, not a fake result. */
    {
        fake g={0};
        bench_port bare={
            .ctx=&g, .now_ticks=now, .ticks_per_second=1000,
            .queue_create=create, .queue_send=send, .queue_receive=receive, .queue_destroy=destroy,
            .handoff_create=NULL, .handoff_measure=NULL, .handoff_destroy=NULL,
            .jitter_create=NULL, .jitter_measure=NULL, .jitter_destroy=NULL,
            .isr_create=NULL, .isr_measure=NULL, .isr_destroy=NULL,
        };
        bench_sink gs={.ctx=&g, .record=record, .flush=flush};
        assert(bench_run_case(&bare,&gs,BENCH_TASK_HANDOFF,(bench_settings){0,1})==BENCH_INVALID);
        assert(bench_run_case(&bare,&gs,BENCH_PERIODIC_JITTER,(bench_settings){0,1})==BENCH_INVALID);
        assert(bench_run_case(&bare,&gs,BENCH_ISR_TO_TASK,(bench_settings){0,1})==BENCH_INVALID);
    }
    return 0;
}
