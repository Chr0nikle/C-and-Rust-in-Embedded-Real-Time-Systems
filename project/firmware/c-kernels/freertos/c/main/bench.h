#ifndef BENCH_H
#define BENCH_H
/* Native benchmark adapter + session wiring for the ESP32-C6 FreeRTOS bench.
 *
 * Implements the bench_port (esp_timer microseconds + FreeRTOS queue) and
 * bench_sink (bounded sample buffer) required by
 * firmware/common/c/include/rtos_bench.h, and drives bench_run_suite from the
 * protocol v1 `start` frame.
 *
 * Protocol v1 frames emitted on a successful run (UART protocol v1):
 *   started      -> run_id, build_id
 *   measurement  -> run_id, case, index, ticks, ticks_per_second  (per sample)
 *   complete     -> run_id, per_case_counts, status="ok"
 * On failure: partial measurements (if any) followed by an error frame;
 * complete is never emitted for a failed run.
 */
#include <stdint.h>
#include <stddef.h>

/* Shared build_id token. main.c also defines this with the same value; the
 * #ifndef keeps a single source of truth if the build system overrides it. */
#ifndef BENCH_BUILD_ID
#define BENCH_BUILD_ID "freertos-c6-bench-nogit"
#endif

/* Validate a `start` frame before acknowledging it (protocol.md:20).
 * run_id: 1-64 ASCII [A-Za-z0-9_-]. build_id must match BENCH_BUILD_ID.
 * cases:  pointer to an array of n_cases uint32 IDs (0=timer_overhead,
 *         1=queue_roundtrip); each must be a supported case ID.
 * reps:   nonzero sample count. warmup: discarded warm-up iterations.
 * On success returns 0 and leaves err_code/err_msg NULL.
 * On failure returns -1 and sets *err_code (a stable error token) and
 * *err_msg (human-readable) for the caller to emit as an error frame. */
int bench_validate_start(const char *run_id, const char *build_id,
                         const uint32_t *cases, size_t n_cases,
                         uint32_t reps, uint32_t warmup,
                         const char **err_code, const char **err_msg);

/* Execute the validated suite. Emits `started`, one `measurement` per sample
 * (flushed between cases), and `complete` on success. On failure flushes any
 * partial measurements already buffered and emits an `error` frame.
 * Returns 0 on success, -1 on failure. */
int bench_run(const char *run_id, const char *build_id,
              const uint32_t *cases, size_t n_cases,
              uint32_t reps, uint32_t warmup);

#endif
