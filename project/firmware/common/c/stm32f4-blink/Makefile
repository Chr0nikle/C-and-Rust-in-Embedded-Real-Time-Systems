# STM32F4-Discovery flash-verification blink (Templates_LL based, no RTOS).
#
# Cross-compiles the LL blink app with the local arm-none-eabi toolchain,
# links the CMSIS startup + system files and the LL driver subset, and emits
# .elf/.hex/.bin plus a size report. The `flash` target writes the .bin over
# ST-Link via st-flash and resets the board.
#
# Build modes:
#   make           -O2 release build (default)
#   make DEBUG=1   -Og -g3 debug build with full symbol info

CROSS   ?= arm-none-eabi-
CC       = $(CROSS)gcc
CP       = $(CROSS)objcopy
SZ       = $(CROSS)size
STFLASH  ?= st-flash

TARGET   = stm32f4-blink
BUILD    = build

ROOT     := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PROJECT_ROOT := $(abspath $(ROOT)/../../../..)
CUBE     ?= $(ROOT)/../../../../.local/source/STM32CubeF4

# Project-local toolchain override: set CROSS to the project's ARM toolchain
# when it is not on PATH. The default path below matches the project layout.
# (Must come after PROJECT_ROOT is defined.)
TOOLCHAIN ?= $(PROJECT_ROOT)/.local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi/bin
ifneq ($(wildcard $(TOOLCHAIN)/$(CROSS)gcc),)
  export PATH := $(TOOLCHAIN):$(PATH)
endif
DRIVER   = $(CUBE)/Drivers
CMSIS    = $(DRIVER)/CMSIS
DEVICE   = $(CMSIS)/Device/ST/STM32F4xx

# Application sources (this directory).
APP_SRCS   = main.c
APP_OBJS   = $(BUILD)/main.o

# LL driver subset (only the non-header-only sources the app actually calls).
# In this STM32CubeF4 revision the LL drivers live inside the HAL driver dir,
# and ll_system/ll_bus/ll_cortex are header-only (no .c to compile).
LLDIR    = $(DRIVER)/STM32F4xx_HAL_Driver
LL_SRCS    = $(LLDIR)/Src/stm32f4xx_ll_gpio.c \
             $(LLDIR)/Src/stm32f4xx_ll_rcc.c \
             $(LLDIR)/Src/stm32f4xx_ll_utils.c
LL_OBJS    = $(BUILD)/ll_gpio.o $(BUILD)/ll_rcc.o $(BUILD)/ll_utils.o

# System + startup (CMSIS device templates, same as the FreeRTOS build).
SYS_SRCS   = $(DEVICE)/Source/Templates/system_stm32f4xx.c \
             $(DEVICE)/Source/Templates/gcc/startup_stm32f407xx.s
SYS_OBJS   = $(BUILD)/system.o $(BUILD)/startup.o

# Linker script: reuse the one proven for this MCU in the FreeRTOS firmware
# (common/c/stm32f4-blink -> freertos/c/stm32f4, three levels up to firmware/).
LDSCRIPT   = $(ROOT)/../../../freertos/c/stm32f4/stm32f407vgtx_flash.ld

ALL_SRCS   = $(APP_SRCS) $(LL_SRCS) $(SYS_SRCS)
ALL_OBJS   = $(APP_OBJS) $(LL_OBJS) $(SYS_OBJS)

ifeq ($(DEBUG),1)
  OPTFLAGS = -Og -g3
else
  OPTFLAGS = -O2
endif

CFLAGS   = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           $(OPTFLAGS) -fdata-sections -ffunction-sections \
           -Wall -Wextra -Wno-unused-parameter
CPPFLAGS = -DUSE_FULL_LL_DRIVER -DSTM32F407xx -DHSE_VALUE=8000000U
INCLUDES = -I. \
           -I$(LLDIR)/Inc \
           -I$(CMSIS)/Include \
           -I$(DEVICE)/Include

LDFLAGS  = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           --specs=nano.specs --specs=nosys.specs \
           -T $(LDSCRIPT) \
           -Wl,--gc-sections \
           -Wl,-Map=$(BUILD)/$(TARGET).map

.PHONY: all size flash clean

all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).hex $(BUILD)/$(TARGET).bin
	$(SZ) $(BUILD)/$(TARGET).elf

size: $(BUILD)/$(TARGET).elf
	$(SZ) $(BUILD)/$(TARGET).elf

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/main.o: main.c main.h | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/ll_gpio.o:  $(LLDIR)/Src/stm32f4xx_ll_gpio.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/ll_rcc.o:   $(LLDIR)/Src/stm32f4xx_ll_rcc.c   | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/ll_utils.o: $(LLDIR)/Src/stm32f4xx_ll_utils.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/system.o:  $(DEVICE)/Source/Templates/system_stm32f4xx.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@
$(BUILD)/startup.o: $(DEVICE)/Source/Templates/gcc/startup_stm32f407xx.s | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/$(TARGET).elf: $(ALL_OBJS) $(LDSCRIPT)
	$(CC) $(CFLAGS) $(LDFLAGS) $(ALL_OBJS) -o $@

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(CP) -O ihex $< $@

# .data has a flash load address in the linker script; include its initializer.
$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(CP) -O binary $< $@

# Options must precede the subcommand on both st-flash 0.0.x and 1.x.
# --reset makes the board start running right after the write.
flash: $(BUILD)/$(TARGET).bin
	$(STFLASH) --reset write $< 0x8000000

clean:
	rm -rf $(BUILD)
