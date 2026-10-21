/* Zephyr bench_port adapter.
 *
 *   now_ticks        : k_cycle_get_64 (monotonic hardware cycles; the
 *                      STM32F4 timer counter wraps every 2^48 cycles at 1 MHz,
 *                      far beyond any benchmark duration, so no rollover
 *                      handling is needed in practice).
 *   ticks_per_second : CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC.
 *   queue            : k_msgq with capacity 1 and a uint32_t payload.
 *
 * The queue is created and destroyed outside the timed sections by the
 * runner; the timed path only sends and receives.
 */
#include "rtos_bench.h"
#include <zephyr/kernel.h>
#include <zephyr/sys/mem_stats.h>
#include <soc.h> /* STM32F4 CMSIS device header: DWT, TIM3/TIM4, RCC */

/* The Rust application core (firmware/c-kernels/zephyr/rust/src/boundary.rs)
 * mirrors the kernel-object layout with opaque arrays sized to a cap. These
 * asserts make a config that grows the objects past the cap fail loudly at
 * build time instead of silently overflowing the mirrors. */
_Static_assert(sizeof(struct k_thread) <= 144, "KThread mirror too small");
_Static_assert(_Alignof(struct k_thread) <= 8, "KThread mirror under-aligned");
_Static_assert(sizeof(struct k_sem) <= 16, "KSem mirror too small");
_Static_assert(sizeof(struct k_msgq) <= 40, "KMsgq mirror too small");
_Static_assert(sizeof(struct k_mutex) <= 20, "KMutex mirror too small");
_Static_assert(sizeof(struct k_timer) <= 56, "KTimer mirror too small");

/* Zephyr's kernel heap behind k_malloc/k_free. There is no public accessor for
 * its sys_heap stats, so reach the global K_HEAP_DEFINE object directly (pinned
 * Zephyr 4.4.2). */
extern struct k_heap _system_heap;

#define MSGQ_SLOT_SIZE 4
#define MSGQ_MAX_MSGS  1
#define MSGQ_MAX_SIZE  (MSGQ_SLOT_SIZE * MSGQ_MAX_MSGS)

static struct k_msgq s_msgq;
static char s_msgq_buf[MSGQ_MAX_SIZE];

static bench_status port_now_ticks(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
	}
	*ticks = k_cycle_get_64();
	return BENCH_OK;
}

static bench_status port_queue_create(void *ctx)
{
	(void)ctx;
	k_msgq_init(&s_msgq, s_msgq_buf, MSGQ_SLOT_SIZE, MSGQ_MAX_MSGS);
	return BENCH_OK;
}

static bench_status port_queue_send(void *ctx, uint32_t value)
{
	(void)ctx;
	uint32_t v = value;
	if (k_msgq_put(&s_msgq, &v, K_NO_WAIT) != 0) {
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
	uint32_t v = 0;
	if (k_msgq_get(&s_msgq, &v, K_NO_WAIT) != 0) {
		return BENCH_PORT;
	}
	*value = v;
	return BENCH_OK;
}

static bench_status port_queue_destroy(void *ctx)
{
	(void)ctx;
	k_msgq_init(&s_msgq, s_msgq_buf, MSGQ_SLOT_SIZE, MSGQ_MAX_MSGS);
	return BENCH_OK;
}

/* ------------------------------------------------------------ task handoff */

#define HANDOFF_STACK_SIZE 2048

static K_THREAD_STACK_DEFINE(s_handoff_stack, HANDOFF_STACK_SIZE);
static struct k_thread  s_handoff_thread;
static struct k_sem     s_handoff_signal;
static struct k_sem     s_handoff_done;
static volatile uint64_t s_handoff_wake;
static volatile int      s_handoff_quit;

static void handoff_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		k_sem_take(&s_handoff_signal, K_FOREVER);
		if (s_handoff_quit) {
			break;
		}
		s_handoff_wake = k_cycle_get_64();
		k_sem_give(&s_handoff_done);
	}
}

static bench_status port_handoff_create(void *ctx)
{
	(void)ctx;
	s_handoff_quit = 0;
	k_sem_init(&s_handoff_signal, 0, 1);
	k_sem_init(&s_handoff_done, 0, 1);
	/* Cooperative priority so the give wakes the peer immediately. */
	k_thread_create(&s_handoff_thread, s_handoff_stack,
			K_THREAD_STACK_SIZEOF(s_handoff_stack),
			handoff_entry, NULL, NULL, NULL,
			K_PRIO_COOP(1), 0, K_NO_WAIT);
	return BENCH_OK;
}

static bench_status port_handoff_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
	}
	k_sem_give(&s_handoff_signal);
	if (k_sem_take(&s_handoff_done, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	*ticks = s_handoff_wake;
	return BENCH_OK;
}

static bench_status port_handoff_destroy(void *ctx)
{
	(void)ctx;
	s_handoff_quit = 1;
	k_sem_give(&s_handoff_signal);
	k_thread_join(&s_handoff_thread, K_FOREVER);
	return BENCH_OK;
}

/* ---------------------------------------------- yield / context switch (5) */

/* An equal-priority peer loops on k_yield(); the benchmark yields and confirms
 * the peer ran, timing one full round trip. */
#define YIELD_STACK_SIZE 2048

static K_THREAD_STACK_DEFINE(s_yield_stack, YIELD_STACK_SIZE);
static struct k_thread   s_yield_thread;
static volatile uint32_t s_yield_peer_runs;
static volatile uint32_t s_yield_peer_started;
static volatile int      s_yield_quit;

static void yield_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	s_yield_peer_started = 1;
	while (!s_yield_quit) {
		s_yield_peer_runs++;
		k_yield();
	}
}

/* Task-count sweep (case 5): extra cooperative same-priority peers. Each needs
 * its own stack; K_THREAD_STACK_ARRAY_DEFINE keeps them in one BSS block. */
#define PEERS_MAX_EXTRA 32
#define PEERS_STACK_SIZE 512
K_THREAD_STACK_ARRAY_DEFINE(s_peers_stacks, PEERS_MAX_EXTRA, PEERS_STACK_SIZE);
static struct k_thread   s_peers_threads[PEERS_MAX_EXTRA];
static volatile int      s_peers_quit[PEERS_MAX_EXTRA];
static uint32_t          s_peers_count;

static void peers_entry(void *a, void *b, void *c)
{
	(void)b;
	(void)c;
	uintptr_t idx = (uintptr_t)a;
	while (!s_peers_quit[idx]) {
		k_yield();
	}
}

static bench_status port_peers_set(void *ctx, uint32_t count)
{
	(void)ctx;
	uint32_t want = (count > 0U) ? (count - 1U) : 0U;
	if (want > PEERS_MAX_EXTRA) {
		return BENCH_PORT;
	}
	int priority = k_thread_priority_get(k_current_get());
	while (s_peers_count > want) {
		s_peers_count--;
		s_peers_quit[s_peers_count] = 1;
		k_yield();
		k_thread_join(&s_peers_threads[s_peers_count], K_FOREVER);
	}
	while (s_peers_count < want) {
		uint32_t i = s_peers_count;
		s_peers_quit[i] = 0;
		k_thread_create(&s_peers_threads[i], s_peers_stacks[i],
				PEERS_STACK_SIZE, peers_entry,
				(void *)(uintptr_t)i, NULL, NULL,
				priority, 0, K_NO_WAIT);
		s_peers_count++;
	}
	return BENCH_OK;
}

static bench_status port_yield_create(void *ctx)
{
	(void)ctx;
	s_yield_quit = 0;
	s_yield_peer_runs = 0;
	s_yield_peer_started = 0;
	int priority = k_thread_priority_get(k_current_get());
	k_thread_create(&s_yield_thread, s_yield_stack,
			K_THREAD_STACK_SIZEOF(s_yield_stack),
			yield_entry, NULL, NULL, NULL,
			priority, 0, K_NO_WAIT);
	for (int i = 0; i < 1000 && s_yield_peer_started == 0; i++) {
		k_yield();
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
		k_yield();
		if (s_yield_peer_runs != before) {
			return BENCH_OK;
		}
	}
	return BENCH_CORRUPT;
}

static bench_status port_yield_destroy(void *ctx)
{
	(void)ctx;
	while (s_peers_count > 0U) {
		s_peers_count--;
		s_peers_quit[s_peers_count] = 1;
		k_yield();
		k_thread_join(&s_peers_threads[s_peers_count], K_FOREVER);
	}
	if (s_yield_peer_started) {
		s_yield_quit = 1;
		k_yield();
		k_thread_join(&s_yield_thread, K_FOREVER);
		s_yield_peer_started = 0;
	}
	return BENCH_OK;
}

/* --------------------------------------------------- preempt latency (6) */

/* H blocks on s_preempt_sem one priority above the benchmark thread (L); L
 * gives it, H is released immediately, timestamps itself and blocks again so L
 * resumes and returns the delta. */
#define PREEMPT_STACK_SIZE 2048

static K_THREAD_STACK_DEFINE(s_preempt_stack, PREEMPT_STACK_SIZE);
static struct k_thread   s_preempt_thread;
static struct k_sem      s_preempt_sem;
static volatile uint64_t s_preempt_wake;
static volatile uint32_t s_preempt_seq;
static volatile uint32_t s_preempt_ready;
static volatile int      s_preempt_quit;

