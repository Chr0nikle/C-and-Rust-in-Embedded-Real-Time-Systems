/* Ultra-minimal LED blink — toggle all four user LEDs (PD12-15).
 * Per Zephyr stm32f4_disco.dts the user LEDs are ACTIVE HIGH: driving a pin
 * HIGH lights the LED. Toggling the bits produces the blink regardless of
 * polarity.
 */
#include <stdint.h>

#define RCC_AHB1ENR   (*(volatile uint32_t*)(0x40023830))
#define GPIOD_MODER   (*(volatile uint32_t*)(0x40020C00))
#define GPIOD_ODR     (*(volatile uint32_t*)(0x40020C14))

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
    RCC_AHB1ENR |= (1u << 2);

    /* PD12-15 to output. */
    GPIOD_MODER &= ~(0x00FF0000u);
    GPIOD_MODER |=  0x00110000u;

    for (;;) {
        GPIOD_ODR ^= 0x0000F000u;
        delay(2000000);
    }
}
