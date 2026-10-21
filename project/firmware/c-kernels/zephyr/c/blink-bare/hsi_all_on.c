/* Diagnostic: use HSI (internal 16MHz RC) instead of HSE crystal.
 * If the board's HSE crystal is faulty or not connected, this will work
 * where other firmware fails. Turns all 4 user LEDs on.
 */
#include <stdint.h>

#define RCC_BASE      0x40023800
#define RCC_CR        (*(volatile uint32_t*)(RCC_BASE + 0x00))
#define RCC_PLLCFGR   (*(volatile uint32_t*)(RCC_BASE + 0x3C))
#define RCC_PLLCFGR_OFFSET 0x3C
#define RCC_AHB1ENR   (*(volatile uint32_t*)(RCC_BASE + 0x30))
#define GPIOD_BASE    0x40020C00
#define GPIOD_MODER   (*(volatile uint32_t*)(GPIOD_BASE + 0x00))
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

int main(void)
{
    /* RCC_CR: enable HSI (bit 0), disable PLL (bit 24), disable HSE (bit 16). */
    RCC_CR &= ~(1u << 0);   /* HSI off (reset state) */
    RCC_CR |=  (1u << 0);   /* HSI on */
    
    /* Wait for HSI ready (bit 10). */
    while (!(RCC_CR & (1u << 10))) {
    }

    /* Enable GPIOD clock. */
    RCC_AHB1ENR |= (1u << 2);

    /* PD12-15 to output. */
    GPIOD_MODER &= ~(0x00FF0000u);
    GPIOD_MODER |=  0x00110000u;

    /* All four LEDs ON (active-low). */
    GPIOD_ODR &= ~0x0000F000u;

    for (;;) {
    }
}
