use core::ffi::c_void;

extern "C" {
    fn blocks() -> *mut *mut c_void;
}

unsafe fn touch() {
    let b = blocks();
    let q = b;
    let r = q;
    *b.add(0) = core::ptr::null_mut();
    let mut i = 0usize;
    while i < 8 {
        *r.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
