/* CubeF4-style LED blinker for the STM32F407 Discovery (MB997B).
 *
 * Uses the STM32F4xx HAL exactly as ST's GPIO_EXTI example does:
 *   - HAL_Init()
 *   - SystemClock_Config() -> 168 MHz from HSE + PLL (ST's canonical config)
 *   - GPIO output on PD12/13/14/15 (LD4/LD3/LD5/LD6) toggled every 500 ms
 *
 * This is a hardware bring-up test: if the four user LEDs blink, the chip,
 * the HSE crystal, the PLL, the AHB1 clock tree and the GPIOD peripheral
 * all work. It isolates the MCU from any RTOS or benchmark register writes.
 */
#include "main.h"

/* LED pins: LD3=PD13, LD4=PD12, LD5=PD14, LD6=PD15 (UM1472 §7.5). */
static const struct {
    GPIO_TypeDef *port;
    uint16_t      pin;
} leds[] = {
    { GPIOD, GPIO_PIN_12 }, /* LD4 green  */
    { GPIOD, GPIO_PIN_13 }, /* LD3 orange */
    { GPIOD, GPIO_PIN_14 }, /* LD5 red    */
    { GPIOD, GPIO_PIN_15 }, /* LD6 blue   */
};
#define N_LEDS (sizeof(leds) / sizeof(leds[0]))

static void SystemClock_Config(void)
{
    RCC_OscInitTypeDef  osc = {0};
    RCC_ClkInitTypeDef  clk = {0};

    __HAL_RCC_PWR_CLK_ENABLE();
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

    /* HSE 8 MHz -> PLL -> SYSCLK 168 MHz (ST's GPIO_EXTI config). */
    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState       = RCC_HSE_ON;
    osc.PLL.PLLState   = RCC_PLL_ON;
    osc.PLL.PLLSource  = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM       = 8;
    osc.PLL.PLLN       = 336;
    osc.PLL.PLLP       = RCC_PLLP_DIV2;
    osc.PLL.PLLQ       = 7;
    if (HAL_RCC_OscConfig(&osc) != HAL_OK) {
        Error_Handler();
    }

    clk.ClockType      = RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK
                       | RCC_CLOCKTYPE_PCLK1  | RCC_CLOCKTYPE_PCLK2;
    clk.SYSCLKSource   = RCC_SYSCLKSOURCE_PLLCLK;
    clk.AHBCLKDivider  = RCC_SYSCLK_DIV1;
    clk.APB1CLKDivider = RCC_HCLK_DIV4;
    clk.APB2CLKDivider = RCC_HCLK_DIV2;
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_5) != HAL_OK) {
        Error_Handler();
    }

    /* Rev-Z+ prefetch enable (no-op on older revs, safe either way). */
    if (HAL_GetREVID() >= 0x1001) {
        FLASH->ACR |= FLASH_ACR_DCEN;
    }
}

static void LED_GPIO_Config(void)
{
    GPIO_InitTypeDef gpio = {0};
    __HAL_RCC_GPIOD_CLK_ENABLE();
    gpio.Pin   = GPIO_PIN_12 | GPIO_PIN_13 | GPIO_PIN_14 | GPIO_PIN_15;
    gpio.Mode  = GPIO_MODE_OUTPUT_PP;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;
    gpio.Pull  = GPIO_NOPULL;
    HAL_GPIO_Init(GPIOD, &gpio);
    HAL_GPIO_WritePin(GPIOD, gpio.Pin, GPIO_PIN_RESET);
}

static void Error_Handler(void)
{
    for (;;) {
    }
}

int main(void)
{
    HAL_Init();
    SystemClock_Config();
    LED_GPIO_Config();

    uint32_t tick = 0;
    for (;;) {
        HAL_Delay(500);
        for (uint32_t i = 0; i < N_LEDS; i++) {
            HAL_GPIO_TogglePin(leds[i].port, leds[i].pin);
        }
        tick++;
    }
}
