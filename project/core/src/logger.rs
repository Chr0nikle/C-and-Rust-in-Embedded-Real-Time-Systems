//! TODO: initialize host logging exactly once in CLI; honor verbosity and --json.
//! Host diagnostics go to stderr and logs/host.log; UART bytes use the UART service.
//! Redact secrets from environment diagnostics. Never mix raw UART and structured stdout.
