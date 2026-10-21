# Upstream dependencies

Revisions are copied from `project/config/sources.toml`; that file remains the
authoritative pin. The table records where each upstream tree is staged for
local builds.

| Source key | Upstream | Revision | Local staging |
|---|---|---|---|
| `esp-idf` | `espressif/esp-idf` | `b774170ff46c393eeb5e495ea37936038d3f4f4f` (`v5.5.5`) | `project/.local/sdk/esp-idf/` |
| `freertos-kernel` | `FreeRTOS/FreeRTOS-Kernel` | `8be86d4a24fd4091f8f4192018423ab590f408db` | `project/.local/source/FreeRTOS-Kernel/` |
| `stm32cubef4` | `STMicroelectronics/STM32CubeF4` | `89e6d4466578bc9eab83de8fcd1e397ceb5e5cc9` | `project/.local/source/STM32CubeF4/` |
| `zephyr` | `zephyrproject-rtos/zephyr` | `dccb09599635bdff17633fa7e9dab014b91dce90` (`v4.4.2`) | `project/.local/west/zephyr/` |
| `nuttx` | `apache/nuttx` | `20752312eaac24994487891207c81fa22c02f7b5` | `project/.local/source/nuttx/` |
| `nuttx-apps` | `apache/nuttx-apps` | `2f76986584ddd3881335e5897bbd80e12ae431a2` | `project/.local/source/nuttx-apps/` |
| `rivet-rtos` | `habeelali/rivet-rtos` | `b2d2cbff1dc891675e4bf98db4c728ad8e3e8a91` | `project/.local/source/rivet-rtos/` |
| `esp-idf-template` | `esp-rs/esp-idf-template` | not pinned | not staged |

`config/sources.toml` is marked `partially-pinned`: empty revisions deliberately
block acquisition until a reproducible setup resolves them. Do not claim a
build is reproducible while an upstream revision is empty.
