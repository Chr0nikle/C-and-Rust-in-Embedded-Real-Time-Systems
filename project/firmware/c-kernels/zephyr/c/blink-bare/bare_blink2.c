/* Ultra-minimal LED blink — NO HAL, NO clock config, NO delay.
 * Just toggle PD12 (LD4) with a raw busy-wait. If this doesn't blink,
 * the MCU is not executing code at all.
 */
#include <stdint.h>

#define RCC_BASE      0x40023800
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

void delay(volatile uint32_t n)
{
    while (n--) {
    }
}

int main(void)
{
    /* Enable GPIOD clock (bit 2 of AHB1ENR). */
    RCC_AHB1ENR |= (1u << 2);

    /* Set PD12 to output mode (bits 25:24 = 01). */
    GPIOD_MODER &= ~(3u << 24);
    GPIOD_MODER |= (1u << 24);

    for (;;) {
        GPIOD_ODR ^= (1u << 12);
        delay(2000000);
    }
}
