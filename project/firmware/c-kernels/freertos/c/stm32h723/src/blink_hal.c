/* Minimal LED blink using STM32 HAL — same infrastructure as the FreeRTOS build.
 * Toggles LD4 (green, PD12) at 1 Hz. No FreeRTOS, no UART.
 */
#include "stm32h7xx_hal.h"

static GPIO_InitTypeDef led_gpio;

void Error_Handler(void)
{
    __BKPT(0);
    for (;;) {
    }
}

void NMI_Handler(void) { for (;;) { } }
void HardFault_Handler(void) { __BKPT(0); for (;;) { } }
void MemManage_Handler(void) { for (;;) { } }
void BusFault_Handler(void) { for (;;) { } }
void UsageFault_Handler(void) { for (;;) { } }
void SVC_Handler(void) { for (;;) { } }
void DebugMon_Handler(void) { for (;;) { } }
void PendSV_Handler(void) { for (;;) { } }
void SysTick_Handler(void) { HAL_IncTick(); }

int main(void)
{
    HAL_Init();

    __HAL_RCC_GPIOD_CLK_ENABLE();

    led_gpio.Pin       = GPIO_PIN_12;
    led_gpio.Mode      = GPIO_MODE_OUTPUT_PP;
    led_gpio.Pull      = GPIO_NOPULL;
    led_gpio.Speed     = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOD, &led_gpio);

    HAL_GPIO_WritePin(GPIOD, GPIO_PIN_12, GPIO_PIN_SET);

    for (;;) {
        HAL_GPIO_TogglePin(GPIOD, GPIO_PIN_12);
        HAL_Delay(500);
    }
}
