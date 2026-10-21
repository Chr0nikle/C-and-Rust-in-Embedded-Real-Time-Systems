//! Static memory footprint from a built ELF.
//!
//! RAM/flash accounting uses ELF section flags (`SHF_ALLOC`) plus the section
//! kind, so it does not depend on linker-script section names. `NOBITS`
//! (uninitialized) sections count toward RAM only; everything else allocated
//! counts toward flash and, when writable, RAM too.

use crate::error::{Error, Result};
use object::{Object, ObjectSection, SectionFlags, SectionKind};
use std::path::Path;

/// Duplicated `SHF_ALLOC` from the ELF spec (object exposes the raw flags).
const SHF_ALLOC: u64 = 0x2;

/// One section's contribution.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SectionSize {
    /// Section name.
    pub name: String,
    /// Size in bytes.
    pub size: u64,
    /// `text`, `rodata`, `data`, `bss` or `other`.
    pub kind: String,
    /// Whether the section occupies memory (SHF_ALLOC).
    pub allocated: bool,
}

/// Aggregated footprint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryReport {
    /// Per-section detail (allocated, non-empty sections only).
    pub sections: Vec<SectionSize>,
    /// Bytes occupied in flash (allocated, initialized).
    pub flash_bytes: u64,
    /// Bytes occupied in RAM (allocated writable + uninitialized).
    pub ram_bytes: u64,
}

/// Classify an object section kind into the reported vocabulary.
pub fn kind_name(kind: SectionKind) -> &'static str {
    match kind {
        SectionKind::Text => "text",
        SectionKind::ReadOnlyData | SectionKind::ReadOnlyString => "rodata",
        SectionKind::Data => "data",
        SectionKind::UninitializedData => "bss",
        _ => "other",
    }
}

fn is_allocated(flags: SectionFlags) -> bool {
    match flags {
        SectionFlags::Elf { sh_flags } => sh_flags & SHF_ALLOC != 0,
        // Non-ELF formats: treat known data/text kinds as allocated.
        _ => true,
    }
}

/// Aggregate section sizes into flash/RAM totals.
pub fn summarize(sections: &[SectionSize]) -> MemoryReport {
    let mut flash = 0u64;
    let mut ram = 0u64;
    for section in sections {
        if !section.allocated {
            continue;
        }
        match section.kind.as_str() {
            "bss" => ram += section.size,
            "data" => {
                ram += section.size;
                flash += section.size;
            }
            "text" | "rodata" => flash += section.size,
            // Unknown allocated sections are conservatively flash.
            _ => flash += section.size,
        }
    }
    MemoryReport {
        sections: sections.to_vec(),
        flash_bytes: flash,
        ram_bytes: ram,
    }
}

/// Parse an ELF file and report allocated section sizes.
pub fn analyze_elf(path: &Path) -> Result<MemoryReport> {
    let data =
        std::fs::read(path).map_err(|e| Error::Invalid(format!("read {}: {e}", path.display())))?;
    let file = object::File::parse(&*data)
        .map_err(|e| Error::Invalid(format!("parse {}: {e}", path.display())))?;

    let mut sections = Vec::new();
    for section in file.sections() {
        let size = section.size();
        if size == 0 {
            continue;
        }
        let allocated = is_allocated(section.flags());
        if !allocated {
            continue;
        }
        sections.push(SectionSize {
            name: section.name().unwrap_or("<unnamed>").to_string(),
            size,
            kind: kind_name(section.kind()).to_string(),
            allocated,
        });
    }
    Ok(summarize(&sections))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn section(name: &str, size: u64, kind: &str) -> SectionSize {
        SectionSize {
            name: name.into(),
            size,
            kind: kind.into(),
            allocated: true,
        }
    }

    #[test]
    fn flash_and_ram_accounting() {
        let report = summarize(&[
            section(".text", 1000, "text"),
            section(".rodata", 100, "rodata"),
            section(".data", 50, "data"),
            section(".bss", 200, "bss"),
            SectionSize {
                name: ".debug_info".into(),
                size: 5000,
                kind: "other".into(),
                allocated: false,
            },
        ]);
        // flash = text + rodata + data; ram = data + bss
        assert_eq!(report.flash_bytes, 1150);
        assert_eq!(report.ram_bytes, 250);
        assert_eq!(report.sections.len(), 5);
    }

    #[test]
    fn unknown_allocated_counts_as_flash() {
        let report = summarize(&[section(".ARM.exidx", 8, "other")]);
        assert_eq!(report.flash_bytes, 8);
        assert_eq!(report.ram_bytes, 0);
    }
}
