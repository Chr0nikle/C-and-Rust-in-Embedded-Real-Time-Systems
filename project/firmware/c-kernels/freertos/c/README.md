# FreeRTOS on ESP32-C6 with ESP-IDF

This directory contains the FreeRTOS benchmark application for ESP32-C6 using ESP-IDF integration.

## Prerequisites

1. ESP-IDF v5.5.5 (pinned in config/sources.toml)
2. CachyOS (Arch Linux) with ESP-IDF toolchain
3. ESP32-C6 development board

## Build Process

```bash
# Activate the ESP-IDF environment first
source <idf-root>/export.sh

cd firmware/c-kernels/freertos/c
idf.py set-target esp32c6   # regenerates sdkconfig from sdkconfig.defaults
idf.py build
```

### 3. Flash to device

```bash
idf.py -p /dev/ttyUSB0 flash
```

Replace `/dev/ttyUSB0` with your actual USB port.

## Benchmark Implementation

- `main/main.c` — `app_main()`, FreeRTOS port (native queue/tick primitives),
  minimal sink; shared runner in `common/c/src/runner.c`
- Timer overhead measurement
- Queue round-trip measurement

## Notes

- The ESP-IDF FreeRTOS scheduler is already running when `app_main()` is called
- UART protocol output is currently a no-op stub (`bench_uart_send`); results
  are only available via the debug console
- The benchmark suite runs automatically on startup
