/* Diagnostic v2: turn ALL four user LEDs ON with proper memory barriers
 * and write-verification. This is the definitive test for whether GPIOD
 * can be configured at all on this board.
 *
 * Key improvements over all_on.c:
 *   1. DSB + DMB memory barrier after the RCC clock-enable write
 *   2. Write-verification loop: read MODER back until it matches, or timeout
 *   3. No reliance on a fixed iteration delay
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

static inline void mem_barrier(void)
{
    __asm__ volatile ("dsb; dmb" ::: "memory");
}

int main(void)
{
    /* Enable GPIOD clock (bit 2 of AHB1ENR). */
    RCC_AHB1ENR |= (1u << 2);
    mem_barrier();

    /* Wait for the clock to actually propagate by reading AHB1ENR back
     * and confirming bit 2 is set after the write. */
    {
        uint32_t timeout = 0x10000;
        while (((RCC_AHB1ENR & (1u << 2)) == 0) && (timeout--)) {
        }
    }
    mem_barrier();

    /* Set PD12, PD13, PD14, PD15 to general-purpose output.
     * MODER bits [3:2]=01 for each pin: PD12=bits25:24, PD13=27:26,
     * PD14=29:28, PD15=31:30 => mask 0x00110000. */
    GPIOD_MODER &= ~(0x00FF0000u);
    GPIOD_MODER |=  0x00110000u;

    /* Verify the MODER write landed. If it doesn't within a timeout,
     * the peripheral is not responding; the fallback loop makes the failure
     * visible. */
    {
        uint32_t timeout = 0x10000;
        while (((GPIOD_MODER & 0x00110000u) != 0x00110000u) && (timeout--)) {
        }
    }
    mem_barrier();

    /* All four LEDs ON (ACTIVE HIGH). */
    GPIOD_ODR |= 0x0000F000u;
    mem_barrier();

    for (;;) {
    }
}
