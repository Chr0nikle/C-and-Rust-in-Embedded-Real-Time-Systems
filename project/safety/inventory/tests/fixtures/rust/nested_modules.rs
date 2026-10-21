static mut TOP_COUNT: u32 = 0;

mod active_mod {
    pub static mut NESTED_COUNT: u32 = 0;

    pub fn nested_read() -> u32 {
        unsafe { NESTED_COUNT }
    }

    pub unsafe fn nested_unsafe(p: *mut u8) -> u8 {
        *p
    }

    pub fn nested_block() {
        unsafe { let _ = NESTED_COUNT; }
    }

    pub fn nested_pointer(p: *mut u8) -> u8 {
        unsafe { *p }
    }

    pub mod inner {
        pub fn check() -> u32 {
            1
        }

        pub fn nested_inner_read() -> u32 {
            unsafe { super::NESTED_COUNT }
        }
    }
}

mod other {
    pub fn check() -> u32 {
        2
    }
}

#[cfg(target_arch = "riscv32")]
mod inactive_parent {
    pub static mut HIDDEN: u32 = 0;

    pub fn child_without_cfg() -> u32 {
        1
    }

    pub fn inactive_read() -> u32 {
        unsafe { HIDDEN }
    }

    pub fn inactive_macro() {
        let _ = core::ptr::addr_of!(HIDDEN);
        let _ = unreachable!();
    }

    pub mod inner {
        pub fn deeper() -> u32 {
            2
        }
    }
}

#[cfg(target_arch = "arm")]
mod arm_active {
    pub fn active_child() -> u32 {
        1
    }

    #[cfg(target_arch = "riscv32")]
    pub fn cfg_off_child() -> u32 {
        2
    }
}

mod declared_only;
