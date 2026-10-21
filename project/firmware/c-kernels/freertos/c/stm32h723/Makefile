# STM32H723ZG smoke firmware build for the stm32h723 bench.
#
# Plain Makefile + st-flash, per the plan. Cross-compiles with the local
# arm-none-eabi toolchain, links the FreeRTOS-Kernel CM4F port + heap_4, the
# STM32F4xx HAL subset, and the two application sources. Emits .elf/.hex/.bin
# and a size report. The `flash` target writes the .bin over ST-Link and resets.

CROSS   ?= arm-none-eabi-
CC       = $(CROSS)gcc
AS       = $(CROSS)gcc
CP       = $(CROSS)objcopy
SZ       = $(CROSS)size
STFLASH  ?= st-flash

TARGET   = freertos-stm32h723-bench
BUILD    = build
# The build-id stamp rule precedes `all`; keep the firmware image the default.
.DEFAULT_GOAL := all

# Paths relative to this directory.
ROOT     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# Build modes:
#   make           -O2 release build (default)
#   make DEBUG=1   -Og -g3 debug build with full symbol info
#
# Project-local toolchain override: prepend the project's ARM toolchain to PATH
# when it is not already available.
PROJECT_ROOT := $(abspath $(ROOT)/../../../../../)
TOOLCHAIN ?= $(PROJECT_ROOT)/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin
ifneq ($(wildcard $(TOOLCHAIN)/$(CROSS)gcc),)
  export PATH := $(TOOLCHAIN):$(PATH)
endif
CUBE     ?= $(ROOT)/../../../../../.local/source/STM32CubeH7
KERNEL   ?= $(ROOT)/../../../../../.local/source/FreeRTOS-Kernel
DRIVER   = $(CUBE)/Drivers
CMSIS    = $(DRIVER)/CMSIS
DEVICE   = $(CMSIS)/Device/ST/STM32H7xx
HAL      = $(DRIVER)/STM32H7xx_HAL_Driver

# Application sources (this directory).
APP_SRCS   = src/main.c src/stm32h7xx_it.c src/bench.c
APP_OBJS   = $(BUILD)/main.o $(BUILD)/stm32h7xx_it.o $(BUILD)/bench.o

# Shared bench runner (common/c) + its header.
COMMON     = $(ROOT)/../../../../../firmware/common/c
RUNNER_SRC = $(COMMON)/src/runner.c

# FreeRTOS-Kernel sources (CM4F port + heap_4 + core).
RTOS_SRCS  = $(KERNEL)/portable/GCC/ARM_CM7/r0p1/port.c \
             $(KERNEL)/portable/MemMang/heap_4.c \
             $(KERNEL)/tasks.c \
             $(KERNEL)/queue.c \
             $(KERNEL)/list.c \
             $(KERNEL)/event_groups.c \
             $(KERNEL)/timers.c
RTOS_OBJS  = $(BUILD)/port.o $(BUILD)/heap_4.o $(BUILD)/tasks.o \
             $(BUILD)/queue.o $(BUILD)/list.o $(BUILD)/event_groups.o \
             $(BUILD)/timers.o

# HAL sources (subset used by the smoke firmware).
HAL_SRCS   = $(HAL)/Src/stm32h7xx_hal.c \
             $(HAL)/Src/stm32h7xx_hal_gpio.c \
             $(HAL)/Src/stm32h7xx_hal_uart.c \
             $(HAL)/Src/stm32h7xx_hal_rcc.c \
             $(HAL)/Src/stm32h7xx_hal_rcc_ex.c \
             $(HAL)/Src/stm32h7xx_hal_cortex.c \
             $(HAL)/Src/stm32h7xx_hal_pwr.c \
             $(HAL)/Src/stm32h7xx_hal_pwr_ex.c \
             $(HAL)/Src/stm32h7xx_hal_dma.c \
             $(HAL)/Src/stm32h7xx_hal_flash.c
HAL_OBJS   = $(BUILD)/hal.o $(BUILD)/hal_gpio.o $(BUILD)/hal_uart.o \
             $(BUILD)/hal_rcc.o $(BUILD)/hal_rcc_ex.o $(BUILD)/hal_cortex.o \
             $(BUILD)/hal_pwr.o $(BUILD)/hal_pwr_ex.o $(BUILD)/hal_dma.o \
             $(BUILD)/hal_flash.o