static void preempt_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	s_preempt_ready = 1;
	while (!s_preempt_quit) {
		k_sem_take(&s_preempt_sem, K_FOREVER);
		if (s_preempt_quit) {
			break;
		}
		s_preempt_wake = k_cycle_get_64();
		s_preempt_seq++;
	}
}

static bench_status port_preempt_create(void *ctx)
{
	(void)ctx;
	s_preempt_quit = 0;
	s_preempt_wake = 0;
	s_preempt_seq = 0;
	s_preempt_ready = 0;
	k_sem_init(&s_preempt_sem, 0, 1);
	int priority = k_thread_priority_get(k_current_get()) - 1;
	if (priority < K_HIGHEST_APPLICATION_THREAD_PRIO) {
		return BENCH_PORT; /* no higher priority available */
	}
	k_thread_create(&s_preempt_thread, s_preempt_stack,
			K_THREAD_STACK_SIZEOF(s_preempt_stack),
			preempt_entry, NULL, NULL, NULL,
			priority, 0, K_NO_WAIT);
	for (int i = 0; i < 1000 && s_preempt_ready == 0; i++) {
		k_yield();
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
	uint64_t start = k_cycle_get_64();
	k_sem_give(&s_preempt_sem);
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
		k_sem_give(&s_preempt_sem);
		k_thread_join(&s_preempt_thread, K_FOREVER);
		s_preempt_ready = 0;
	}
	return BENCH_OK;
}

/* ------------------------------------------------------- ipc ping-pong */

/* Two capacity-1 k_msgq carry a request and its echo; the peer runs at a
 * cooperative priority so each request wakes it immediately. measure() times the
 * full round trip (two context switches) and rejects a mismatched reply. */

#define IPC_STACK_SIZE 2048
#define IPC_QUEUE_MAX  1

static K_THREAD_STACK_DEFINE(s_ipc_stack, IPC_STACK_SIZE);
static struct k_thread  s_ipc_thread;
static struct k_msgq    s_ipc_req;
static struct k_msgq    s_ipc_rep;
static char             s_ipc_req_buf[MSGQ_SLOT_SIZE * IPC_QUEUE_MAX];
static char             s_ipc_rep_buf[MSGQ_SLOT_SIZE * IPC_QUEUE_MAX];
static volatile int     s_ipc_quit;
static int              s_ipc_active;

static void ipc_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		uint32_t v = 0;
		k_msgq_get(&s_ipc_req, &v, K_FOREVER);
		if (s_ipc_quit) {
			break;
		}
		k_msgq_put(&s_ipc_rep, &v, K_FOREVER);
	}
}

static bench_status port_ipc_create(void *ctx)
{
	(void)ctx;
	s_ipc_quit = 0;
	k_msgq_init(&s_ipc_req, s_ipc_req_buf, MSGQ_SLOT_SIZE, IPC_QUEUE_MAX);
	k_msgq_init(&s_ipc_rep, s_ipc_rep_buf, MSGQ_SLOT_SIZE, IPC_QUEUE_MAX);
	k_thread_create(&s_ipc_thread, s_ipc_stack,
			K_THREAD_STACK_SIZEOF(s_ipc_stack),
			ipc_entry, NULL, NULL, NULL,
			K_PRIO_COOP(1), 0, K_NO_WAIT);
	s_ipc_active = 1;
	return BENCH_OK;
}

