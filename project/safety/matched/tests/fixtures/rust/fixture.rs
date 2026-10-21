extern "C" {
    fn xQueueGenericSend(queue: *mut u8, item: *const u8, len: usize, flags: u32) -> i32;
    fn xQueueGenericSendFromISR(queue: *mut u8, item: *const u8, woken: *mut i32) -> i32;
    fn bridge_send(queue: *mut u8, item: *const u8, len: usize) -> i32;
    fn bridge_alloc(size: usize) -> *mut u8;
    fn missing_symbol() -> i32;
    fn ambiguous_symbol() -> i32;
    fn xQueueGenericCreate(queue_length: u32, item_size: u32, queue_type: u8) -> *mut u8;
    fn xTaskCreate(task: extern "C" fn(*mut u8), name: *const u8, stack_depth: u32, arg: *mut u8, priority: u32, created: *mut *mut u8) -> i32;
    fn xPortGetFreeHeapSize() -> u32;
}

struct Port;

impl Port {
    fn safe_send(&mut self, payload: &[u8]) -> i32 {
        if payload.len() == 0 {
            return -1;
        }
        unsafe {
            xQueueGenericSend(core::ptr::null_mut(), payload.as_ptr(), payload.len(), 0)
        }
    }

    unsafe fn unsafe_send(&mut self, payload: *mut u8, len: usize) -> i32 {
        xQueueGenericSend(payload, payload, len, 0)
    }

    fn direct_ffi(&mut self, payload: *mut u8) -> i32 {
        if payload.is_null() {
            return -1;
        }
        unsafe { xQueueGenericSend(payload, payload, 0, 0) }
    }

    fn bridge(&mut self) -> i32 {
        unsafe { bridge_send(core::ptr::null_mut(), core::ptr::null(), 0) }
    }

    fn bridge_alloc(&mut self, size: usize) -> *mut u8 {
        unsafe { bridge_alloc(size) }
    }

    fn missing(&mut self) -> i32 {
        unsafe { missing_symbol() }
    }

    fn ambiguous(&mut self) -> i32 {
        ambiguous()
    }

    fn macro_use(&mut self) {
        let _ = unknown_macro!();
    }

    fn queue_create(&mut self) -> *mut u8 {
        unsafe { xQueueGenericCreate(1, core::mem::size_of::<u32>() as u32, 0) }
    }

    fn loop_check(&mut self) {
        unsafe {
            while xPortGetFreeHeapSize() > 0 {
                break;
            }
        }
    }

    fn assigned_call(&mut self, queue: *mut u8, item: *const u8) {
        let _result = unsafe { xQueueGenericSend(queue, item, 0, 0) };
    }
}

fn ambiguous() -> i32 { 0 }

fn callback_entry(arg: *mut u8) {
    unsafe {
        let _ = xQueueGenericSend(core::ptr::null_mut(), core::ptr::null(), 0, 0);
    }
    let _ = arg;
}

fn create_with_callback() {
    unsafe {
        let _ = xTaskCreate(callback_entry, core::ptr::null(), 128, core::ptr::null_mut(), 1, core::ptr::null_mut());
    }
}

mod a {
    pub fn ambiguous() -> i32 { 1 }
}

mod b {
    pub fn ambiguous() -> i32 { 2 }
}

fn sample(port: &mut Port) {
    port.safe_send(&[1, 2, 3]);
}

#[cfg(target_arch = "riscv32")]
fn inactive(payload: *mut u8) -> i32 {
    unsafe { xQueueGenericSend(payload, payload, 0, 0) }
}
