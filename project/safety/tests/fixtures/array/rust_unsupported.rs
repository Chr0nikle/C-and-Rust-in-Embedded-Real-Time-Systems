use core::ffi::c_void;

extern "C" {
    fn slot_blocks() -> *mut *mut c_void;
}

unsafe fn touch(n: usize) {
    let blocks = slot_blocks();
    let mut i = 0usize;
    while i < n {
        *blocks.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