static bench_status port_ipc_measure(void *ctx, uint32_t value, uint64_t *ticks)
{
	(void)ctx;
	if (!s_ipc_active || ticks == NULL) {
		return BENCH_PORT;
	}
	uint32_t v = value;
	uint64_t start = k_cycle_get_64();
	if (k_msgq_put(&s_ipc_req, &v, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint32_t received_value = 0;
	if (k_msgq_get(&s_ipc_rep, &received_value, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t end = k_cycle_get_64();
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
		(void)k_msgq_put(&s_ipc_req, &sentinel, K_FOREVER);
		k_thread_join(&s_ipc_thread, K_FOREVER);
		s_ipc_active = 0;
	}
	return BENCH_OK;
}

/* Case 18: IPC payload sweep. Each payload size owns a capacity-1 request/reply
 * k_msgq pair so the kernel's item copy scales with the payload; one cooperative
 * echo thread per size (same structure as case 7, so the 4-byte point matches).
 * measure() times the full round trip with k_cycle_get_64 and verifies every
 * payload byte. */
#define IPC_PAYLOAD_N 5
#define IPC_PAYLOAD_MAX 256
static const uint16_t k_ipc_payload_bytes[IPC_PAYLOAD_N] = {1, 4, 16, 64, 256};
static struct k_msgq s_ipc_payload_req[IPC_PAYLOAD_N];
static struct k_msgq s_ipc_payload_rep[IPC_PAYLOAD_N];
static char __aligned(4) s_ipc_payload_req_buf[IPC_PAYLOAD_N][IPC_PAYLOAD_MAX];
static char __aligned(4) s_ipc_payload_rep_buf[IPC_PAYLOAD_N][IPC_PAYLOAD_MAX];
static struct k_thread s_ipc_payload_thread[IPC_PAYLOAD_N];
static K_THREAD_STACK_ARRAY_DEFINE(s_ipc_payload_stack, IPC_PAYLOAD_N, 2048);
static uint8_t s_ipc_payload_tx[IPC_PAYLOAD_MAX];
static uint8_t s_ipc_payload_rx[IPC_PAYLOAD_MAX];
static volatile int s_ipc_payload_quit;
static int s_ipc_payload_active;

static void ipc_payload_entry(void *p1, void *p2, void *p3)
{
	const size_t idx = (size_t)p1;
	uint8_t buf[IPC_PAYLOAD_MAX];
	(void)p2;
	(void)p3;
	for (;;) {
		if (k_msgq_get(&s_ipc_payload_req[idx], buf, K_FOREVER) != 0) {
			continue;
		}
		if (s_ipc_payload_quit) {
			break;
		}
		(void)k_msgq_put(&s_ipc_payload_rep[idx], buf, K_FOREVER);
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
	s_ipc_payload_quit = 0;
	for (int i = 0; i < IPC_PAYLOAD_N; i++) {
		k_msgq_init(&s_ipc_payload_req[i], s_ipc_payload_req_buf[i],
			    k_ipc_payload_bytes[i], 1);
		k_msgq_init(&s_ipc_payload_rep[i], s_ipc_payload_rep_buf[i],
			    k_ipc_payload_bytes[i], 1);
		k_thread_create(&s_ipc_payload_thread[i], s_ipc_payload_stack[i], 2048,
				ipc_payload_entry, (void *)(size_t)i, NULL, NULL,
				K_PRIO_COOP(1), 0, K_NO_WAIT);
	}
	s_ipc_payload_active = 1;
	return BENCH_OK;
}

static bench_status port_ipc_payload_measure(void *ctx, uint32_t payload_bytes,
					     uint64_t *ticks)
{
	(void)ctx;
	int idx = ipc_payload_index(payload_bytes);
	if (!s_ipc_payload_active || idx < 0 || ticks == NULL) {
		return BENCH_PORT;
	}
	uint64_t start = k_cycle_get_64();
	if (k_msgq_put(&s_ipc_payload_req[idx], s_ipc_payload_tx, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	if (k_msgq_get(&s_ipc_payload_rep[idx], s_ipc_payload_rx, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t end = k_cycle_get_64();
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
		for (int i = 0; i < IPC_PAYLOAD_N; i++) {
			/* Any message wakes the peer so it observes s_ipc_payload_quit;
			 * s_ipc_payload_tx is at least as large as every queue slot. */
			(void)k_msgq_put(&s_ipc_payload_req[i], s_ipc_payload_tx, K_FOREVER);
			k_thread_join(&s_ipc_payload_thread[i], K_FOREVER);
		}
		s_ipc_payload_active = 0;
	}
	return BENCH_OK;
}

/* Case 19: IPC throughput. A producer pushes BENCH_TP_MSGS 64-byte messages into
 * a depth-8 k_msgq drained by a cooperative consumer; measure() times the whole
 * transfer with k_cycle_get_64 (64-bit, so no wrap handling is needed). */
#define TP_MSG_BYTES 64
#define TP_MSGS      16384 /* 1 MiB / 64 B */
#define TP_QUEUE_DEPTH 8
static struct k_msgq s_tp_q;
static char __aligned(4) s_tp_q_buf[TP_QUEUE_DEPTH * TP_MSG_BYTES];
static struct k_sem s_tp_done;
static K_THREAD_STACK_DEFINE(s_tp_stack, 2048);
static struct k_thread s_tp_thread;
static uint8_t s_tp_tx[TP_MSG_BYTES];
static volatile uint32_t s_tp_target;
static volatile uint32_t s_tp_seen;
static volatile int s_tp_quit;
static int s_tp_active;

static void tp_entry(void *a, void *b, void *c)
{
	uint8_t buf[TP_MSG_BYTES];
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		if (k_msgq_get(&s_tp_q, buf, K_FOREVER) != 0) {
			continue;
		}
		if (s_tp_quit) {
			break;
		}
		if (++s_tp_seen >= s_tp_target) {
			k_sem_give(&s_tp_done);
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
	k_msgq_init(&s_tp_q, s_tp_q_buf, TP_MSG_BYTES, TP_QUEUE_DEPTH);
	k_sem_init(&s_tp_done, 0, 1);
	k_thread_create(&s_tp_thread, s_tp_stack,
			K_THREAD_STACK_SIZEOF(s_tp_stack),
			tp_entry, NULL, NULL, NULL,
			K_PRIO_COOP(1), 0, K_NO_WAIT);
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
	uint64_t start = k_cycle_get_64();
	for (uint32_t i = 0; i < TP_MSGS; i++) {
		if (k_msgq_put(&s_tp_q, s_tp_tx, K_FOREVER) != 0) {
			return BENCH_PORT;
		}
	}
	if (k_sem_take(&s_tp_done, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t end = k_cycle_get_64();
	*ticks = end - start;
	return BENCH_OK;
}

static bench_status port_tp_destroy(void *ctx)
{
	(void)ctx;
	if (s_tp_active) {
		s_tp_quit = 1;
		(void)k_msgq_put(&s_tp_q, s_tp_tx, K_FOREVER);
		k_thread_join(&s_tp_thread, K_FOREVER);
		s_tp_active = 0;
	}
	return BENCH_OK;
}

/* Case 27: zero-copy IPC. The copied leg round-trips the 256-byte payload
 * through a capacity-1 k_msgq (the kernel copies the bytes); the zero-copy leg
 * sends only a pointer to a shared buffer through a pointer-sized k_msgq, so
 * the payload never crosses the kernel. measure() returns copied minus
 * zero-copy k_cycle_get_64() ticks, clamped to 0 when a sample transiently
 * shows no benefit. The copied leg matches case 18 at 256 B. */
#define ZCOPY_BYTES 256
static struct k_msgq s_zcopy_cp_req;
static struct k_msgq s_zcopy_cp_rep;
static struct k_msgq s_zcopy_zc_req;
static struct k_msgq s_zcopy_zc_rep;
static char __aligned(4) s_zcopy_cp_req_buf[ZCOPY_BYTES];
static char __aligned(4) s_zcopy_cp_rep_buf[ZCOPY_BYTES];
static char __aligned(4) s_zcopy_zc_req_buf[sizeof(void *)];
static char __aligned(4) s_zcopy_zc_rep_buf[sizeof(void *)];
static uint8_t s_zcopy_shared[ZCOPY_BYTES];
static K_THREAD_STACK_DEFINE(s_zcopy_cp_stack, 1024);
static K_THREAD_STACK_DEFINE(s_zcopy_zc_stack, 1024);
static struct k_thread s_zcopy_cp_thread;
static struct k_thread s_zcopy_zc_thread;
static volatile int s_zcopy_quit;
static int s_zcopy_active;
/* The copied leg reuses the case-18 send/receive buffers (cases run
 * sequentially) to keep static RAM down. */
#define s_zcopy_tx s_ipc_payload_tx
#define s_zcopy_rx s_ipc_payload_rx

static void zcopy_copy_entry(void *a, void *b, void *c)
{
	uint8_t buf[ZCOPY_BYTES];
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		if (k_msgq_get(&s_zcopy_cp_req, buf, K_FOREVER) != 0) {
			continue;
		}
		if (s_zcopy_quit) {
			break;
		}
		(void)k_msgq_put(&s_zcopy_cp_rep, buf, K_FOREVER);
	}
}

static void zcopy_ref_entry(void *a, void *b, void *c)
{
	void *ref;
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		if (k_msgq_get(&s_zcopy_zc_req, &ref, K_FOREVER) != 0) {
			continue;
		}
		if (s_zcopy_quit) {
			break;
		}
		(void)k_msgq_put(&s_zcopy_zc_rep, &ref, K_FOREVER);
	}
}

static bench_status port_zcopy_create(void *ctx)
{
	(void)ctx;
	for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
		s_zcopy_tx[i] = (uint8_t)(0x3Cu ^ i);
		s_zcopy_shared[i] = (uint8_t)(0x3Cu ^ i);
	}
	s_zcopy_quit = 0;
	k_msgq_init(&s_zcopy_cp_req, s_zcopy_cp_req_buf, ZCOPY_BYTES, 1);
	k_msgq_init(&s_zcopy_cp_rep, s_zcopy_cp_rep_buf, ZCOPY_BYTES, 1);
	k_msgq_init(&s_zcopy_zc_req, s_zcopy_zc_req_buf, sizeof(void *), 1);
	k_msgq_init(&s_zcopy_zc_rep, s_zcopy_zc_rep_buf, sizeof(void *), 1);
	k_thread_create(&s_zcopy_cp_thread, s_zcopy_cp_stack,
			K_THREAD_STACK_SIZEOF(s_zcopy_cp_stack),
			zcopy_copy_entry, NULL, NULL, NULL,
			K_PRIO_COOP(1), 0, K_NO_WAIT);
	k_thread_create(&s_zcopy_zc_thread, s_zcopy_zc_stack,
			K_THREAD_STACK_SIZEOF(s_zcopy_zc_stack),
			zcopy_ref_entry, NULL, NULL, NULL,
			K_PRIO_COOP(1), 0, K_NO_WAIT);
	s_zcopy_active = 1;
	return BENCH_OK;
}

static bench_status port_zcopy_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (!s_zcopy_active || ticks == NULL) {
		return BENCH_PORT;
	}
	uint64_t start = k_cycle_get_64();
	if (k_msgq_put(&s_zcopy_cp_req, s_zcopy_tx, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	if (k_msgq_get(&s_zcopy_cp_rep, s_zcopy_rx, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t copied = k_cycle_get_64() - start;
	for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
		if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
			return BENCH_CORRUPT;
		}
	}
	void *ref = s_zcopy_shared;
	void *back = NULL;
	start = k_cycle_get_64();
	if (k_msgq_put(&s_zcopy_zc_req, &ref, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	if (k_msgq_get(&s_zcopy_zc_rep, &back, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t zcopy = k_cycle_get_64() - start;
	if (back != s_zcopy_shared) {
		return BENCH_CORRUPT;
	}
	for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
		if (s_zcopy_shared[i] != s_zcopy_tx[i]) {
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
		void *sentinel = s_zcopy_shared;
		(void)k_msgq_put(&s_zcopy_cp_req, s_zcopy_tx, K_FOREVER);
		(void)k_msgq_put(&s_zcopy_zc_req, &sentinel, K_FOREVER);
		k_thread_join(&s_zcopy_cp_thread, K_FOREVER);
		k_thread_join(&s_zcopy_zc_thread, K_FOREVER);
		s_zcopy_active = 0;
	}
	return BENCH_OK;
}

/* ------------------------------------------- priority-inheritance mutex */

/* H (the benchmark thread) blocks on a k_mutex held by L while M spins at a
 * priority between them. k_mutex raises the holder to the highest waiter, so L
 * releases promptly; otherwise H waits out M's bounded spin. */

static K_THREAD_STACK_DEFINE(s_mutex_stack_m, 2048);
static K_THREAD_STACK_DEFINE(s_mutex_stack_l, 2048);
static struct k_thread  s_mutex_thread_m;
static struct k_thread  s_mutex_thread_l;
static struct k_mutex   s_mutex;
static struct k_sem     s_m_req;
static struct k_sem     s_m_held;
static struct k_sem     s_m_rel;
static struct k_sem     s_m_go;
static struct k_sem     s_m_done;
static volatile int     s_m_quit;
static volatile int     s_m_stop;
static int              s_m_made;

static void mutex_low_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		k_sem_take(&s_m_req, K_FOREVER);
		if (s_m_quit) {
			break;
		}
		k_mutex_lock(&s_mutex, K_FOREVER);
		k_sem_give(&s_m_held);
		k_sem_take(&s_m_rel, K_FOREVER);
		k_mutex_unlock(&s_mutex);
		if (s_m_quit) {
			break;
		}
	}
}

static void mutex_med_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		k_sem_take(&s_m_go, K_FOREVER);
		if (s_m_quit) {
			break;
		}
		uint64_t start = k_cycle_get_64();
		uint64_t limit = (uint64_t)CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC / 500;
		while (!s_m_stop && (k_cycle_get_64() - start) < limit) {
		}
		k_sem_give(&s_m_done);
	}
}

static bench_status port_mutex_create(void *ctx)
{
	(void)ctx;
	if (s_m_made) {
		return BENCH_OK;
	}
	k_sem_init(&s_m_req, 0, 1);
	k_sem_init(&s_m_held, 0, 1);
	k_sem_init(&s_m_rel, 0, 1);
	k_sem_init(&s_m_go, 0, 1);
	k_sem_init(&s_m_done, 0, 1);
	k_mutex_init(&s_mutex);
	s_m_quit = 0;
	int base = k_thread_priority_get(k_current_get());
	int pm = (base >= 0) ? base + 1 : K_PRIO_PREEMPT(2);
	int pl = (base >= 0) ? base + 2 : K_PRIO_PREEMPT(3);
	k_thread_create(&s_mutex_thread_m, s_mutex_stack_m,
			K_THREAD_STACK_SIZEOF(s_mutex_stack_m),
			mutex_med_entry, NULL, NULL, NULL, pm, 0, K_NO_WAIT);
	k_thread_create(&s_mutex_thread_l, s_mutex_stack_l,
			K_THREAD_STACK_SIZEOF(s_mutex_stack_l),
			mutex_low_entry, NULL, NULL, NULL, pl, 0, K_NO_WAIT);
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
	k_sem_give(&s_m_req);
	k_sem_take(&s_m_held, K_FOREVER);
	k_sem_give(&s_m_go);
	uint64_t t0 = k_cycle_get_64();
	k_sem_give(&s_m_rel);
	k_mutex_lock(&s_mutex, K_FOREVER);
	uint64_t t1 = k_cycle_get_64();
	s_m_stop = 1;
	k_sem_take(&s_m_done, K_FOREVER);
	k_mutex_unlock(&s_mutex);
	*ticks = t1 - t0;
	return BENCH_OK;
}

static bench_status port_mutex_destroy(void *ctx)
{
	(void)ctx;
	if (s_m_made) {
		s_m_quit = 1;
		k_sem_give(&s_m_req);
		k_sem_give(&s_m_rel);
		k_sem_give(&s_m_go);
		k_thread_join(&s_mutex_thread_m, K_FOREVER);
		k_thread_join(&s_mutex_thread_l, K_FOREVER);
		s_m_made = 0;
	}
	return BENCH_OK;
}

/* ------------------------------------------------- timer callback (9) */

/* One-shot k_timer (system timer ISR context) armed for 10 ms; the expiry
 * function timestamps itself and gives the semaphore. */

static struct k_timer     s_timer;
static struct k_sem       s_timer_sem;
static volatile uint64_t  s_timer_fire;

static void timer_expiry(struct k_timer *t)
{
	(void)t;
	s_timer_fire = k_cycle_get_64();
	k_sem_give(&s_timer_sem);
}

static bench_status port_timer_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_timer_sem, 0, 1);
	k_timer_init(&s_timer, timer_expiry, NULL);
	return BENCH_OK;
}

static bench_status port_timer_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_PORT;
	}
	k_sem_take(&s_timer_sem, K_NO_WAIT);
	uint64_t t0 = k_cycle_get_64();
	uint64_t period = (uint64_t)CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC / 100;
	k_timer_start(&s_timer, K_MSEC(10), K_NO_WAIT);
	k_sem_take(&s_timer_sem, K_FOREVER);
	uint64_t t1 = s_timer_fire;
	uint64_t interval = (t1 >= t0) ? (t1 - t0) : 0;
	*ticks = (interval >= period) ? (interval - period) : (period - interval);
	return BENCH_OK;
}

static bench_status port_timer_destroy(void *ctx)
{
	(void)ctx;
	k_timer_stop(&s_timer);
	return BENCH_OK;
}

/* ------------------------------------------------------- periodic jitter */

/* Absolute 1 ms grid: advance an absolute tick target by exactly one period
 * each iteration. A bare relative k_sleep(1 tick) is rounded up to the next
 * tick boundary and lands a whole tick late every time; targeting the grid
 * keeps consecutive wakes one period apart (matching xTaskDelayUntil). */
#define JITTER_PERIOD_TICKS 1
static int64_t  s_jitter_target;
static uint64_t s_jitter_prev;

static bench_status port_jitter_create(void *ctx)
{
	(void)ctx;
	s_jitter_target = k_uptime_ticks() + JITTER_PERIOD_TICKS;
	k_sleep(K_TIMEOUT_ABS_TICKS(s_jitter_target));
	s_jitter_prev = k_cycle_get_64();
	return BENCH_OK;
}

static bench_status port_jitter_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
	}
	s_jitter_target += JITTER_PERIOD_TICKS;
	k_sleep(K_TIMEOUT_ABS_TICKS(s_jitter_target));
	uint64_t now = k_cycle_get_64();
	uint64_t period =
		CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC / CONFIG_SYS_CLOCK_TICKS_PER_SEC;
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

/* k_timer expiry runs in system-timer ISR context: it timestamps and gives the
 * semaphore from the interrupt, which is the path under test. */
static struct k_timer   s_isr_timer;
static struct k_sem     s_isr_sem;
static volatile uint64_t s_isr_entry;

static void isr_timer_expiry(struct k_timer *timer)
{
	(void)timer;
	s_isr_entry = k_cycle_get_64();
	k_sem_give(&s_isr_sem);
}

static bench_status port_isr_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_isr_sem, 0, 1);
	k_timer_init(&s_isr_timer, isr_timer_expiry, NULL);
	return BENCH_OK;
}

static bench_status port_isr_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
	}
	k_timer_start(&s_isr_timer, K_USEC(20), K_NO_WAIT);
	if (k_sem_take(&s_isr_sem, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint64_t wake = k_cycle_get_64();
	*ticks = (wake >= s_isr_entry) ? (wake - s_isr_entry) : 0;
	return BENCH_OK;
}

static bench_status port_isr_destroy(void *ctx)
{
	(void)ctx;
	k_timer_stop(&s_isr_timer);
	return BENCH_OK;
}

/* ------------------------------------------------------- trigger_to_isr */

/* Spare NVIC line for the software-asserted trigger. STM32F4 EXTI lines 6-10,
 * 23, 40 and USART2 (38) are claimed by Zephyr's drivers, so use line 62
 * (CONFIG_NUM_IRQS=68). The CMSIS IRQn symbols are cast expressions that do not
 * fit Zephyr's IRQ_CONNECT token concatenation, so the raw number is used. */
#define TRIGGER_IRQ 62

/* Software-asserted minimal interrupt. The task masks interrupts, reads the
 * cycle counter, pends spare NVIC line EXTI0 and unmasks, so the exception is
 * taken at the unmask; the handler timestamps its first instruction. Value =
 * trigger -> entry; unlike case 4 there is no programmed one-shot delay to
 * subtract. EXTI0 is not otherwise used by this build. */
static struct k_sem      s_trig_sem;
static volatile uint64_t s_trig_entry;
static volatile uint32_t s_trig_state; /* 0 idle, 1 armed, 2 entered */

static void trigger_isr(const void *arg)
{
	(void)arg;
	s_trig_entry = k_cycle_get_64();
	s_trig_state = 2;
	NVIC_ClearPendingIRQ(TRIGGER_IRQ);
	k_sem_give(&s_trig_sem);
}

static bench_status port_trigger_isr_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_trig_sem, 0, 1);
	s_trig_state = 0;
	IRQ_CONNECT(TRIGGER_IRQ, 1, trigger_isr, NULL, 0);
	NVIC_ClearPendingIRQ(TRIGGER_IRQ);
	irq_enable(TRIGGER_IRQ);
	return BENCH_OK;
}

