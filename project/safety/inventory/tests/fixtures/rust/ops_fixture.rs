use core::ffi::c_void;

extern "C" {
    fn pvPortMalloc(size: usize) -> *mut c_void;
    fn vPortFree(ptr: *mut c_void);
    fn xQueueGenericCreate(length: u32, item_size: u32, queue_type: u8) -> *mut c_void;
    fn xQueueGenericSend(queue: *mut c_void, item: *const c_void, ticks: u32) -> u32;
}

struct Pair {
    value: u32,
}

static mut GLOBAL: u32 = 0;
static mut PTR: *mut u8 = core::ptr::null_mut();
static mut S_COUNT: u32 = 0;
static mut S_PTR: *mut u32 = core::ptr::null_mut();
static mut S_ARR: [u32; 4] = [0; 4];
static mut S_STRUCT: Pair = Pair { value: 0 };

macro_rules! unknown_macro {
    ($value:expr) => {
        $value
    };
}

fn safe_reference(value: &u32) -> u32 {
    *value
}

unsafe fn raw_operations(p: *mut u8) -> u8 {
    let first = *p;
    let advanced = p.add(1);
    let converted = p as *const u8;
    let _ = converted;
    let _ = advanced;
    GLOBAL = 1;
    let _ = GLOBAL;
    *PTR = first;
    let _ = pvPortMalloc(8);
    vPortFree(p as *mut c_void);
    let _ = xQueueGenericCreate(1, 4, 0);
    first
}

fn unsafe_block(p: *mut u8) {
    unsafe {
        let _ = *p;
        GLOBAL += 1;
    }
}

#[cfg(target_arch = "riscv32")]
unsafe fn inactive_riscv(p: *mut u8) -> u8 {
    *p
}

fn macro_expression() {
    let _ = core::ptr::addr_of_mut!(GLOBAL);
}

fn compound_updates() {
    unsafe {
        S_COUNT = 1;
        let _x = S_COUNT;
        S_COUNT += 1;
        S_COUNT -= 1;
        S_COUNT |= 1;
        S_ARR[1] = 2;
        S_ARR[1] += 3;
        S_STRUCT.value = 4;
        S_STRUCT.value += 5;
        *S_PTR = 6;
        *S_PTR += 7;
    }
}

fn address_taking() {
    unsafe {
        let _a = &S_COUNT;
        let _b = &mut S_COUNT;
        let _c = core::ptr::addr_of!(S_COUNT);
        let _d = core::ptr::addr_of_mut!(S_COUNT);
        let _e = unknown_macro!(S_COUNT);
    }
}

fn project_ptr(p: *mut u8) -> *mut u8 {
    p
}

fn project_ref(value: &u8) -> &u8 {
    value
}

fn use_project_ptr(p: *mut u8) -> u8 {
    let r = project_ptr(p);
    unsafe { *r }
}

fn use_project_ref(value: &u8) -> u8 {
    let r = project_ref(value);
    *r
}

fn function_alias(p: *mut u8) -> u8 {
    let f = project_ptr;
    let r = f(p);
    unsafe { *r }
}

#[cfg(feature = "one")]
fn ambiguous_ptr() -> *mut u8 {
    core::ptr::null_mut()
}

#[cfg(feature = "two")]
fn ambiguous_ptr() -> *mut u8 {
    core::ptr::null_mut()
}

fn use_ambiguous_ptr() -> u8 {
    let r = ambiguous_ptr();
    unsafe { *r }
}

#[cfg(target_arch = "arm")]
fn conditional_ptr() -> *mut u8 {
    core::ptr::null_mut()
}

#[cfg(target_arch = "riscv32")]
fn conditional_ptr() -> *mut u8 {
    core::ptr::null_mut()
}

fn use_conditional_ptr() -> u8 {
    let r = conditional_ptr();
    unsafe { *r }
}

type PtrAlias = *mut u8;

fn alias_return() -> PtrAlias {
    core::ptr::null_mut()
}

fn use_alias_return() -> u8 {
    let r = alias_return();
    unsafe { *r }
}

fn safe_wrapper(value: u32) -> u32 {
    value + 1
}

fn port_dummy() {}

fn call_project_wrapper() {
    port_dummy();
}

fn unsafe_block_in_safe_fn() {
    unsafe {
        let _ = GLOBAL;
    }
}

unsafe fn unsafe_fn_without_block() -> u32 {
    GLOBAL
}

fn nested_unsafe_blocks() {
    unsafe {
        unsafe {
            let _ = GLOBAL;
        }
    }
}

unsafe fn unsafe_fn_with_block() {
    unsafe {
        let _ = GLOBAL;
    }
}

fn safe_operation_in_unsafe_block() {
    unsafe {
        let _ = core::ptr::addr_of!(S_COUNT);
    }
}

fn unsafe_ffi_call() {
    unsafe {
        let _ = xQueueGenericCreate(1, 4, 0);
    }
}

fn safe_calls_safe_wrapper() {
    let _ = core::ptr::addr_of!(S_COUNT);
    let _ = safe_wrapper(1);
}

#[cfg(target_arch = "riscv32")]
fn inactive_unsafe_block(p: *mut u8) -> u8 {
    unsafe { *p }
}
