extern "C" {
    #[cfg(target_arch = "riscv32")]
    pub fn abi_inactive(value: u32) -> i32;
}
