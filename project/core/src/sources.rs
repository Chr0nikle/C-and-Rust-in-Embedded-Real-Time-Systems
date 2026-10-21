//! TODO: consume config/sources.toml; fetch immutable revisions into a user cache.
//! Verify repository URL, full commit SHA, submodule SHAs and SDK/tool checksums.
//! Resolve tags during explicit setup, record the resolution, then use detached checkouts.
//! Refuse blank pins; offline mode must explain missing objects. Never update SDKs during flash.
use crate::error::{Error, Result};
/// An exact source identity; repository and commit are not executable shell fragments.
pub struct SourcePin {
    /// Upstream URL from the manifest.
    pub repository: String,
    /// Full immutable commit SHA; blank/TODO is invalid.
    pub commit: String,
}
/// Fetching is intentionally unavailable until pin verification and cache locking exist.
pub fn fetch(_pin: &SourcePin) -> Result<()> {
    Err(Error::NotImplemented(
        "pinned source acquisition and offline cache",
    ))
}
