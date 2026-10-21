//! Static image footprint from ELF section headers, without external tools.
//!
//! The C-versus-Rust comparison needs code and data size, not only runtime
//! memory. Parsing the section table directly keeps this host-side and testable,
//! and works for both the 32-bit ARM and 64-bit host ELF layouts. Only
//! `SHF_ALLOC` sections are counted; debug and symbol tables are ignored.
use crate::error::{Error, Result};
use std::path::Path;

/// Size breakdown of one executable image, in bytes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Footprint {
    /// Executable code (alloc, executable).
    pub text: u64,
    /// Read-only data (alloc, not writable, not executable).
    pub rodata: u64,
    /// Initialized writable data (alloc, writable, has file contents).
    pub data: u64,
    /// Zero-initialized writable data (alloc, writable, `SHT_NOBITS`).
    pub bss: u64,
}

impl Footprint {
    /// Bytes stored in flash: executable code plus any initialized data.
    pub fn flash(&self) -> u64 {
        self.text + self.rodata + self.data
    }

    /// Bytes occupied in RAM at run time: initialized plus zero-initialized data.
    pub fn ram(&self) -> u64 {
        self.data + self.bss
    }
}

const EI_CLASS: usize = 4;
const EI_DATA: usize = 5;
const ELFCLASS32: u8 = 1;
const ELFCLASS64: u8 = 2;
const ELFDATA2LSB: u8 = 1;
const ELFDATA2MSB: u8 = 2;
const SHT_NOBITS: u32 = 8;
const SHF_WRITE: u64 = 0x1;
const SHF_ALLOC: u64 = 0x2;
const SHF_EXECINSTR: u64 = 0x4;

fn read_u16(bytes: &[u8], offset: usize, little: bool) -> Result<u16> {
    let raw = bytes
        .get(offset..offset + 2)
        .ok_or_else(|| Error::Invalid("truncated ELF header".into()))?;
    let raw: [u8; 2] = raw.try_into().unwrap();
    Ok(if little {
        u16::from_le_bytes(raw)
    } else {
        u16::from_be_bytes(raw)
    })
}

fn read_u32(bytes: &[u8], offset: usize, little: bool) -> Result<u32> {
    let raw = bytes
        .get(offset..offset + 4)
        .ok_or_else(|| Error::Invalid("truncated ELF header".into()))?;
    let raw: [u8; 4] = raw.try_into().unwrap();
    Ok(if little {
        u32::from_le_bytes(raw)
    } else {
        u32::from_be_bytes(raw)
    })
}

fn read_u64(bytes: &[u8], offset: usize, little: bool) -> Result<u64> {
    let raw = bytes
        .get(offset..offset + 8)
        .ok_or_else(|| Error::Invalid("truncated ELF header".into()))?;
    let raw: [u8; 8] = raw.try_into().unwrap();
    Ok(if little {
        u64::from_le_bytes(raw)
    } else {
        u64::from_be_bytes(raw)
    })
}

/// Sum `SHF_ALLOC` sections of an ELF image into text/rodata/data/bss.
pub fn section_sizes(path: &Path) -> Result<Footprint> {
    let bytes = std::fs::read(path)
        .map_err(|e| Error::Invalid(format!("read ELF {}: {e}", path.display())))?;
    parse(&bytes).map_err(|e| Error::Invalid(format!("parse ELF {}: {e}", path.display())))
}

/// Parse one ELF image's allocatable sections.
pub fn parse(bytes: &[u8]) -> Result<Footprint> {
    if bytes.len() < 64 || &bytes[..4] != b"\x7fELF" {
        return Err(Error::Invalid("not an ELF image".into()));
    }
    let class = bytes[EI_CLASS];
    let little = match bytes[EI_DATA] {
        ELFDATA2LSB => true,
        ELFDATA2MSB => false,
        other => return Err(Error::Invalid(format!("unknown ELF data encoding {other}"))),
    };
    let is64 = match class {
        ELFCLASS32 => false,
        ELFCLASS64 => true,
        other => return Err(Error::Invalid(format!("unknown ELF class {other}"))),
    };

    let shoff = if is64 {
        read_u64(bytes, 0x28, little)? as usize
    } else {
        read_u32(bytes, 0x20, little)? as usize
    };
    let shentsize = read_u16(bytes, if is64 { 0x3a } else { 0x2e }, little)? as usize;
    let shnum = read_u16(bytes, if is64 { 0x3c } else { 0x30 }, little)? as usize;
    if shnum == 0 || shentsize == 0 {
        return Err(Error::Invalid("ELF has no section table".into()));
    }

    let mut footprint = Footprint::default();
    for index in 0..shnum {
        let base = shoff
            .checked_add(index * shentsize)
            .ok_or_else(|| Error::Invalid("ELF section table overflow".into()))?;
        let (sh_type, flags, size) = if is64 {
            (
                read_u32(bytes, base + 4, little)?,
                read_u64(bytes, base + 8, little)?,
                read_u64(bytes, base + 0x20, little)?,
            )
        } else {
            (
                read_u32(bytes, base + 4, little)?,
                read_u32(bytes, base + 8, little)? as u64,
                read_u32(bytes, base + 0x14, little)? as u64,
            )
        };
        if flags & SHF_ALLOC == 0 {
            continue;
        }
        if flags & SHF_WRITE != 0 {
            if sh_type == SHT_NOBITS {
                footprint.bss += size;
            } else {
                footprint.data += size;
            }
        } else if flags & SHF_EXECINSTR != 0 {
            footprint.text += size;
        } else {
            footprint.rodata += size;
        }
    }
    Ok(footprint)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Minimal 64-bit little-endian ELF with four allocatable sections.
    fn synthetic_elf64() -> Vec<u8> {
        let mut bytes = vec![0u8; 64 + 4 * 64];
        bytes[..4].copy_from_slice(b"\x7fELF");
        bytes[EI_CLASS] = ELFCLASS64;
        bytes[EI_DATA] = ELFDATA2LSB;
        bytes[0x28..0x30].copy_from_slice(&64u64.to_le_bytes());
        bytes[0x3a..0x3c].copy_from_slice(&64u16.to_le_bytes());
        bytes[0x3c..0x3e].copy_from_slice(&4u16.to_le_bytes());

        let sections: [(u32, u64, u64); 4] = [
            (1, SHF_ALLOC | SHF_EXECINSTR, 0x1000),
            (1, SHF_ALLOC, 0x200),
            (1, SHF_ALLOC | SHF_WRITE, 0x80),
            (8, SHF_ALLOC | SHF_WRITE, 0x400),
        ];
        for (index, (sh_type, flags, size)) in sections.iter().enumerate() {
            let base = 64 + index * 64;
            bytes[base + 4..base + 8].copy_from_slice(&sh_type.to_le_bytes());
            bytes[base + 8..base + 16].copy_from_slice(&flags.to_le_bytes());
            bytes[base + 0x20..base + 0x28].copy_from_slice(&size.to_le_bytes());
        }
        bytes
    }

    #[test]
    fn classifies_alloc_sections_and_ignores_non_alloc() {
        let footprint = parse(&synthetic_elf64()).unwrap();
        assert_eq!(footprint.text, 0x1000);
        assert_eq!(footprint.rodata, 0x200);
        assert_eq!(footprint.data, 0x80);
        assert_eq!(footprint.bss, 0x400);
        assert_eq!(footprint.flash(), 0x1280);
        assert_eq!(footprint.ram(), 0x480);
    }

    #[test]
    fn rejects_non_elf_input() {
        assert!(parse(b"not an elf at all, definitely not").is_err());
    }
}
