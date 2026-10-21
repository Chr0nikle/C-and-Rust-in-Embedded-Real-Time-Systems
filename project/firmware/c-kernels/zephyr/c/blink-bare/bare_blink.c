/* Bare-metal blink for STM32F407 — no Zephyr, no drivers.
 * Toggles LD4 (green, PD12) at ~1 Hz. Enables HSE + PLL + GPIOD clock.
 * If this blinks, the flash/reset cycle works.
 */
#include <stdint.h>

#define REG(addr) (*(volatile uint32_t *)(addr))

#define RCC_CR        REG(0x40021000)
#define RCC_PLLCFGR   REG(0x40021004)
#define RCC_CFGR      REG(0x40021008)
#define RCC_AHB1ENR   REG(0x40020004)

#define GPIOD_MODER   REG(0x40020C00)
#define GPIOD_OSPEEDR REG(0x40020C08)
#define GPIOD_BSRR    REG(0x40020C18)

int main(void)
{
    /* 1. Enable HSE (8 MHz crystal) */
    RCC_CR |= (1U << 16);
    for (volatile uint32_t i = 0; i < 2000000U; i++) {
        if (RCC_CR & (1U << 17)) break;
    }

    /* 2. Configure PLL: 8 MHz * 336 / 8 / 2 = 168 MHz */
    RCC_PLLCFGR = (RCC_PLLCFGR & ~(0xF0000000U))
                | (8U - 1)
                | ((336U - 1) << 6)
                | (0U << 16);

    /* 3. Enable PLL and wait for lock */
    RCC_CR |= (1U << 24);
    for (volatile uint32_t i = 0; i < 2000000U; i++) {
        if (RCC_CR & (1U << 25)) break;
    }

    /* 4. Switch system clock to PLL */
    RCC_CFGR = (RCC_CFGR & ~(7U)) | (2U);
    for (volatile uint32_t i = 0; i < 2000000U; i++) {
        if ((RCC_CFGR & 7U) == 2U) break;
    }

    /* 5. Enable GPIOD clock (bit 3 of AHB1ENR) */
    RCC_AHB1ENR |= (1U << 3);

    /* 6. Configure PD12 (LD4 green) as output, high speed */
    GPIOD_MODER = (GPIOD_MODER & ~(3U << 24)) | (1U << 24);
    GPIOD_OSPEEDR = (GPIOD_OSPEEDR & ~(3U << 24)) | (2U << 24);

    /* 7. Blink PD12 at ~1 Hz */
    while (1) {
        GPIOD_BSRR = (1U << 12);
        for (volatile uint32_t i = 0; i < 4200000U; i++) { }
        GPIOD_BSRR = (1U << (16 + 12));
        for (volatile uint32_t i = 0; i < 4200000U; i++) { }
    }
}