static bench_status port_trigger_isr_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
	}
	(void)k_sem_take(&s_trig_sem, K_NO_WAIT); /* drain any stale signal */
	s_trig_state = 1;
	unsigned int key = irq_lock();
	uint64_t t0 = k_cycle_get_64();
	NVIC_SetPendingIRQ(TRIGGER_IRQ);
	irq_unlock(key); /* unmask: the exception is taken here */
	if (k_sem_take(&s_trig_sem, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	if (s_trig_state != 2) {
		return BENCH_CORRUPT;
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
	irq_disable(TRIGGER_IRQ);
	NVIC_ClearPendingIRQ(TRIGGER_IRQ);
	return BENCH_OK;
}

/* ------------------------------------ periodic jitter under load (11) */

/* Case 11 on STM32F4. TIM3 auto-reloads at an exact 750 us (126000 DWT cycles
 * at 168 MHz; APB1 timer clock 84 MHz, ARR=62999) and its update ISR gives the
 * semaphore; TIM4 injects a DWT-calibrated busy-loop ISR every 137 us (23016
 * cycles, ARR=11507) at a higher NVIC priority and makes no RTOS calls.
 *
 * Timestamps use DWT->CYCCNT directly rather than k_cycle_get_64: the SysTick
 * 64-bit extension loses cycles under this tickless build (as measured on the
 * RP2350 path), while DWT is a free-running core-clock counter. The timer and
 * DWT share the PLL (84 MHz vs 168 MHz, exact 2:1), so the auto-reload grid is
 * an exact multiple of P in DWT ticks with no drift. R0 is the first hardware
 * release (DWT at TIM3 start + P) rather than the first ISR entry, so it is
 * independent of ISR-entry latency. TIM3/TIM4 are otherwise unused (Zephyr
 * case 4 uses a k_timer). IRQ numbers are the STM32F407 CMSIS values. */
#define PJIT_TIM3_IRQ 29 /* TIM3_IRQn */
#define PJIT_TIM4_IRQ 30 /* TIM4_IRQn */
#define PJIT_P_CYCLES    126000U                     /* 750 us @ 168 MHz DWT */
#define PJIT_TIMER_ARR   ((PJIT_P_CYCLES / 2U) - 1U) /* 84 MHz APB1 timer */
#define PJIT_LOAD_CYCLES 23016U                      /* 137 us @ 168 MHz DWT */
#define PJIT_LOAD_ARR    ((PJIT_LOAD_CYCLES / 2U) - 1U)
#define PJIT_LOAD_TARGET 8000U /* ~35% duty at an 8000-cycle budget */
#ifndef PJIT_LOAD_ENABLE
#define PJIT_LOAD_ENABLE 1
#endif

static struct k_sem      s_pjit_sem;
static volatile uint32_t s_pjit_r0;      /* DWT at the release grid origin */
static volatile uint32_t s_pjit_releases;
static volatile uint32_t s_pjit_n;
static volatile uint32_t s_pjit_iters;
static int s_pjit_made;

static void pjit_dwt_enable(void)
{
	CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
	DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
}

static inline void pjit_load_body(uint32_t n)
{
	volatile uint32_t a = 0;
	while (n-- > 0U) {
		a += n;
	}
	(void)a;
}

static void pjit_release_isr(const void *arg)
{
	(void)arg;
	if ((TIM3->SR & TIM_SR_UIF) == 0U) {
		return;
	}
	TIM3->SR = ~TIM_SR_UIF;
	s_pjit_releases++;
	k_sem_give(&s_pjit_sem);
}

#if PJIT_LOAD_ENABLE
static void pjit_load_isr(const void *arg)
{
	(void)arg;
	TIM4->SR = ~TIM_SR_UIF;
	pjit_load_body(s_pjit_iters);
}
#endif

static bench_status port_pjit_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_pjit_sem, 0, 1);
	s_pjit_r0 = 0;
	s_pjit_releases = 0;
	s_pjit_n = 0;

	pjit_dwt_enable();

#if PJIT_LOAD_ENABLE
	/* Calibrate the busy-loop body against DWT with interrupts masked, so no
	 * ISR can inflate the measurement. Bounded to avoid spinning forever. */
	s_pjit_iters = 100U;
	for (uint32_t tries = 0; tries < 64U; ++tries) {
		unsigned int key = irq_lock();
		uint32_t a = DWT->CYCCNT;
		pjit_load_body(s_pjit_iters);
		uint32_t d = DWT->CYCCNT - a;
		irq_unlock(key);
		if (d >= PJIT_LOAD_TARGET &&
		    d < (PJIT_LOAD_TARGET + PJIT_LOAD_TARGET / 8U)) {
			break;
		}
		if (d == 0U) {
			s_pjit_iters++;
		} else {
			uint32_t next =
				(uint32_t)(((uint64_t)s_pjit_iters *
					    PJIT_LOAD_TARGET) / d);
			s_pjit_iters = (next == 0U) ? 1U : next;
		}
	}
#endif

	RCC->APB1ENR |= RCC_APB1ENR_TIM3EN;
	TIM3->CR1 = 0;
	TIM3->PSC = 0;
	TIM3->ARR = PJIT_TIMER_ARR;
	TIM3->EGR = TIM_EGR_UG; /* latch PSC/ARR */
	TIM3->SR = 0;
	TIM3->DIER = TIM_DIER_UIE;
	IRQ_CONNECT(PJIT_TIM3_IRQ, 5, pjit_release_isr, NULL, 0);
	NVIC_ClearPendingIRQ(PJIT_TIM3_IRQ);
	irq_enable(PJIT_TIM3_IRQ);

#if PJIT_LOAD_ENABLE
	RCC->APB1ENR |= RCC_APB1ENR_TIM4EN;
	TIM4->CR1 = 0;
	TIM4->PSC = 0;
	TIM4->ARR = PJIT_LOAD_ARR;
	TIM4->EGR = TIM_EGR_UG;
	TIM4->SR = 0;
	TIM4->DIER = TIM_DIER_UIE;
	IRQ_CONNECT(PJIT_TIM4_IRQ, 1, pjit_load_isr, NULL, 0);
	NVIC_ClearPendingIRQ(PJIT_TIM4_IRQ);
	irq_enable(PJIT_TIM4_IRQ);
#endif

	/* Start the release timer and record the ideal first-release instant
	 * before any ISR can run. */
	unsigned int key = irq_lock();
	TIM3->CR1 = TIM_CR1_CEN;
	s_pjit_r0 = DWT->CYCCNT + PJIT_P_CYCLES;
#if PJIT_LOAD_ENABLE
	TIM4->CR1 = TIM_CR1_CEN;
#endif
	irq_unlock(key);

	s_pjit_made = 1;
	return BENCH_OK;
}

