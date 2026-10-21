# nRF52840 DK (PCA10056) FreeRTOS bench firmware.
#
# Cross-compiles with the local arm-none-eabi toolchain, links the vendored
# FreeRTOS-Kernel CM4F port + heap_4 and the shared C runner. No vendor HAL: the
# board layer is the startup vector table plus a polling UARTE0 console.
CROSS   ?= arm-none-eabi-
CC       = $(CROSS)gcc
AS       = $(CROSS)gcc
CP       = $(CROSS)objcopy
SZ       = $(CROSS)size

TARGET   = freertos-nrf52840-bench
BUILD    = build
.DEFAULT_GOAL := all

ROOT     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

PROJECT_ROOT := $(abspath $(ROOT)/../../../../../)
TOOLCHAIN ?= $(PROJECT_ROOT)/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin
ifneq ($(wildcard $(TOOLCHAIN)/$(CROSS)gcc),)
  export PATH := $(TOOLCHAIN):$(PATH)
endif
KERNEL   ?= $(PROJECT_ROOT)/.local/source/FreeRTOS-Kernel
# Vendored nRF5 SDK ships CMSIS core headers (core_cm4.h) only.
NRF_SDK  ?= $(PROJECT_ROOT)/.local/west/modules/lib/lora-basics-modem/lbm_applications/2_porting_nrf_52840/mcu_drivers/nRF5_SDK_17.1.0_ddde560_Driver
CMSIS_INC = $(NRF_SDK)/components/toolchain/cmsis/include

APP_SRCS   = src/main.c src/bench.c src/nrf_uart.c src/startup_nrf52840.c
APP_OBJS   = $(BUILD)/main.o $(BUILD)/bench.o $(BUILD)/nrf_uart.o $(BUILD)/startup_nrf52840.o

COMMON     = $(PROJECT_ROOT)/firmware/common/c
RUNNER_SRC = $(COMMON)/src/runner.c

RTOS_SRCS  = $(KERNEL)/portable/GCC/ARM_CM4F/port.c \
             $(KERNEL)/portable/MemMang/heap_4.c \
             $(KERNEL)/tasks.c \
             $(KERNEL)/queue.c \
             $(KERNEL)/list.c \
             $(KERNEL)/event_groups.c \
             $(KERNEL)/timers.c
RTOS_OBJS  = $(BUILD)/port.o $(BUILD)/heap_4.o $(BUILD)/tasks.o \
             $(BUILD)/queue.o $(BUILD)/list.o $(BUILD)/event_groups.o \
             $(BUILD)/timers.o

ALL_SRCS   = $(APP_SRCS) $(RTOS_SRCS) $(RUNNER_SRC)
ALL_OBJS   = $(APP_OBJS) $(RTOS_OBJS) $(BUILD)/runner.o

BENCH_BUILD_ID ?= freertos-nrf52840-bench-nogit
BUILD_ID_STAMP = $(BUILD)/.build_id

.PHONY: FORCE
FORCE:

$(BUILD_ID_STAMP): FORCE | $(BUILD)
	@echo '$(BENCH_BUILD_ID)' | cmp -s - $@ 2>/dev/null || echo '$(BENCH_BUILD_ID)' > $@

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

CFLAGS   = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
            $(OPTFLAGS) -fdata-sections -ffunction-sections -Wall -Wextra \
            -Wno-unused-parameter
CPPFLAGS = -DNRF52840_XXAA -D__NVIC_PRIO_BITS=3 -D__FPU_PRESENT=1 \
            '-DBENCH_BUILD_ID="$(BENCH_BUILD_ID)"'
# Positive control for max_interrupt_disabled: MAXDIS_CONTROL=1 masks interrupts
# (at the benchmark task's priority) for a calibrated ~80 us before the probe
# timer fires, which must raise the reported delay. Set on the make command line
# or in the environment; a changed CPPFLAGS alone needs a clean `rtos build`.
MAXDIS_CONTROL ?= 0
CPPFLAGS += -DMAXDIS_CONTROL=$(MAXDIS_CONTROL)
# Positive control for nested_irq_overhead: NESTED_CONTROL=1 disables the higher
# IRQ so the reported delta falls to ~0.
NESTED_CONTROL ?= 0
CPPFLAGS += -DNESTED_CONTROL=$(NESTED_CONTROL)
ifeq ($(RUST),1)
CPPFLAGS += -DBENCH_LANGUAGE_RUST
endif
INCLUDES = -Iinclude \
            -Isrc \
            -I$(COMMON)/include \
            -I$(KERNEL)/include \
            -I$(KERNEL)/portable/GCC/ARM_CM4F \
            -I$(CMSIS_INC)

LDFLAGS  = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           --specs=nano.specs --specs=nosys.specs \
           -T $(ROOT)/nrf52840_flash.ld \
           -Wl,--gc-sections \
           -Wl,-Map=$(BUILD)/$(TARGET).map

DEPS = $(ALL_OBJS:.o=.d)

.PHONY: all size clean

all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).hex $(BUILD)/$(TARGET).bin
	$(SZ) $(BUILD)/$(TARGET).elf

size: $(BUILD)/$(TARGET).elf
	$(SZ) $(BUILD)/$(TARGET).elf

$(BUILD)/%.o: src/%.c $(BUILD_ID_STAMP) | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/port.o: $(KERNEL)/portable/GCC/ARM_CM4F/port.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/heap_4.o: $(KERNEL)/portable/MemMang/heap_4.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/tasks.o:        $(KERNEL)/tasks.c        | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/queue.o:        $(KERNEL)/queue.c        | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/list.o:         $(KERNEL)/list.c         | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/event_groups.o: $(KERNEL)/event_groups.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/timers.o:       $(KERNEL)/timers.c       | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/runner.o: $(RUNNER_SRC) | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(APP_OBJS) $(RTOS_OBJS): include/FreeRTOSConfig.h

$(BUILD):
	mkdir -p $(BUILD)

$(RUST_LIB): $(wildcard $(RUST_CRATE)/src/*.rs) $(RUST_CRATE)/Cargo.toml $(ROOT)/../../../../../firmware/common/rust/src/lib.rs
	cd $(RUST_CRATE) && cargo build --release --target $(RUST_TARGET)

$(BUILD)/$(TARGET).elf: $(ALL_OBJS) $(RUST_LINK)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ -o $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(CP) -O ihex $< $@

FLASH_SECTIONS = --only-section=.isr_vector --only-section=.text \
                  --only-section=.rodata --only-section=.data \
                  --only-section=.init_array --only-section=.fini_array \
                  --only-section=.ARM.extab --only-section=.ARM
$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(CP) -O binary $(FLASH_SECTIONS) $< $@

clean:
	rm -rf $(BUILD)

-include $(DEPS)
