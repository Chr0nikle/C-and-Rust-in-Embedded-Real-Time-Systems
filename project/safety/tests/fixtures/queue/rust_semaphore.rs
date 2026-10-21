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

unsafe fn semaphore_give(semaphore: *mut c_void) -> i32 {
    xQueueGenericSend(semaphore, core::ptr::null(), 0, 0)
}