static bench_status port_pjit_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (!s_pjit_made || ticks == NULL) {
		return BENCH_PORT;
	}
	if (k_sem_take(&s_pjit_sem, K_FOREVER) != 0) {
		return BENCH_PORT;
	}
	uint32_t wake = DWT->CYCCNT;
	uint32_t n = s_pjit_n;
	s_pjit_n = n + 1U;
	if (s_pjit_releases > (n + 1U)) {
		/* A release was coalesced: resync the sample index so one transient
		 * backlog does not poison every following sample of the case. */
		s_pjit_n = s_pjit_releases;
		return BENCH_CORRUPT; /* coalesced/missed release */
	}
	uint32_t ideal = s_pjit_r0 + n * PJIT_P_CYCLES;
	uint32_t err = wake - ideal;
	if ((int32_t)err < 0) {
		return BENCH_CORRUPT;
	}
	*ticks = (uint64_t)err;
	return BENCH_OK;
}

static bench_status port_pjit_destroy(void *ctx)
{
	(void)ctx;
	irq_disable(PJIT_TIM3_IRQ);
#if PJIT_LOAD_ENABLE
	irq_disable(PJIT_TIM4_IRQ);
	TIM4->CR1 = 0;
	TIM4->DIER = 0;
	NVIC_ClearPendingIRQ(PJIT_TIM4_IRQ);
#endif
	TIM3->CR1 = 0;
	TIM3->DIER = 0;
	NVIC_ClearPendingIRQ(PJIT_TIM3_IRQ);
	s_pjit_made = 0;
	return BENCH_OK;
}

/* ------------------------------- uncontended sync / alloc / delay (12-15) */

/* Case 12: uncontended binary-semaphore give -> take API round trip. */
static struct k_sem s_api_sem;
static bench_status port_sem_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_api_sem, 0, 1);
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
	k_sem_give(&s_api_sem);
	if (k_sem_take(&s_api_sem, K_NO_WAIT) != 0) {
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
	return BENCH_OK;
}

/* Case 13: uncontended mutex lock -> unlock API round trip. */
static struct k_mutex s_api_mutex;
static bench_status port_mutex_u_create(void *ctx)
{
	(void)ctx;
	k_mutex_init(&s_api_mutex);
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
	k_mutex_lock(&s_api_mutex, K_FOREVER);
	k_mutex_unlock(&s_api_mutex);
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
	return BENCH_OK;
}

/* Case 14: allocator allocate -> free round trip (64-byte payload). */
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
	void *p = k_malloc(64);
	if (p != NULL) {
		k_free(p);
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

/* Case 15: one-shot delay accuracy. Requests one RTOS millisecond and reports
 * the absolute deviation of the actual elapsed ticks from the nominal period. */
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
	k_msleep(1);
	status = port_now_ticks(NULL, &end);
	if (status != BENCH_OK) {
		return status;
	}
	const uint64_t nominal = (uint64_t)CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC / 1000ULL;
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

/* Case 16: one k_yield() with no equal/higher-priority ready peer. */
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
	k_yield();
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

/* Case 17: k_sem notification. The peer runs one (preemptible) priority below
 * the benchmark, so it cannot signal before the benchmark blocks. */
#define NOTIFY_STACK_SIZE 1024
static K_THREAD_STACK_DEFINE(s_notify_stack, NOTIFY_STACK_SIZE);
static struct k_thread   s_notify_thread;
static struct k_sem      s_notify_go;
static struct k_sem      s_notify_done;
static volatile uint64_t s_notify_signal;
static volatile int      s_notify_quit;
static int               s_notify_active;

static void notify_entry(void *a, void *b, void *c)
{
	(void)a;
	(void)b;
	(void)c;
	for (;;) {
		k_sem_take(&s_notify_go, K_FOREVER);
		if (s_notify_quit) {
			break;
		}
		uint64_t t = 0;
		(void)port_now_ticks(NULL, &t);
		s_notify_signal = t;
		k_sem_give(&s_notify_done);
	}
}

static bench_status port_notify_create(void *ctx)
{
	(void)ctx;
	s_notify_quit = 0;
	s_notify_active = 0;
	k_sem_init(&s_notify_go, 0, 1);
	k_sem_init(&s_notify_done, 0, 1);
	const int prio = k_thread_priority_get(k_current_get()) + 1;
	k_thread_create(&s_notify_thread, s_notify_stack,
			K_THREAD_STACK_SIZEOF(s_notify_stack),
			notify_entry, NULL, NULL, NULL,
			prio, 0, K_NO_WAIT);
	s_notify_active = 1;
	return BENCH_OK;
}

static bench_status port_notify_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (!s_notify_active || ticks == NULL) {
		return BENCH_PORT;
	}
	while (k_sem_take(&s_notify_done, K_NO_WAIT) == 0) {
		/* drain stale tokens */
	}
	k_sem_give(&s_notify_go);
	if (k_sem_take(&s_notify_done, K_FOREVER) != 0) {
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
		k_sem_give(&s_notify_go);
		k_thread_join(&s_notify_thread, K_FOREVER);
		s_notify_active = 0;
	}
	return BENCH_OK;
}

/* Case 26: cooperative peers released together; the highest priority runs
 * first, each records its id and blocks for ack. */
#define ZPRIO_N 4
static K_THREAD_STACK_ARRAY_DEFINE(s_prio_stacks, ZPRIO_N, 1024);
static struct k_thread s_prio_thread[ZPRIO_N];
static struct k_sem    s_prio_release;
static struct k_sem    s_prio_all;
static struct k_sem    s_prio_ack;
static volatile uint32_t s_prio_n;
static volatile uint8_t  s_prio_trace[ZPRIO_N];
static volatile int      s_prio_quit;
static int               s_prio_active;

static void prio_entry(void *a, void *b, void *c)
{
	(void)b;
	(void)c;
	const uint8_t id = (uint8_t)(uintptr_t)a;
	for (;;) {
		k_sem_take(&s_prio_release, K_FOREVER);
		if (s_prio_quit) {
			break;
		}
		if (s_prio_n < ZPRIO_N) {
			s_prio_trace[s_prio_n] = id;
			s_prio_n++;
		}
		k_sem_give(&s_prio_all);
		k_sem_take(&s_prio_ack, K_FOREVER);
	}
}

