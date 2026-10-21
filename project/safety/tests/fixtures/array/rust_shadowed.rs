use core::ffi::c_void;

extern "C" {
    fn slot_blocks() -> *mut *mut c_void;
}

const N: usize = 1024;

unsafe fn touch() {
    const N: usize = 2048;
    let b = slot_blocks();
    let mut i = 0usize;
    while i < N {
        *b.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
