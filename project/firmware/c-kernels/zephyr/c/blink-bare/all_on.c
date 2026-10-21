/* Diagnostic: turn ALL four user LEDs ON and stay there.
 * Per Zephyr stm32f4_disco.dts, LD3-LD6 are PD13/PD12/PD14/PD15 and are
 * ACTIVE HIGH (driving the pin HIGH lights the LED).
 *
 * Key fix: after enabling the GPIOD clock via RCC_AHB1ENR, the AHB clock
 * needs a few cycles to propagate. Write to GPIO registers immediately can
 * silently fail (the peripheral is not clocked yet). A read of
 * the just-written AHB1ENR (a dummy read) plus a short delay before touching
 * GPIOD_MODER / GPIOD_ODR.
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

int main(void)
{
    /* Enable GPIOD clock (bit 2 of AHB1ENR). */
    RCC_AHB1ENR |= (1u << 2);

    /* Wait for the AHB clock to propagate to the GPIOD peripheral.
     * A read-back of AHB1ENR forces a bus transaction; a short delay
     * gives the clock synchronizer time to stabilize. */
    (void)RCC_AHB1ENR;          /* dummy read */
    {
        volatile uint32_t n = 200;
        while (n--) { }
    }

    /* PD12, PD13, PD14, PD15 to output (bits 29:24 = 0101). */
    GPIOD_MODER &= ~(0x00FF0000u);
    GPIOD_MODER |=  0x00110000u;

    /* All four LEDs ON (ACTIVE HIGH). */
    GPIOD_ODR |= 0x0000F000u;

    for (;;) {
    }
}
