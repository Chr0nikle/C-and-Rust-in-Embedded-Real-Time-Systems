use core::ffi::c_void;

extern "C" {
    fn xQueueGenericCreate(length: u32, item_size: u32, queue_type: u8) -> *mut c_void;
    fn xQueueGenericSend(
        queue: *mut c_void,
        item: *const c_void,
        wait: u32,
        position: u32,
    ) -> i32;
}

static mut S_Q: *mut c_void = core::ptr::null_mut();

unsafe fn make() {
    S_Q = xQueueGenericCreate(1, 8, 0);
}

unsafe fn send(value: u32) {
    xQueueGenericSend(S_Q, &value as *const u32 as *const c_void, 0, 0);
}
