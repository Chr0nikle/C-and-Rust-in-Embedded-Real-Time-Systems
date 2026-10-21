/* Linker script for the nRF52840 DK (PCA10056): 1 MiB flash, 256 KiB RAM.
 * The FreeRTOS Cortex-M4F port supplies its own heap (heap_4); the main stack
 * (MSP) grows down from the top of RAM. */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 1024K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 256K
}

SECTIONS
{
  .isr_vector :
  {
    KEEP(*(.isr_vector))
  } > FLASH

  .text :
  {
    *(.text*)
    *(.init)
    *(.fini)
    *(.rodata*)
    KEEP(*(.init_array*))
    KEEP(*(.fini_array*))
    *(.glue_7)
    *(.glue_7t)
    *(.ARM.extab*)
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
    . = ALIGN(4);
    _etext = .;
  } > FLASH

  .data : AT(_etext)
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } > RAM

  .bss :
  {
    . = ALIGN(4);
    _sbss = .;
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
  } > RAM

  . = ALIGN(8);
  PROVIDE(end = .);
  PROVIDE(_end = .);

  .ARM.attributes 0 : { *(.ARM.attributes) }
}
