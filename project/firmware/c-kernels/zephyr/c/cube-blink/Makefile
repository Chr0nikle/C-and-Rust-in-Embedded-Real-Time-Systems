# CubeF4-style LED blinker for STM32F407 Discovery (MB997B).
# Plain Makefile + st-flash. Compiles ST's HAL subset + our main.c with the
# same recipe as the FreeRTOS smoke firmware, using the pinned toolchain.

TARGET  = cube-blink
BUILD   = build

ROOT   := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
REPO   := $(ROOT)/../../../../../
CUBE   := $(REPO).local/source/STM32CubeF4
DRIVER := $(CUBE)/Drivers
CMSIS  := $(DRIVER)/CMSIS
DEVICE := $(CMSIS)/Device/ST/STM32F4xx
HAL    := $(DRIVER)/STM32F4xx_HAL_Driver

TC     := $(REPO).local/toolchains/arm-none-eabi/arm-gnu-toolchain-14.2.rel1-x86_64-arm-none-eabi
CROSS  ?= $(TC)/bin/arm-none-eabi-
CC      = $(CROSS)gcc
CP      = $(CROSS)objcopy
SZ      = $(CROSS)size
STFLASH ?= st-flash

APP_OBJS = $(BUILD)/main.o $(BUILD)/it.o
HAL_OBJS = $(BUILD)/hal.o $(BUILD)/hal_gpio.o $(BUILD)/hal_rcc.o \
           $(BUILD)/hal_rcc_ex.o $(BUILD)/hal_cortex.o \
           $(BUILD)/hal_pwr.o $(BUILD)/hal_pwr_ex.o $(BUILD)/hal_flash.o
SYS_OBJS = $(BUILD)/system.o $(BUILD)/startup.o

ALL_OBJS = $(APP_OBJS) $(HAL_OBJS) $(SYS_OBJS)

CFLAGS   = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           -O2 -fdata-sections -ffunction-sections -Wall -Wextra \
           -Wno-unused-parameter -ffunction-sections -fdata-sections
CPPFLAGS = -DSTM32F407xx -DUSE_HAL_DRIVER
INCLUDES = -Iinc \
           -I$(CMSIS)/Include \
           -I$(DEVICE)/Include \
           -I$(HAL)/Inc

LDFLAGS  = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard \
           --specs=nano.specs --specs=nosys.specs \
           -T $(ROOT)/../../../../c-kernels/freertos/c/stm32f4/stm32f407vgtx_flash.ld \
           -Wl,--gc-sections \
           -Wl,-Map=$(BUILD)/$(TARGET).map

.PHONY: all size flash clean

all: $(BUILD)/$(TARGET).elf $(BUILD)/$(TARGET).hex $(BUILD)/$(TARGET).bin
	$(SZ) $(BUILD)/$(TARGET).elf

$(BUILD)/$(TARGET).elf: $(ALL_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $(ALL_OBJS) -o $@
	$(CP) -O ihex $@ $@.hex
	$(CP) -O binary $@ $(BUILD)/$(TARGET).bin

$(BUILD)/main.o: src/main.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/it.o: src/stm32f4xx_it.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/hal.o:        $(HAL)/Src/stm32f4xx_hal.c        | $(BUILD)
$(BUILD)/hal_gpio.o:   $(HAL)/Src/stm32f4xx_hal_gpio.c   | $(BUILD)
$(BUILD)/hal_rcc.o:    $(HAL)/Src/stm32f4xx_hal_rcc.c    | $(BUILD)
$(BUILD)/hal_rcc_ex.o: $(HAL)/Src/stm32f4xx_hal_rcc_ex.c | $(BUILD)
$(BUILD)/hal_cortex.o: $(HAL)/Src/stm32f4xx_hal_cortex.c | $(BUILD)
$(BUILD)/hal_pwr.o:    $(HAL)/Src/stm32f4xx_hal_pwr.c    | $(BUILD)
$(BUILD)/hal_pwr_ex.o: $(HAL)/Src/stm32f4xx_hal_pwr_ex.c | $(BUILD)
$(BUILD)/hal_flash.o:  $(HAL)/Src/stm32f4xx_hal_flash.c  | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/system.o: $(DEVICE)/Source/Templates/system_stm32f4xx.c | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD)/startup.o: $(DEVICE)/Source/Templates/gcc/startup_stm32f407xx.s | $(BUILD)
	$(CC) $(CFLAGS) $(CPPFLAGS) $(INCLUDES) -c $< -o $@

$(BUILD):
	mkdir -p $(BUILD)

flash: $(BUILD)/$(TARGET).bin
	$(STFLASH) write $(BUILD)/$(TARGET).bin 0x08000000
	$(STFLASH) reset

clean:
	rm -rf $(BUILD)
