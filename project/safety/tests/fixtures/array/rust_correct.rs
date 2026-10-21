use core::ffi::c_void;

extern "C" {
    fn slot_blocks() -> *mut *mut c_void;
}

const SLOT_N: usize = 1024;

unsafe fn touch() {
    let blocks = slot_blocks();
    let mut i = 0usize;
    while i < SLOT_N {
        *blocks.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