# System + startup.
SYS_SRCS   = $(DEVICE)/Source/Templates/system_stm32h7xx.c \
             $(DEVICE)/Source/Templates/gcc/startup_stm32h723xx.s
SYS_OBJS   = $(BUILD)/system.o $(BUILD)/startup.o

ALL_SRCS   = $(APP_SRCS) $(RTOS_SRCS) $(HAL_SRCS) $(SYS_SRCS) $(RUNNER_SRC)
ALL_OBJS   = $(APP_OBJS) $(RTOS_OBJS) $(HAL_OBJS) $(SYS_OBJS) $(BUILD)/runner.o

# Build-id injected into the firmware (protocol v1 metadata). Default is the
# no-git fallback; the Rust build path can override with BENCH_BUILD_ID.
BENCH_BUILD_ID ?= freertos-stm32h723-bench-nogit

# The injected build-id is part of CPPFLAGS, but object files depend only on
# their sources, so an incremental build would silently keep a stale id. Stamp
# the current id and make the firmware metadata object depend on it.
BUILD_ID_STAMP = $(BUILD)/.build_id

# FORCE makes the recipe re-run every build; `cmp` preserves the mtime (and so
# avoids a rebuild) unless the id actually changed.
.PHONY: FORCE
FORCE:

$(BUILD_ID_STAMP): FORCE | $(BUILD)
	@echo '$(BENCH_BUILD_ID)' | cmp -s - $@ 2>/dev/null || echo '$(BENCH_BUILD_ID)' > $@

# Optional Rust application core (same workloads, native FreeRTOS primitives).
# `make RUST=1` builds and links the staticlib; the C session drives it per case.
RUST_CRATE   = $(ROOT)/../../../../../firmware/c-kernels/freertos/rust
RUST_TARGET ?= thumbv7em-none-eabihf
RUST_LIB     = $(ROOT)/../../../../../firmware/target/$(RUST_TARGET)/release/librtos_firmware_freertos.a
ifeq ($(RUST),1)
RUST_LINK = $(RUST_LIB)
endif

ifeq ($(DEBUG),1)
  OPTFLAGS = -Og -g3
else
  OPTFLAGS = -O2
endif

CFLAGS   = -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard \
            $(OPTFLAGS) -fdata-sections -ffunction-sections -Wall -Wextra -Wno-unused-parameter \
            -ffunction-sections -fdata-sections
CPPFLAGS = -DSTM32H723xx -DUSE_HAL_DRIVER \
           '-DBENCH_BUILD_ID="$(BENCH_BUILD_ID)"'
ifeq ($(RUST),1)
CPPFLAGS += -DBENCH_LANGUAGE_RUST
endif
# Deliberate-inversion control for the priority_scheduling_correctness case:
# set PRIO_INVERT=1 in the environment (or on the make command line) to build
# the inverse priority mapping, which must make case 26 report 0.
PRIO_INVERT ?= 0
CPPFLAGS += -DPRIO_INVERT=$(PRIO_INVERT)
# Positive control for max_interrupt_disabled: MAXDIS_CONTROL=1 masks interrupts
# for a calibrated ~80 us around the probe so the reported delay rises to ~30 us.
MAXDIS_CONTROL ?= 0
CPPFLAGS += -DMAXDIS_CONTROL=$(MAXDIS_CONTROL)
# Positive control for nested_irq_overhead: NESTED_CONTROL=1 disables the higher
# IRQ so both baseline and nested pend only the low line, and the reported delta
# must fall to ~0.
NESTED_CONTROL ?= 0
CPPFLAGS += -DNESTED_CONTROL=$(NESTED_CONTROL)
INCLUDES = -Iinclude \
            -Isrc \
            -I$(COMMON)/include \
            -I$(KERNEL)/include \
            -I$(KERNEL)/portable/GCC/ARM_CM7/r0p1 \
            -I$(CMSIS)/Include \
            -I$(DEVICE)/Include \
            -I$(HAL)/Inc