static bench_status port_prio_create(void *ctx)
{
	(void)ctx;
	s_prio_quit = 0;
	s_prio_active = 0;
	k_sem_init(&s_prio_release, 0, ZPRIO_N);
	k_sem_init(&s_prio_all, 0, ZPRIO_N);
	k_sem_init(&s_prio_ack, 0, ZPRIO_N);
	for (uint8_t i = 0; i < ZPRIO_N; i++) {
		/* id ZPRIO_N-1 gets K_PRIO_COOP(1), the highest. */
		const int prio = K_PRIO_COOP(ZPRIO_N - i);
		k_thread_create(&s_prio_thread[i], s_prio_stacks[i],
				K_THREAD_STACK_SIZEOF(s_prio_stacks[i]),
				prio_entry, (void *)(uintptr_t)i, NULL, NULL,
				prio, 0, K_NO_WAIT);
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
	for (uint8_t i = 0; i < ZPRIO_N; i++) {
		k_sem_give(&s_prio_release);
	}
	for (uint8_t i = 0; i < ZPRIO_N; i++) {
		if (k_sem_take(&s_prio_all, K_FOREVER) != 0) {
			return BENCH_PORT;
		}
	}
	int ok = (s_prio_n == ZPRIO_N);
	for (uint32_t i = 0; ok && i < ZPRIO_N; i++) {
		if (s_prio_trace[i] != (uint8_t)(ZPRIO_N - 1 - i)) {
			ok = 0;
		}
	}
	for (uint8_t i = 0; i < ZPRIO_N; i++) {
		k_sem_give(&s_prio_ack);
	}
	*ticks = (ok != 0) ? 1u : 0u;
	return BENCH_OK;
}

static bench_status port_prio_destroy(void *ctx)
{
	(void)ctx;
	if (s_prio_active) {
		s_prio_quit = 1;
		for (uint8_t i = 0; i < ZPRIO_N; i++) {
			k_sem_give(&s_prio_release);
		}
		for (uint8_t i = 0; i < ZPRIO_N; i++) {
			k_thread_join(&s_prio_thread[i], K_FOREVER);
		}
		s_prio_active = 0;
	}
	return BENCH_OK;
}

/* Case 25: maximum interrupt-disabled time proxy. TIM5 is a one-pulse probe at
 * NVIC preempt priority 5, which irq_lock() masks (Zephyr reserves priority 0
 * for zero-latency IRQs and 1 for TIM4's load ISR in case 11); measure() arms it
 * for ~50 us and returns the delay beyond that period. MAXDIS_CONTROL=1 adds a
 * calibrated ~80 us irq_lock section as the positive control. TIM3/TIM4 serve
 * case 11, SysTick is the kernel tick, so TIM5 is free; APB1 timer clock is
 * 84 MHz (ARR+1 = 4200 -> 50 us) and DWT counts at 168 MHz. */
#define MAXDIS_PERIOD_TICKS       8400U  /* (4200 counts)*2 at 168/84 MHz */
#define MAXDIS_CONTROL_CRIT_TICKS 13440U /* 80 us at 168 MHz */

static struct k_sem s_maxdis_sem;
static volatile uint32_t s_maxdis_isr_ts;

static void maxdis_isr(const void *arg)
{
	(void)arg;
	if ((TIM5->SR & TIM_SR_UIF) == 0U) {
		return;
	}
	TIM5->SR = ~TIM_SR_UIF;
	s_maxdis_isr_ts = DWT->CYCCNT;
	k_sem_give(&s_maxdis_sem);
}

static bench_status port_maxdis_create(void *ctx)
{
	(void)ctx;
	k_sem_init(&s_maxdis_sem, 0, 1);
	pjit_dwt_enable();
	RCC->APB1ENR |= RCC_APB1ENR_TIM5EN;
	TIM5->CR1 = 0;
	TIM5->PSC = 0;
	TIM5->ARR = (MAXDIS_PERIOD_TICKS / 2U) - 1U; /* 4199 -> 4200 counts = 50 us */
	TIM5->DIER = TIM_DIER_UIE;
	TIM5->SR = 0;
	IRQ_CONNECT(TIM5_IRQn, 5, maxdis_isr, NULL, 0);
	NVIC_ClearPendingIRQ(TIM5_IRQn);
	irq_enable(TIM5_IRQn);
	return BENCH_OK;
}

static bench_status port_maxdis_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_PORT;
	}
	(void)k_sem_take(&s_maxdis_sem, K_NO_WAIT); /* drain a stale give */
	const uint32_t t0 = DWT->CYCCNT;
	TIM5->CNT = 0;
	TIM5->SR = 0;
	TIM5->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
#if defined(MAXDIS_CONTROL) && MAXDIS_CONTROL
	unsigned int key = irq_lock();
	const uint32_t c0 = DWT->CYCCNT;
	while ((DWT->CYCCNT - c0) < MAXDIS_CONTROL_CRIT_TICKS) {
	}
	irq_unlock(key);
#endif
	if (k_sem_take(&s_maxdis_sem, K_FOREVER) != 0) {
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
	irq_disable(TIM5_IRQn);
	TIM5->CR1 = 0;
	TIM5->DIER = 0;
	NVIC_ClearPendingIRQ(TIM5_IRQn);
	return BENCH_OK;
}

/* Case 20: nested interrupt overhead. Spare NVIC line 60 (preempt priority 4)
 * runs a fixed-work spin and force-pends line 61 (preempt priority 1) ten
 * times; measure() = min(nested) - min(baseline) over three runs each. TIM3/4/5
 * serve cases 11/25 and line 62 is case 10, so 60/61 are free. NESTED_CONTROL=1
 * disables the higher line. DWT counts at 168 MHz (same base as case 25). */
#define NESTED_LOW_IRQ  60
#define NESTED_HIGH_IRQ 61
#define NESTED_SPIN  1000U
#define NESTED_PENDS 10U
#ifndef NESTED_CONTROL
#define NESTED_CONTROL 0
#endif

static volatile uint32_t s_nested_low_done;
static volatile uint32_t s_nested_pend_h;

static void nested_low_isr(const void *arg)
{
	(void)arg;
	const uint32_t step = NESTED_SPIN / (NESTED_PENDS + 1U);
	uint32_t next = step;
	for (volatile uint32_t i = 0; i < NESTED_SPIN; i++) {
#if !NESTED_CONTROL
		if (s_nested_pend_h && i == next && next <= NESTED_PENDS * step) {
			NVIC_SetPendingIRQ(NESTED_HIGH_IRQ); /* higher line preempts */
			next += step;
		}
#endif
	}
	s_nested_low_done = 1;
}

static void nested_high_isr(const void *arg)
{
	(void)arg;
}

static bench_status port_nested_create(void *ctx)
{
	(void)ctx;
	s_nested_low_done = 0;
	s_nested_pend_h = 0;
	pjit_dwt_enable();
	IRQ_CONNECT(NESTED_LOW_IRQ, 4, nested_low_isr, NULL, 0);
	IRQ_CONNECT(NESTED_HIGH_IRQ, 1, nested_high_isr, NULL, 0);
	NVIC_ClearPendingIRQ(NESTED_LOW_IRQ);
	NVIC_ClearPendingIRQ(NESTED_HIGH_IRQ);
	irq_enable(NESTED_LOW_IRQ);
	irq_enable(NESTED_HIGH_IRQ);
	return BENCH_OK;
}

static uint32_t nested_run(uint32_t pend_h)
{
	s_nested_low_done = 0;
	s_nested_pend_h = pend_h;
	const uint32_t t0 = DWT->CYCCNT;
	NVIC_SetPendingIRQ(NESTED_LOW_IRQ);
	while (s_nested_low_done == 0) {
	}
	return DWT->CYCCNT - t0;
}

static bench_status port_nested_measure(void *ctx, uint64_t *ticks)
{
	(void)ctx;
	if (ticks == NULL) {
		return BENCH_INVALID;
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
	irq_disable(NESTED_LOW_IRQ);
	irq_disable(NESTED_HIGH_IRQ);
	NVIC_ClearPendingIRQ(NESTED_LOW_IRQ);
	NVIC_ClearPendingIRQ(NESTED_HIGH_IRQ);
	return BENCH_OK;
}

/* Case 24: heap fragmentation against Zephyr's kernel system heap
 * (CONFIG_HEAP_MEM_POOL_SIZE, 8 KB; k_malloc/k_free). Allocate 256-byte
 * blocks until only ~1/8 of the baseline free memory remains, free every other
 * block to leave non-adjacent holes that cannot coalesce, then probe the
 * largest single allocation with a k_malloc binary search. value =
 * round(1e6*(1 - largest_free_block/total_free)). Freeing all blocks
 * coalesces them, so the same probe then reads ~0 (control). Case 14
 * alloc_latency measures per-call cost on the same heap; this measures its
 * fragmentation response. */
#define FRAG_N 1024
#define FRAG_BYTES 256u
static void *s_frag_blocks[FRAG_N];

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
	struct sys_memory_stats stats;
	if (	sys_heap_runtime_stats_get(&_system_heap.heap, &stats) != 0 || stats.free_bytes == 0u) {
		return BENCH_UNSUPPORTED;
	}
	const size_t baseline = stats.free_bytes;
	size_t margin = baseline / 8u;
	if (margin < 1024u) {
		margin = 1024u;
	}
	uint32_t allocated = 0;
	while (allocated < FRAG_N &&
	       (size_t)allocated * FRAG_BYTES + margin < baseline) {
		void *b = k_malloc(FRAG_BYTES);
		if (b == NULL) {
			break;
		}
		s_frag_blocks[allocated++] = b;
	}
	if (allocated < 8u) {
		for (uint32_t i = 0; i < allocated; ++i) {
			k_free(s_frag_blocks[i]);
			s_frag_blocks[i] = NULL;
		}
		return BENCH_PORT;
	}
	for (uint32_t i = 0; i < allocated; i += 2) {
		k_free(s_frag_blocks[i]);
		s_frag_blocks[i] = NULL;
	}
	if (	sys_heap_runtime_stats_get(&_system_heap.heap, &stats) != 0) {
		for (uint32_t i = 1; i < allocated; i += 2) {
			k_free(s_frag_blocks[i]);
			s_frag_blocks[i] = NULL;
		}
		return BENCH_PORT;
	}
	const size_t total = stats.free_bytes;
	size_t lo = 0, hi = total;
	while (lo < hi) {
		size_t mid = lo + (hi - lo + 1) / 2;
		void *probe = k_malloc(mid);
		if (probe != NULL) {
			k_free(probe);
			lo = mid;
		} else {
			hi = mid - 1;
		}
	}
	const size_t largest = lo;
	for (uint32_t i = 1; i < allocated; i += 2) {
		k_free(s_frag_blocks[i]);
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
			k_free(s_frag_blocks[i]);
			s_frag_blocks[i] = NULL;
		}
	}
	return BENCH_OK;
}

