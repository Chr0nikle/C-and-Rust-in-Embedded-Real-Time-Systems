//! Shared, RTOS-independent arena allocator for the portable fragmentation case.
//!
//! Mirror of `firmware/common/c/include/bench_arena.h`; the algorithm, offsets
//! and constants must stay identical so C and Rust report the same value. No OS
//! calls and no heap: pure computation over a fixed 8 KiB arena. Offsets (not
//! pointers) identify blocks. See the C header for the physical layout.

pub const ARENA_SIZE: u32 = 8192;
pub const WORDS: usize = (ARENA_SIZE / 4) as usize;
const HDR: u32 = 8;
const MIN: u32 = 16;
const NIL: u32 = 0xFFFF_FFFF;
const FREE: u32 = 0x1;
const PREVFREE: u32 = 0x2;
const SIZEMASK: u32 = 0xFFFF_FFF8;

pub struct Arena {
    buf: [u32; WORDS],
    free_head: u32,
}

impl Default for Arena {
    fn default() -> Self {
        Self::new()
    }
}

impl Arena {
    pub fn new() -> Arena {
        Arena {
            buf: [0; WORDS],
            free_head: 0,
        }
    }
    fn size(&self, off: u32) -> u32 {
        self.buf[(off / 4) as usize] & SIZEMASK
    }
    /// Total block size (including the 8-byte header) at byte offset `off`.
    pub fn block_size(&self, off: u32) -> u32 {
        self.size(off)
    }
    fn next(&self, off: u32) -> u32 {
        self.buf[(off / 4) as usize + 2]
    }
    fn prev(&self, off: u32) -> u32 {
        self.buf[(off / 4) as usize + 3]
    }
    fn set_link(&mut self, off: u32, next: u32, prev: u32) {
        self.buf[(off / 4) as usize + 2] = next;
        self.buf[(off / 4) as usize + 3] = prev;
    }
    fn insert(&mut self, off: u32) {
        let mut cur = self.free_head;
        let mut prev = NIL;
        while cur != NIL && cur < off {
            prev = cur;
            cur = self.next(cur);
        }
        self.set_link(off, cur, prev);
        if prev == NIL {
            self.free_head = off;
        } else {
            self.buf[(prev / 4) as usize + 2] = off;
        }
        if cur != NIL {
            self.buf[(cur / 4) as usize + 3] = off;
        }
    }
    fn remove(&mut self, off: u32) {
        let next = self.next(off);
        let prev = self.prev(off);
        if prev == NIL {
            self.free_head = next;
        } else {
            self.buf[(prev / 4) as usize + 2] = next;
        }
        if next != NIL {
            self.buf[(next / 4) as usize + 3] = prev;
        }
    }
    pub fn init(&mut self) {
        for w in self.buf.iter_mut() {
            *w = 0;
        }
        let usable_size = ARENA_SIZE - HDR;
        self.buf[0] = usable_size | FREE;
        self.buf[1] = 0;
        self.set_link(0, NIL, NIL);
        self.buf[(usable_size / 4) as usize] = HDR | PREVFREE;
        self.buf[(usable_size / 4) as usize + 1] = usable_size;
        self.free_head = 0;
    }
    pub fn alloc(&mut self, size: u32) -> u32 {
        let mut need_size = ((size + 7) & !7) + HDR;
        if need_size < MIN {
            need_size = MIN;
        }
        let mut cur = self.free_head;
        while cur != NIL && self.size(cur) < need_size {
            cur = self.next(cur);
        }
        if cur == NIL {
            return 0;
        }
        let block_size = self.size(cur);
        let flags = self.buf[(cur / 4) as usize] & PREVFREE;
        if block_size - need_size >= MIN {
            let rem_off = cur + need_size;
            let next_off = cur + block_size;
            self.remove(cur);
            self.buf[(cur / 4) as usize] = need_size | flags;
            self.buf[(rem_off / 4) as usize] = (block_size - need_size) | FREE;
            self.buf[(rem_off / 4) as usize + 1] = need_size;
            self.buf[(next_off / 4) as usize] |= PREVFREE;
            self.buf[(next_off / 4) as usize + 1] = block_size - need_size;
            self.insert(rem_off);
        } else {
            let next_off = cur + block_size;
            self.remove(cur);
            self.buf[(cur / 4) as usize] = block_size | flags;
            self.buf[(next_off / 4) as usize] &= !PREVFREE;
        }
        cur + HDR
    }
    pub fn free(&mut self, handle: u32) {
        if handle < HDR {
            return;
        }
        let mut off = handle - HDR;
        let mut block_size = self.size(off);
        let prev_free = self.buf[(off / 4) as usize] & PREVFREE;
        self.buf[(off / 4) as usize] = block_size | FREE | prev_free;
        let mut next_off = off + block_size;
        self.buf[(next_off / 4) as usize] |= PREVFREE;
        self.buf[(next_off / 4) as usize + 1] = block_size;
        let prev_size = self.buf[(off / 4) as usize + 1];
        if prev_free != 0 && prev_size != 0 {
            let prev_off = off - prev_size;
            self.remove(prev_off);
            block_size += prev_size;
            off = prev_off;
            self.buf[(off / 4) as usize] =
                block_size | FREE | (self.buf[(off / 4) as usize] & PREVFREE);
            next_off = off + block_size;
            self.buf[(next_off / 4) as usize + 1] = block_size;
        }
        next_off = off + block_size;
        if self.buf[(next_off / 4) as usize] & FREE != 0 {
            let next_size = self.size(next_off);
            self.remove(next_off);
            block_size += next_size;
            self.buf[(off / 4) as usize] =
                block_size | FREE | (self.buf[(off / 4) as usize] & PREVFREE);
            next_off = off + block_size;
            self.buf[(next_off / 4) as usize] |= PREVFREE;
            self.buf[(next_off / 4) as usize + 1] = block_size;
        }
        self.insert(off);
    }
    pub fn stats(&self) -> (u32, u32) {
        let mut total = 0;
        let mut largest = 0;
        let mut cur = self.free_head;
        while cur != NIL {
            let block_size = self.size(cur);
            total += block_size;
            if block_size > largest {
                largest = block_size;
            }
            cur = self.next(cur);
        }
        (total, largest)
    }
}

