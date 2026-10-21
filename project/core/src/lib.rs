//! Host-side contracts. No SDK, firmware, or concrete plugin dependencies.
//! Hardware services deliberately fail until implemented; see rules.md.
pub mod boards;
pub mod config_manager;
pub mod debug;
pub mod device_manager;
pub mod elf;
pub mod error;
pub mod flash;
pub mod gdb;
pub mod logger;
pub mod logic_analyzer;
pub mod memory;
pub mod monitor;
pub mod plugin_interface;
pub mod process;
pub mod results;
pub mod safety;
pub mod scope;
pub mod sources;
pub mod tool_checker;
pub mod tooling;
pub mod uart;
pub mod workflow;
