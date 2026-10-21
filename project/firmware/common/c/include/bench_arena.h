#ifndef BENCH_ARENA_H
#define BENCH_ARENA_H
/* Shared, RTOS-independent arena allocator for the portable fragmentation case.
 *
 * This is deliberately self-contained `no_std`-style code with no OS calls, so
 * the identical algorithm runs on every board/RTOS and in Rust. It is a
 * portability control: with a fixed arena size the fragmentation value must be
 * identical on every path. Offsets (not pointers) identify blocks so the C and
 * Rust layouts match exactly.
 *
 * Physical layout (byte offsets, 8-byte aligned; u32 words):
 *   [ size|flags ][ prev_size ][ payload ] ...
 * `size` includes the 8-byte header. Flags live in the low bits of `size`
 * (sizes are multiples of 8): bit0 FREE, bit1 PREVFREE (previous physical block
 * is free). Free blocks store next/prev free-list offsets in the first two
 * payload words. A trailing sentinel header prevents coalescing past the end.
 * The free list is kept address-ordered so first-fit is deterministic. */
#include <stdint.h>

#define BENCH_ARENA_SIZE 8192u
#define BENCH_ARENA_WORDS (BENCH_ARENA_SIZE / 4u)
#define BENCH_ARENA_HDR 8u
#define BENCH_ARENA_MIN 16u
#define BENCH_ARENA_NIL 0xFFFFFFFFu
#define BENCH_ARENA_FREE 0x1u
#define BENCH_ARENA_PREVFREE 0x2u
#define BENCH_ARENA_SIZEMASK 0xFFFFFFF8u

typedef struct {
    uint32_t buf[BENCH_ARENA_WORDS];
    uint32_t free_head;
} bench_arena;

static inline uint32_t bench_arena_size(const bench_arena *a, uint32_t off) {
    return a->buf[off / 4u] & BENCH_ARENA_SIZEMASK;
}
static inline int bench_arena_is_free(const bench_arena *a, uint32_t off) {
    return (a->buf[off / 4u] & BENCH_ARENA_FREE) != 0u;
}
static inline uint32_t bench_arena_next(const bench_arena *a, uint32_t off) {
    return a->buf[off / 4u + 2u];
}
static inline uint32_t bench_arena_prev(const bench_arena *a, uint32_t off) {
    return a->buf[off / 4u + 3u];
}
static inline void bench_arena_set_link(bench_arena *a, uint32_t off, uint32_t next,
                                        uint32_t prev) {
    a->buf[off / 4u + 2u] = next;
    a->buf[off / 4u + 3u] = prev;
}

static inline void bench_arena_insert(bench_arena *a, uint32_t off) {
    uint32_t cur = a->free_head;
    uint32_t prev = BENCH_ARENA_NIL;
    while (cur != BENCH_ARENA_NIL && cur < off) {
        prev = cur;
        cur = bench_arena_next(a, cur);
    }
    bench_arena_set_link(a, off, cur, prev);
    if (prev == BENCH_ARENA_NIL) {
        a->free_head = off;
    } else {
        a->buf[prev / 4u + 2u] = off;
    }
    if (cur != BENCH_ARENA_NIL) {
        a->buf[cur / 4u + 3u] = off;
    }
}

static inline void bench_arena_remove(bench_arena *a, uint32_t off) {
    uint32_t next = bench_arena_next(a, off);
    uint32_t prev = bench_arena_prev(a, off);
    if (prev == BENCH_ARENA_NIL) {
        a->free_head = next;
    } else {
        a->buf[prev / 4u + 2u] = next;
    }
    if (next != BENCH_ARENA_NIL) {
        a->buf[next / 4u + 3u] = prev;
    }
}

static inline void bench_arena_init(bench_arena *a) {
    uint32_t i;
    for (i = 0; i < BENCH_ARENA_WORDS; ++i) {
        a->buf[i] = 0u;
    }
    uint32_t usable_size = BENCH_ARENA_SIZE - BENCH_ARENA_HDR;
    uint32_t sentinel = usable_size;
    a->buf[0] = usable_size | BENCH_ARENA_FREE;
    a->buf[1] = 0u;
    bench_arena_set_link(a, 0u, BENCH_ARENA_NIL, BENCH_ARENA_NIL);
    a->buf[sentinel / 4u] = BENCH_ARENA_HDR | BENCH_ARENA_PREVFREE;
    a->buf[sentinel / 4u + 1u] = usable_size;
    a->free_head = 0u;
}

