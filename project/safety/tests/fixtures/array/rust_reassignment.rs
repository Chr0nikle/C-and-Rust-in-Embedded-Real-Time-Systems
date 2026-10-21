use core::ffi::c_void;

extern "C" {
    fn blocks() -> *mut *mut c_void;
}

unsafe fn touch() {
    let mut q = blocks();
    q = core::ptr::null_mut();
    let mut i = 0usize;
    while i < 2 {
        *q.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
