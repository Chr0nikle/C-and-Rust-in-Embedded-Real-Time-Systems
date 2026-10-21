use core::ffi::c_void;

extern "C" {
    fn slot_at(i: u32) -> *mut c_void;
}

unsafe fn touch() {
    let p = slot_at(0);
    let mut i = 0usize;
    while i < 4 {
        *p.add(i) = core::ptr::null_mut();
        i += 1;
    }
}
