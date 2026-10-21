use core::ffi::c_void;

extern "C" {
    fn blocks() -> *mut *mut c_void;
}

unsafe fn touch(n: usize) {
    let b = blocks();
    let q = b;
    let mut i = 0usize;
    while i < n {
        *q.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