fn fragmentation(total: u32, largest: u32) -> u32 {
    if total == 0 {
        return 0;
    }
    1_000_000 - (u64::from(largest) * 1_000_000 / u64::from(total)) as u32
}

/// Run the fragmentation pattern into a caller-provided arena. `coalesce ==
/// false` frees every other allocated block before measuring; `coalesce == true`
/// frees all blocks first (the coalesced control). Mirrors
/// `bench_arena_measure` in the C header.
pub fn measure(a: &mut Arena, coalesce: bool) -> u32 {
    let mut handles = [0u32; 64];
    let mut handle_count = 0usize;
    a.init();
    while handle_count < handles.len() {
        let handle = a.alloc(256);
        if handle == 0 {
            break;
        }
        handles[handle_count] = handle;
        handle_count += 1;
    }
    if handle_count < 2 {
        return 0;
    }
    if coalesce {
        for &handle in handles.iter().take(handle_count) {
            a.free(handle);
        }
    } else {
        let mut i = 0;
        while i < handle_count {
            a.free(handles[i]);
            i += 2;
        }
    }
    let (total, largest) = a.stats();
    fragmentation(total, largest)
}

/// BSS scratch arena for the firmware runner: the 8 KiB buffer must not live on
/// a small RTOS task stack. Single-threaded benchmark use only.
static mut SCRATCH: Arena = Arena {
    buf: [0; WORDS],
    free_head: 0,
};

/// Firmware entry point using the BSS scratch arena.
pub fn measure_static(coalesce: bool) -> u32 {
    // Safety: the benchmark runner calls this from one task with no concurrency.
    let a = unsafe { &mut *core::ptr::addr_of_mut!(SCRATCH) };
    measure(a, coalesce)
}

/// Convenience for host tests: a local arena (large host stacks).
pub fn run(coalesce: bool) -> u32 {
    let mut a = Arena::new();
    measure(&mut a, coalesce)
}
