/* Flash-verification blink for the STM32F4-Discovery (STM32F407VG).
 *
 * Based on the STM32CubeF4 Templates_LL example (Projects/STM32F4-Discovery/
 * Templates_LL), reduced to a no-RTOS blink with a UART heartbeat so execution can be
 * confirmed visually:
 *   LED3 (PD13) blinks at 200 ms
 *   LED4 (PD12) at 500 ms
 *   LED5 (PD14) at 1 s
 *   LED6 (PD15) at 2 s
 *
 * In addition, a heartbeat line is printed over USART2 (PA2 = TX, 115200 8N1)
 * every 500 ms so the running board can be verified from the host over a
 * USB-serial adapter (CP2102 dongle on PA2/PA3), not just visually.
 *
 * Clock setup is the template's SystemClock_Config (HSE -> PLL, 168 MHz).
 * All register access goes through the LL API, like the template.
 */
#include "main.h"

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void LED_Init(void);
static void UART_Init(void);
static void UART_TxByte(uint8_t byte);
static void UART_TxString(const char *str);
static void UART_Heartbeat(uint32_t beat);
void SysTick_Handler(void);

static volatile uint32_t g_ms;
static volatile uint32_t g_clock_failed;

int main(void)
{
    uint32_t beat = 0U;

    SystemClock_Config();
    LED_Init();
    UART_Init();
    UART_Heartbeat(beat++);

    uint32_t last3 = g_ms, last4 = g_ms, last5 = g_ms, last6 = g_ms;
    uint32_t last_uart = g_ms;
    for (;;)
    {
        const uint32_t now = g_ms;
        if ((uint32_t)(now - last3) >= 200U) {
            last3 = now;
            LL_GPIO_TogglePin(LED3_GPIO_PORT, LED3_PIN);
        }
        if ((uint32_t)(now - last4) >= 500U) {
            last4 = now;
            LL_GPIO_TogglePin(LED4_GPIO_PORT, LED4_PIN);
        }
        if ((uint32_t)(now - last5) >= 1000U) {
            last5 = now;
            LL_GPIO_TogglePin(LED5_GPIO_PORT, LED5_PIN);
        }
        if ((uint32_t)(now - last6) >= 2000U) {
            last6 = now;
            LL_GPIO_TogglePin(LED6_GPIO_PORT, LED6_PIN);
        }
        if ((uint32_t)(now - last_uart) >= 500U) {
            last_uart = now;
            UART_Heartbeat(beat++);
        }
        __WFI();
    }
}

void SysTick_Handler(void)
{
    ++g_ms;
}

/* ==============   BOARD SPECIFIC CONFIGURATION CODE BEGIN    ============== */

/**
 * @brief System Clock Configuration
 *        The system Clock is configured as follow :
 *           System Clock source            = PLL (HSE)
 *           SYSCLK(Hz)                     = 168000000
 *           HCLK(Hz)                       = 168000000
 *           AHB Prescaler                  = 1
 *           APB1 Prescaler                 = 4
 *           APB2 Prescaler                 = 2
 *           HSE Frequency(Hz)              = 8000000
 *           PLL_M                          = 8
 *           PLL_N                          = 336
 *           PLL_P                          = 2
 *           VDD(V)                         = 3.3
 *           Main regulator output voltage  = Scale1 mode
 *           Flash Latency(WS)              = 5
 */
void SystemClock_Config(void)
{
    /* Set bus dividers before switching to the fast system clock. */
    LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_PWR);
    LL_PWR_SetRegulVoltageScaling(LL_PWR_REGU_VOLTAGE_SCALE1);
    LL_FLASH_SetLatency(LL_FLASH_LATENCY_5);
    LL_RCC_SetAHBPrescaler(LL_RCC_SYSCLK_DIV_1);
    LL_RCC_SetAPB1Prescaler(LL_RCC_APB1_DIV_4);
    LL_RCC_SetAPB2Prescaler(LL_RCC_APB2_DIV_2);

    LL_RCC_HSE_Enable();
    uint32_t timeout = 1000000U;
    while (!LL_RCC_HSE_IsReady() && timeout > 0U) {
        --timeout;
    }
    if (LL_RCC_HSE_IsReady()) {
        LL_RCC_PLL_ConfigDomain_SYS(LL_RCC_PLLSOURCE_HSE, LL_RCC_PLLM_DIV_8,
                                   336, LL_RCC_PLLP_DIV_2);
    } else {
        /* Keep PLL input at 1 MHz with the 16 MHz internal oscillator. */
        g_clock_failed = 1U;
        LL_RCC_HSE_Disable();
        LL_RCC_PLL_ConfigDomain_SYS(LL_RCC_PLLSOURCE_HSI, LL_RCC_PLLM_DIV_16,
                                   336, LL_RCC_PLLP_DIV_2);
    }
    LL_RCC_PLL_Enable();
    timeout = 1000000U;
    while (!LL_RCC_PLL_IsReady() && timeout > 0U) {
        --timeout;
    }
    if (LL_RCC_PLL_IsReady()) {
        LL_RCC_SetSysClkSource(LL_RCC_SYS_CLKSOURCE_PLL);
        timeout = 1000000U;
        while (LL_RCC_GetSysClkSource() != LL_RCC_SYS_CLKSOURCE_STATUS_PLL &&
               timeout > 0U) {
            --timeout;
        }
    }
    if (LL_RCC_GetSysClkSource() != LL_RCC_SYS_CLKSOURCE_STATUS_PLL) {
        /* HSI remains enabled from reset. Derive clocks from actual status. */
        g_clock_failed = 2U;
        LL_RCC_SetSysClkSource(LL_RCC_SYS_CLKSOURCE_HSI);
    }
    SystemCoreClockUpdate();
    SysTick_Config(SystemCoreClock / 1000U);
}