bench_status zephyr_bench_port_init(bench_port *port)
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
	port->ticks_per_second = CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC;
#ifndef BENCH_LANGUAGE_RUST
	/* Kernel-object cases are native in the Rust application core
	 * (firmware/c-kernels/zephyr/rust); only the C runner supplies them. */
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
#endif /* !BENCH_LANGUAGE_RUST */
	/* Timer/ISR register harness stays in C for both languages. */
	port->isr_create = port_isr_create;
	port->isr_measure = port_isr_measure;
	port->isr_destroy = port_isr_destroy;
	port->trigger_isr_create = port_trigger_isr_create;
	port->trigger_isr_measure = port_trigger_isr_measure;
	port->trigger_isr_destroy = port_trigger_isr_destroy;
	port->pjit_create = port_pjit_create;
	port->pjit_measure = port_pjit_measure;
	port->pjit_destroy = port_pjit_destroy;
	port->maxdis_create = port_maxdis_create;
	port->maxdis_measure = port_maxdis_measure;
	port->maxdis_destroy = port_maxdis_destroy;
	port->nested_create = port_nested_create;
	port->nested_measure = port_nested_measure;
	port->nested_destroy = port_nested_destroy;
	port->frag_create = port_frag_create;
	port->frag_measure = port_frag_measure;
	port->frag_destroy = port_frag_destroy;
	return BENCH_OK;
}

/* --------------------------------------------------- Rust application core */
#ifdef BENCH_LANGUAGE_RUST
/* Thin shims for firmware/zephyr/rust. Zephyr's k_* API is macro/inline and
 * takes k_timeout_t structs by value, so a direct Rust FFI call is not
 * ABI-stable. These wrappers reuse the exact port operations the C path reaches
 * through its vtable, giving both languages the same Zephyr primitive calls. */
uint64_t bench_rust_now_ticks(void)
{
	uint64_t ticks = 0;
	(void)port_now_ticks(NULL, &ticks);
	return ticks;
}

uint64_t bench_rust_elapsed_ticks(uint64_t start, uint64_t end)
{
	/* This port exposes a 64-bit monotonic tick value. */
	return end - start;
}

uint64_t bench_rust_ticks_per_second(void)
{
	return (uint64_t)CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC;
}

int bench_rust_coop_prio(int x)
{
	return (int)K_PRIO_COOP(x);
}

int64_t bench_rust_ms_to_ticks(uint32_t ms)
{
	return (int64_t)k_ms_to_ticks_ceil64(ms);
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
	k_sem_give(&s_handoff_signal);
	return 0;
}

