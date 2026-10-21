/* Definitive polarity test: light all 4 user LEDs on BOTH polarities in
 * sequence to identify which polarity drives the LEDs on this board.
 *
 * Phase 1: active-HIGH (ODR |= 0xF000)  for ~2 seconds
 * Phase 2: active-LOW  (ODR &= ~0xF000) for ~2 seconds
 * Loop.
 *
 * Uses the known-good STM32F4 address map (RCC 0x40023800, GPIOD 0x40020C00).
 * HSI is the default system clock after reset, so no PLL/HSE setup needed.
 */
#include <stdint.h>

#define RCC_BASE      0x40023800
#define RCC_AHB1ENR   (*(volatile uint32_t*)(RCC_BASE + 0x30))
#define GPIOD_BASE    0x40020C00
#define GPIOD_MODER   (*(volatile uint32_t*)(GPIOD_BASE + 0x00))
#define GPIOD_OSPEEDR (*(volatile uint32_t*)(GPIOD_BASE + 0x08))
#define GPIOD_ODR     (*(volatile uint32_t*)(GPIOD_BASE + 0x14))

void Error_Handler(void) { for (;;) { } }
void NMI_Handler(void) { for (;;) { } }
void HardFault_Handler(void) { for (;;) { } }
void MemManage_Handler(void) { for (;;) { } }
void BusFault_Handler(void) { for (;;) { } }
void UsageFault_Handler(void) { for (;;) { } }
void SVC_Handler(void) { for (;;) { } }
void DebugMon_Handler(void) { for (;;) { } }
void PendSV_Handler(void) { for (;;) { } }
void SysTick_Handler(void) { for (;;) { } }

static void delay(volatile uint32_t n)
{
    while (n--) {
    }
}

int main(void)
{
    /* Enable GPIOD clock (bit 2). HSI is already running as default. */
    RCC_AHB1ENR |= (1u << 2);

    /* Small delay for the AHB clock to propagate to the peripheral. */
    delay(10000);

    /* PD12-15 to general-purpose output, medium speed. */
    GPIOD_MODER  &= ~(0x00FF0000u);
    GPIOD_MODER  |=  0x00110000u;   /* 01 = output for each of PD12-15 */
    GPIOD_OSPEEDR &= ~(0x00FF0000u);
    GPIOD_OSPEEDR |=  0x00440000u;  /* 10 = medium speed */

    delay(10000);

    for (;;) {
        /* Phase 1: ACTIVE-HIGH (drive pins HIGH). */
        GPIOD_ODR |= 0x0000F000u;
        delay(40000000);   /* ~2 s at 16 MHz HSI */

        /* Phase 2: ACTIVE-LOW (drive pins LOW). */
        GPIOD_ODR &= ~0x0000F000u;
        delay(40000000);   /* ~2 s at 16 MHz HSI */
    }
}