LDFLAGS  = -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard \
           --specs=nano.specs --specs=nosys.specs \
           -T $(ROOT)/stm32h723zgtx_flash.ld \
           -Wl,--gc-sections \
           -Wl,-Map=$(BUILD)/$(TARGET).map

DEPS     = $(ALL_OBJS:.o=.d)

.PHONY: all size flash clean

all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).hex $(BUILD)/$(TARGET).bin
	$(SZ) $(BUILD)/$(TARGET).elf

size: $(BUILD)/$(TARGET).elf
	$(SZ) $(BUILD)/$(TARGET).elf

# Application + system objects. BENCH_BUILD_ID is embedded by main.c and
# bench.c, so every app object depends on the current-id stamp.
$(BUILD)/%.o: src/%.c $(BUILD_ID_STAMP) | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/%.o: src/%.s | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

# FreeRTOS-Kernel objects (built from the kernel tree into our build dir).
$(BUILD)/port.o: $(KERNEL)/portable/GCC/ARM_CM7/r0p1/port.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/heap_4.o: $(KERNEL)/portable/MemMang/heap_4.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/tasks.o:         $(KERNEL)/tasks.c         | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/queue.o:         $(KERNEL)/queue.c         | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/list.o:          $(KERNEL)/list.c          | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/event_groups.o:  $(KERNEL)/event_groups.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/timers.o:        $(KERNEL)/timers.c        | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

# FreeRTOS configuration changes (e.g. INCLUDE_*) must rebuild every RTOS/app
# object that includes FreeRTOS.h.
$(APP_OBJS) $(RTOS_OBJS): include/FreeRTOSConfig.h

# HAL objects.
$(BUILD)/hal.o:       $(HAL)/Src/stm32h7xx_hal.c       | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_gpio.o:  $(HAL)/Src/stm32h7xx_hal_gpio.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_uart.o:  $(HAL)/Src/stm32h7xx_hal_uart.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_rcc.o:   $(HAL)/Src/stm32h7xx_hal_rcc.c   | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_rcc_ex.o:$(HAL)/Src/stm32h7xx_hal_rcc_ex.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_cortex.o:$(HAL)/Src/stm32h7xx_hal_cortex.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_pwr.o:   $(HAL)/Src/stm32h7xx_hal_pwr.c   | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_pwr_ex.o:$(HAL)/Src/stm32h7xx_hal_pwr_ex.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_dma.o:   $(HAL)/Src/stm32h7xx_hal_dma.c   | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/hal_flash.o: $(HAL)/Src/stm32h7xx_hal_flash.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

# Shared bench runner (common/c).
$(BUILD)/runner.o:  $(RUNNER_SRC) | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

# System + startup.
$(BUILD)/system.o:  $(DEVICE)/Source/Templates/system_stm32h7xx.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/startup.o: $(DEVICE)/Source/Templates/gcc/startup_stm32h723xx.s | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

$(RUST_LIB): $(wildcard $(RUST_CRATE)/src/*.rs) $(RUST_CRATE)/Cargo.toml $(ROOT)/../../../../../firmware/common/rust/src/lib.rs
	cd $(RUST_CRATE) && cargo build --release --target $(RUST_TARGET)

$(BUILD)/$(TARGET).elf: $(ALL_OBJS) $(RUST_LINK)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(CP) -O ihex $< $@

# The .bin must contain only the flash-loaded sections. A bare `-O binary`
# spans the full address range between the flash and RAM LOAD segments and
# produces a multi-hundred-MB file, so select the flash sections explicitly.
FLASH_SECTIONS = --only-section=.isr_vector --only-section=.text \
                  --only-section=.rodata --only-section=.data \
                  --only-section=.ARM --only-section=.ARM.extab \
                  --only-section=.init_array --only-section=.fini_array
$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(CP) -O binary $(FLASH_SECTIONS) $< $@

# Options must precede the subcommand on both st-flash 0.0.x and 1.x;
# 1.x has no `exit` subcommand, so use --reset to start running.
flash: $(BUILD)/$(TARGET).bin
	$(STFLASH) --reset write $< 0x8000000

clean:
	rm -rf $(BUILD)

-include $(DEPS)