int bench_rust_handoff_wait(void)
{
	return (k_sem_take(&s_handoff_done, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
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

int64_t bench_rust_uptime_ticks(void)
{
	return k_uptime_ticks();
}

void bench_rust_sleep_abs_ticks(int64_t target)
{
	k_sleep(K_TIMEOUT_ABS_TICKS(target));
}

uint64_t bench_rust_jitter_period(void)
{
	return (uint64_t)(CONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC / CONFIG_SYS_CLOCK_TICKS_PER_SEC);
}

int bench_rust_isr_create(void)
{
	return (int)port_isr_create(NULL);
}

int bench_rust_isr_arm(void)
{
	k_timer_start(&s_isr_timer, K_USEC(20), K_NO_WAIT);
	return 0;
}

int bench_rust_isr_wait(void)
{
	return (k_sem_take(&s_isr_sem, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_isr_entry(void)
{
	return s_isr_entry;
}

int bench_rust_isr_destroy(void)
{
	return (int)port_isr_destroy(NULL);
}

int bench_rust_trigger_isr_create(void)
{
	return (int)port_trigger_isr_create(NULL);
}

int bench_rust_trig_drain(void)
{
	(void)k_sem_take(&s_trig_sem, K_NO_WAIT);
	return 0;
}

uint64_t bench_rust_trig_fire(void)
{
	s_trig_state = 1;
	unsigned int key = irq_lock();
	uint64_t t0 = k_cycle_get_64();
	NVIC_SetPendingIRQ(TRIGGER_IRQ);
	irq_unlock(key);
	return t0;
}

int bench_rust_trig_wait(void)
{
	if (k_sem_take(&s_trig_sem, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	return (s_trig_state != 2) ? (int)BENCH_CORRUPT : 0;
}

uint64_t bench_rust_trig_entry(void)
{
	return s_trig_entry;
}

int bench_rust_trigger_isr_destroy(void)
{
	return (int)port_trigger_isr_destroy(NULL);
}

int bench_rust_pjit_create(void)
{
	return (int)port_pjit_create(NULL);
}

int bench_rust_pjit_wait(void)
{
	return (k_sem_take(&s_pjit_sem, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_pjit_read(uint64_t *ticks)
{
	if (!s_pjit_made || ticks == NULL) {
		return (int)BENCH_PORT;
	}
	uint32_t wake = DWT->CYCCNT;
	uint32_t n = s_pjit_n;
	s_pjit_n = n + 1U;
	if (s_pjit_releases > (n + 1U)) {
		/* A release was coalesced: resync the sample index so one transient
		 * backlog does not poison every following sample of the case. */
		s_pjit_n = s_pjit_releases;
		return (int)BENCH_CORRUPT;
	}
	uint32_t ideal = s_pjit_r0 + n * PJIT_P_CYCLES;
	uint32_t err = wake - ideal;
	if ((int32_t)err < 0) {
		return (int)BENCH_CORRUPT;
	}
	*ticks = (uint64_t)err;
	return 0;
}

int bench_rust_pjit_destroy(void)
{
	return (int)port_pjit_destroy(NULL);
}

int bench_rust_sem_create(void)
{
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

int bench_rust_sem_post(void)
{
	k_sem_give(&s_api_sem);
	return 0;
}

int bench_rust_sem_wait(void)
{
	return (k_sem_take(&s_api_sem, K_NO_WAIT) != 0) ? (int)BENCH_CORRUPT : 0;
}

int bench_rust_mutex_u_create(void)
{
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
	k_mutex_lock(&s_api_mutex, K_FOREVER);
	return 0;
}

int bench_rust_mutex_u_unlock(void)
{
	k_mutex_unlock(&s_api_mutex);
	return 0;
}

int bench_rust_alloc_create(void)
{
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

void *bench_rust_kalloc(size_t n)
{
	return k_malloc(n);
}

void bench_rust_kfree(void *p)
{
	k_free(p);
}

int bench_rust_sleep_create(void)
{
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

int bench_rust_nanosleep_ms(unsigned int ms)
{
	k_msleep((int)ms);
	return 0;
}

int bench_rust_yield_create(void)
{
	return (int)port_yield_create(NULL);
}

int bench_rust_yield_now(void)
{
	return (int)port_yield_now(NULL);
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
	k_sem_give(&s_preempt_sem);
	return 0;
}

uint64_t bench_rust_preempt_wake(void)
{
	return s_preempt_wake;
}

int bench_rust_ipc_create(void)
{
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

int bench_rust_ipc_send(uint32_t value)
{
	if (!s_ipc_active) {
		return (int)BENCH_PORT;
	}
	uint32_t v = value;
	return (k_msgq_put(&s_ipc_req, &v, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_ipc_recv(uint32_t value, uint32_t *received_value)
{
	if (k_msgq_get(&s_ipc_rep, received_value, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	return (*received_value == value) ? 0 : (int)BENCH_CORRUPT;
}

int bench_rust_ipc_payload_create(void)
{
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
	int idx = ipc_payload_index(payload_bytes);
	if (!s_ipc_payload_active || idx < 0) {
		return (int)BENCH_PORT;
	}
	return (k_msgq_put(&s_ipc_payload_req[idx], s_ipc_payload_tx, K_FOREVER) != 0)
		       ? (int)BENCH_PORT
		       : 0;
}

int bench_rust_ipc_payload_recv(uint32_t payload_bytes)
{
	int idx = ipc_payload_index(payload_bytes);
	if (idx < 0) {
		return (int)BENCH_PORT;
	}
	if (k_msgq_get(&s_ipc_payload_rep[idx], s_ipc_payload_rx, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
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
	return (int)port_tp_create(NULL);
}

uint32_t bench_rust_tp_count(void)
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
	return (k_msgq_put(&s_tp_q, s_tp_tx, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_tp_done(void)
{
	return (k_sem_take(&s_tp_done, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_zcopy_create(void)
{
	return (int)port_zcopy_create(NULL);
}

int bench_rust_zcopy_reset(void)
{
	if (!s_zcopy_active) {
		return (int)BENCH_PORT;
	}
	for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
		s_zcopy_tx[i] = (uint8_t)(0x3Cu ^ i);
		s_zcopy_shared[i] = (uint8_t)(0x3Cu ^ i);
	}
	return 0;
}

int bench_rust_zcopy_copied(uint64_t *ticks)
{
	uint64_t start = k_cycle_get_64();
	if (k_msgq_put(&s_zcopy_cp_req, s_zcopy_tx, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	if (k_msgq_get(&s_zcopy_cp_rep, s_zcopy_rx, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	uint64_t copied = k_cycle_get_64() - start;
	for (uint32_t i = 0; i < ZCOPY_BYTES; i++) {
		if (s_zcopy_rx[i] != s_zcopy_tx[i]) {
			return (int)BENCH_CORRUPT;
		}
	}
	*ticks = copied;
	return 0;
}

int bench_rust_zcopy_ref(uint64_t *ticks)
{
	void *ref = s_zcopy_shared;
	void *back = NULL;
	uint64_t start = k_cycle_get_64();
	if (k_msgq_put(&s_zcopy_zc_req, &ref, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	if (k_msgq_get(&s_zcopy_zc_rep, &back, K_FOREVER) != 0) {
		return (int)BENCH_PORT;
	}
	*ticks = k_cycle_get_64() - start;
	if (back != s_zcopy_shared) {
		return (int)BENCH_CORRUPT;
	}
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

int bench_rust_zcopy_destroy(void)
{
	return (int)port_zcopy_destroy(NULL);
}

int bench_rust_tp_destroy(void)
{
	return (int)port_tp_destroy(NULL);
}

int bench_rust_mutex_create(void)
{
	return (int)port_mutex_create(NULL);
}

int bench_rust_mutex_ready(void)
{
	return s_m_made ? 1 : 0;
}

int bench_rust_mutex_reset(void)
{
	s_m_stop = 0;
	return 0;
}

int bench_rust_mutex_req(void)
{
	k_sem_give(&s_m_req);
	return 0;
}

int bench_rust_mutex_held(void)
{
	return (k_sem_take(&s_m_held, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_go(void)
{
	k_sem_give(&s_m_go);
	return 0;
}

int bench_rust_mutex_rel(void)
{
	k_sem_give(&s_m_rel);
	return 0;
}

int bench_rust_mutex_lock(void)
{
	return (k_mutex_lock(&s_mutex, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_set_stop(void)
{
	s_m_stop = 1;
	return 0;
}

int bench_rust_mutex_done(void)
{
	return (k_sem_take(&s_m_done, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_mutex_unlock(void)
{
	k_mutex_unlock(&s_mutex);
	return 0;
}

int bench_rust_mutex_destroy(void)
{
	return (int)port_mutex_destroy(NULL);
}

int bench_rust_timer_create(void)
{
	return (int)port_timer_create(NULL);
}

int bench_rust_timer_drain(void)
{
	(void)k_sem_take(&s_timer_sem, K_NO_WAIT);
	return 0;
}

int bench_rust_timer_arm(void)
{
	k_timer_start(&s_timer, K_MSEC(10), K_NO_WAIT);
	return 0;
}

int bench_rust_timer_wait(void)
{
	return (k_sem_take(&s_timer_sem, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_timer_fire(void)
{
	return s_timer_fire;
}

int bench_rust_timer_destroy(void)
{
	return (int)port_timer_destroy(NULL);
}
int bench_rust_sched_create(void) { return (int)port_sched_create(NULL); }
int bench_rust_sched_measure(uint64_t *ticks) { return (int)port_sched_measure(NULL, ticks); }
int bench_rust_sched_destroy(void) { return (int)port_sched_destroy(NULL); }

int bench_rust_sched_yield(void)
{
	k_yield();
	return 0;
}
int bench_rust_notify_create(void) { return (int)port_notify_create(NULL); }
int bench_rust_notify_drain(void)
{
	if (!s_notify_active) {
		return (int)BENCH_PORT;
	}
	while (k_sem_take(&s_notify_done, K_NO_WAIT) == 0) {
	}
	return 0;
}

int bench_rust_notify_go(void)
{
	k_sem_give(&s_notify_go);
	return 0;
}

int bench_rust_notify_done(void)
{
	return (k_sem_take(&s_notify_done, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_notify_signal(void)
{
	return s_notify_signal;
}
int bench_rust_notify_destroy(void) { return (int)port_notify_destroy(NULL); }
int bench_rust_prio_create(void) { return (int)port_prio_create(NULL); }
int bench_rust_prio_ready(void)
{
	return s_prio_active ? 1 : 0;
}

uint32_t bench_rust_prio_n(void)
{
	return ZPRIO_N;
}

int bench_rust_prio_reset(void)
{
	s_prio_n = 0;
	return 0;
}

int bench_rust_prio_release(void)
{
	k_sem_give(&s_prio_release);
	return 0;
}

int bench_rust_prio_all(void)
{
	return (k_sem_take(&s_prio_all, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

int bench_rust_prio_check(void)
{
	if (s_prio_n != ZPRIO_N) {
		return 1;
	}
	for (uint32_t i = 0; i < ZPRIO_N; i++) {
		if (s_prio_trace[i] != (uint8_t)(ZPRIO_N - 1 - i)) {
			return 1;
		}
	}
	return 0;
}

int bench_rust_prio_ack(void)
{
	k_sem_give(&s_prio_ack);
	return 0;
}
int bench_rust_prio_destroy(void) { return (int)port_prio_destroy(NULL); }
int bench_rust_frag_create(void) { return (int)port_frag_create(NULL); }
int64_t bench_rust_frag_baseline(void)
{
	struct sys_memory_stats stats;
	if (sys_heap_runtime_stats_get(&_system_heap.heap, &stats) != 0) {
		return -1;
	}
	return (int64_t)stats.free_bytes;
}

int64_t bench_rust_frag_free_bytes(void)
{
	struct sys_memory_stats stats;
	if (sys_heap_runtime_stats_get(&_system_heap.heap, &stats) != 0) {
		return -1;
	}
	return (int64_t)stats.free_bytes;
}

uint32_t bench_rust_frag_bytes(void)
{
	return FRAG_BYTES;
}

uint32_t bench_rust_frag_n(void)
{
	return FRAG_N;
}

int bench_rust_frag_alloc(uint32_t idx)
{
	if (idx >= FRAG_N) {
		return 1;
	}
	void *b = k_malloc(FRAG_BYTES);
	if (b == NULL) {
		return 1;
	}
	s_frag_blocks[idx] = b;
	return 0;
}

void bench_rust_frag_free(uint32_t idx)
{
	if (idx < FRAG_N && s_frag_blocks[idx] != NULL) {
		k_free(s_frag_blocks[idx]);
		s_frag_blocks[idx] = NULL;
	}
}

int bench_rust_frag_try(uint32_t bucket)
{
	void *p = k_malloc(bucket);
	if (p == NULL) {
		return 0;
	}
	k_free(p);
	return 1;
}
int bench_rust_frag_destroy(void) { return (int)port_frag_destroy(NULL); }
int bench_rust_maxdis_create(void) { return (int)port_maxdis_create(NULL); }
uint64_t bench_rust_maxdis_begin(void)
{
	(void)k_sem_take(&s_maxdis_sem, K_NO_WAIT); /* drain a stale give */
	return (uint64_t)DWT->CYCCNT;
}

int bench_rust_maxdis_arm(void)
{
	TIM5->CNT = 0;
	TIM5->SR = 0;
	TIM5->CR1 = TIM_CR1_OPM | TIM_CR1_URS | TIM_CR1_CEN;
	return 0;
}

int bench_rust_maxdis_wait(uint64_t t0)
{
	(void)t0;
	return (k_sem_take(&s_maxdis_sem, K_FOREVER) != 0) ? (int)BENCH_PORT : 0;
}

uint64_t bench_rust_maxdis_isr_ts(void)
{
	return (uint64_t)s_maxdis_isr_ts;
}
int bench_rust_maxdis_destroy(void) { return (int)port_maxdis_destroy(NULL); }
int bench_rust_nested_create(void) { return (int)port_nested_create(NULL); }
uint32_t bench_rust_nested_run(uint32_t pend_h) { return nested_run(pend_h); }
int bench_rust_nested_destroy(void) { return (int)port_nested_destroy(NULL); }
/* Complete C measurement paths exposed to the Rust adapter.  Rust calls these
 * instead of sequencing begin/arm/wait through separate FFI calls, so both
 * languages use the same timestamp boundaries. */
bench_status bench_isr_measure(uint64_t *ticks) { return port_isr_measure(NULL, ticks); }
bench_status bench_pjit_measure(uint64_t *ticks) { return port_pjit_measure(NULL, ticks); }
bench_status bench_maxdis_measure(uint64_t *ticks) { return port_maxdis_measure(NULL, ticks); }

#endif /* BENCH_LANGUAGE_RUST */
