//! Keep an idle foreground session cancellable without a detached input thread.
use crate::{error::Result, tooling::Cancellation};
use std::io::{self, Read};

pub(super) struct Stdin {
    cancellation: Cancellation,
}
impl Stdin {
    pub fn new() -> Result<Self> {
        Ok(Self {
            cancellation: Cancellation::new()?,
        })
    }
}
impl Read for Stdin {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if buffer.is_empty() {
            return Ok(0);
        }
        loop {
            self.cancellation.check().map_err(io::Error::other)?;
            #[cfg(unix)]
            {
                let mut fd = libc::pollfd {
                    fd: libc::STDIN_FILENO,
                    events: libc::POLLIN,
                    revents: 0,
                };
                // SAFETY: fd points to one initialized pollfd for this call's lifetime.
                let ready = unsafe { libc::poll(&mut fd, 1, 50) };
                if ready == 0 {
                    continue;
                }
                if ready < 0 {
                    let error = io::Error::last_os_error();
                    if error.kind() == io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err(error);
                }
            }
            return io::stdin().read(buffer);
        }
    }
}
