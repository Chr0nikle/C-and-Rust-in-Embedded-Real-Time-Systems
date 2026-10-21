use core::ffi::c_void;

extern "C" {
    fn blocks() -> *mut *mut c_void;
}

unsafe fn touch() {
    let b = blocks();
    let mut i = 0usize;
    while 4 > i {
        *b.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
