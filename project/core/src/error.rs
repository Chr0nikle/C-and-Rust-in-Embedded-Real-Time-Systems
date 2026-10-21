//! Errors shared by adapters and host services.
use std::fmt;

/// Explicit failures; unimplemented operations must never masquerade as success.
#[derive(Debug)]
pub enum Error {
    /// A generation/implementation task still needs to be completed.
    NotImplemented(&'static str),
    /// A platform cannot support the requested configuration.
    Unsupported(&'static str),
    /// A user request or registration is invalid.
    Invalid(String),
    /// An OS-level I/O operation failed.
    Io(String),
    /// A deadline was exceeded while waiting for a device or process.
    Timeout(String),
    /// Operation interrupted; partial evidence is retained.
    Interrupted(String),
    /// A protocol frame was malformed or violated the session contract.
    Protocol(String),
    /// A firmware build invocation failed.
    Build(String),
    /// A firmware flash invocation failed.
    Flash(String),
    /// A debug probe operation (halt, inspect, breakpoint) failed.
    Debug(String),
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotImplemented(task) => write!(f, "not implemented: {task}"),
            Self::Unsupported(reason) => write!(f, "unsupported: {reason}"),
            Self::Invalid(reason) => f.write_str(reason),
            Self::Io(reason) => write!(f, "io: {reason}"),
            Self::Timeout(reason) => write!(f, "timeout: {reason}"),
            Self::Interrupted(reason) => write!(f, "interrupted: {reason}"),
            Self::Protocol(reason) => write!(f, "protocol: {reason}"),
            Self::Build(reason) => write!(f, "build: {reason}"),
            Self::Flash(reason) => write!(f, "flash: {reason}"),
            Self::Debug(reason) => write!(f, "debug: {reason}"),
        }
    }
}
impl std::error::Error for Error {}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e.to_string())
    }
}
/// Host operation result.
pub type Result<T> = std::result::Result<T, Error>;