static inline uint32_t bench_arena_alloc(bench_arena *a, uint32_t size) {
    uint32_t need_size = ((size + 7u) & ~7u) + BENCH_ARENA_HDR;
    if (need_size < BENCH_ARENA_MIN) {
        need_size = BENCH_ARENA_MIN;
    }
    uint32_t cur = a->free_head;
    while (cur != BENCH_ARENA_NIL && bench_arena_size(a, cur) < need_size) {
        cur = bench_arena_next(a, cur);
    }
    if (cur == BENCH_ARENA_NIL) {
        return 0u;
    }
    uint32_t block_size = bench_arena_size(a, cur);
    uint32_t flags = a->buf[cur / 4u] & BENCH_ARENA_PREVFREE;
    if (block_size - need_size >= BENCH_ARENA_MIN) {
        uint32_t rem_off = cur + need_size;
        uint32_t next_off = cur + block_size;
        bench_arena_remove(a, cur);
        a->buf[cur / 4u] = need_size | flags;
        a->buf[rem_off / 4u] = (block_size - need_size) | BENCH_ARENA_FREE;
        a->buf[rem_off / 4u + 1u] = need_size;
        a->buf[next_off / 4u] |= BENCH_ARENA_PREVFREE;
        a->buf[next_off / 4u + 1u] = block_size - need_size;
        bench_arena_insert(a, rem_off);
    } else {
        uint32_t next_off = cur + block_size;
        bench_arena_remove(a, cur);
        a->buf[cur / 4u] = block_size | flags;
        a->buf[next_off / 4u] &= ~BENCH_ARENA_PREVFREE;
    }
    return cur + BENCH_ARENA_HDR;
}

static inline void bench_arena_free(bench_arena *a, uint32_t handle) {
    if (handle < BENCH_ARENA_HDR) {
        return;
    }
    uint32_t off = handle - BENCH_ARENA_HDR;
    uint32_t block_size = bench_arena_size(a, off);
    uint32_t prev_free = a->buf[off / 4u] & BENCH_ARENA_PREVFREE;
    a->buf[off / 4u] = block_size | BENCH_ARENA_FREE | prev_free;
    uint32_t next_off = off + block_size;
    a->buf[next_off / 4u] |= BENCH_ARENA_PREVFREE;
    a->buf[next_off / 4u + 1u] = block_size;
    uint32_t prev_size = a->buf[off / 4u + 1u];
    if (prev_free != 0u && prev_size != 0u) {
        uint32_t prev_off = off - prev_size;
        bench_arena_remove(a, prev_off);
        block_size += prev_size;
        off = prev_off;
        a->buf[off / 4u] = block_size | BENCH_ARENA_FREE |
                           (a->buf[off / 4u] & BENCH_ARENA_PREVFREE);
        next_off = off + block_size;
        a->buf[next_off / 4u + 1u] = block_size;
    }
    next_off = off + block_size;
    if ((a->buf[next_off / 4u] & BENCH_ARENA_FREE) != 0u) {
        uint32_t next_size = bench_arena_size(a, next_off);
        bench_arena_remove(a, next_off);
        block_size += next_size;
        a->buf[off / 4u] = block_size | BENCH_ARENA_FREE |
                           (a->buf[off / 4u] & BENCH_ARENA_PREVFREE);
        next_off = off + block_size;
        a->buf[next_off / 4u] |= BENCH_ARENA_PREVFREE;
        a->buf[next_off / 4u + 1u] = block_size;
    }
    bench_arena_insert(a, off);
}

static inline void bench_arena_stats(const bench_arena *a, uint32_t *total_free,
                                     uint32_t *largest_free) {
    uint32_t total = 0u;
    uint32_t largest = 0u;
    uint32_t cur = a->free_head;
    while (cur != BENCH_ARENA_NIL) {
        uint32_t block_size = bench_arena_size(a, cur);
        total += block_size;
        if (block_size > largest) {
            largest = block_size;
        }
        cur = bench_arena_next(a, cur);
    }
    *total_free = total;
    *largest_free = largest;
}

/* Fragmented pattern (coalesce == 0): allocate 256-byte payloads until <=20% of
 * the arena is free, then release every other block and report
 * round(1e6 * (1 - largest_free / total_free)). Coalesced control (coalesce == 1)
 * releases every block before measuring. Pure computation, no OS calls. */
static inline uint32_t bench_arena_measure(bench_arena *a, uint32_t coalesce) {
    uint32_t handles[64];
    uint32_t handle_count = 0u;
    bench_arena_init(a);
    while (handle_count < 64u) {
        uint32_t handle = bench_arena_alloc(a, 256u);
        if (handle == 0u) {
            break;
        }
        handles[handle_count++] = handle;
    }
    if (handle_count < 2u) {
        return 0u;
    }
    uint32_t i;
    if (coalesce != 0u) {
        for (i = 0; i < handle_count; ++i) {
            bench_arena_free(a, handles[i]);
        }
    } else {
        for (i = 0u; i < handle_count; i += 2u) {
            bench_arena_free(a, handles[i]);
        }
    }
    uint32_t total_free;
    uint32_t largest_free;
    bench_arena_stats(a, &total_free, &largest_free);
    if (total_free == 0u) {
        return 0u;
    }
    return 1000000u - (uint32_t)(((uint64_t)largest_free * 1000000u) / total_free);
}

/* Host/tests convenience: a local arena (large host stacks). Firmware callers
 * must use bench_arena_measure with a static arena to avoid overflowing a small
 * RTOS task stack with the 8 KiB buffer. */
static inline uint32_t bench_arena_run(uint32_t coalesce) {
    bench_arena a;
    return bench_arena_measure(&a, coalesce);
}

#endif