/* ==============   BOARD SPECIFIC CONFIGURATION CODE END      ============== */

static void LED_Init(void)
{
    LL_GPIO_InitTypeDef gpio;

    LED3_GPIO_CLK_ENABLE();
    LED4_GPIO_CLK_ENABLE();
    LED5_GPIO_CLK_ENABLE();
    LED6_GPIO_CLK_ENABLE();

    /* Discovery user LEDs are active-high, driven with push-pull outputs. */
    gpio.Pin        = LED3_PIN | LED4_PIN | LED5_PIN | LED6_PIN;
    gpio.Mode       = LL_GPIO_MODE_OUTPUT;
    gpio.Speed      = LL_GPIO_SPEED_FREQ_LOW;
    gpio.OutputType = LL_GPIO_OUTPUT_PUSHPULL;
    gpio.Pull       = LL_GPIO_PULL_NO;
    gpio.Alternate  = LL_GPIO_AF_0;
    LL_GPIO_Init(LED3_GPIO_PORT, &gpio);

    /* Start with all LEDs off (pins low). */
    LL_GPIO_ResetOutputPin(LED3_GPIO_PORT, LED3_PIN | LED4_PIN | LED5_PIN | LED6_PIN);
}

static void UART_Init(void)
{
    LL_GPIO_InitTypeDef gpio;

    UART_TX_GPIO_CLK_ENABLE();
    UART_RX_GPIO_CLK_ENABLE();
    UART_USART_CLK_ENABLE();

    /* USART2 on APB1: PCLK1 = 168 MHz / 4 = 42 MHz. */
    uint32_t pclk1 = SystemCoreClock / 4U;

    LL_USART_SetDataWidth(UART_USART, LL_USART_DATAWIDTH_8B);
    LL_USART_SetStopBitsLength(UART_USART, LL_USART_STOPBITS_1);
    LL_USART_SetParity(UART_USART, LL_USART_PARITY_NONE);
    LL_USART_SetTransferDirection(UART_USART, LL_USART_DIRECTION_TX_RX);
    LL_USART_SetHWFlowCtrl(UART_USART, LL_USART_HWCONTROL_NONE);
    LL_USART_SetOverSampling(UART_USART, LL_USART_OVERSAMPLING_16);
    LL_USART_SetBaudRate(UART_USART, pclk1, LL_USART_OVERSAMPLING_16, UART_BAUDRATE);
    LL_USART_Enable(UART_USART);

    /* PA2: USART2_TX, push-pull, very-high speed, AF7. */
    gpio.Pin        = UART_TX_PIN;
    gpio.Mode       = LL_GPIO_MODE_ALTERNATE;
    gpio.Speed      = LL_GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.OutputType = LL_GPIO_OUTPUT_PUSHPULL;
    gpio.Pull       = LL_GPIO_PULL_UP;
    gpio.Alternate  = LL_GPIO_AF_7;
    LL_GPIO_Init(UART_TX_GPIO_PORT, &gpio);

    /* PA3: USART2_RX, input (AF7). */
    gpio.Pin        = UART_RX_PIN;
    gpio.Mode       = LL_GPIO_MODE_ALTERNATE;
    gpio.Speed      = LL_GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.OutputType = LL_GPIO_OUTPUT_PUSHPULL;
    gpio.Pull       = LL_GPIO_PULL_UP;
    gpio.Alternate  = LL_GPIO_AF_7;
    LL_GPIO_Init(UART_RX_GPIO_PORT, &gpio);
}

static void UART_TxByte(uint8_t byte)
{
    /* Wait for the data register to be empty before writing the next byte. */
    while (LL_USART_IsActiveFlag_TXE(UART_USART) != 1U)
    {
    }
    LL_USART_TransmitData8(UART_USART, byte);
}

static void UART_TxString(const char *str)
{
    while (*str != '\0')
    {
        UART_TxByte((uint8_t)*str++);
    }
    /* Wait for the last byte to leave the shift register before returning. */
    while (LL_USART_IsActiveFlag_TC(UART_USART) != 1U)
    {
    }
}

static void UART_Heartbeat(uint32_t beat)
{
    char buf[32];
    int i = 0;
    uint32_t v = beat;

    while (v > 0U && i < (int)sizeof(buf) - 2)
    {
        buf[i++] = (char)('0' + (v % 10U));
        v /= 10U;
    }
    if (i == 0)
    {
        buf[i++] = '0';
    }
    /* Reverse into a small stack buffer and prefix. */
    {
        char line[48];
        int j = 0;
        int k;
        line[j++] = '{';
        line[j++] = '"';
        line[j++] = 't';
        line[j++] = 'y';
        line[j++] = 'p';
        line[j++] = 'e';
        line[j++] = '"';
        line[j++] = ':';
        line[j++] = '"';
        line[j++] = 'h';
        line[j++] = 'e';
        line[j++] = 'a';
        line[j++] = 'r';
        line[j++] = 't';
        line[j++] = 'b';
        line[j++] = 'e';
        line[j++] = 'a';
        line[j++] = 't';
        line[j++] = '"';
        line[j++] = ',';
        line[j++] = '"';
        line[j++] = 'b';
        line[j++] = 'e';
        line[j++] = 'a';
        line[j++] = 't';
        line[j++] = '"';
        line[j++] = ':';
        for (k = i - 1; k >= 0; k--)
        {
            line[j++] = buf[k];
        }
        line[j++] = '}';
        line[j++] = '\n';
        line[j] = '\0';
        UART_TxString(line);
    }
}
