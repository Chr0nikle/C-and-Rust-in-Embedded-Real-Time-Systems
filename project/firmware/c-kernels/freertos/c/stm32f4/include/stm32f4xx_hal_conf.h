/* HAL configuration for the stm32f4discovery bench.
 *
 * Enables only the modules required by the smoke firmware: core, RCC, GPIO,
 * UART, PWR, DMA, and EXTI. Everything else is left off to keep the image
 * small. HSE/HSI values are the board defaults; USE_FULL_ASSERT is off.
 */
#ifndef __STM32F4xx_HAL_CONF_H
#define __STM32F4xx_HAL_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

/* Module selection: only what the smoke firmware needs. */
#define HAL_MODULE_ENABLED
/* #define HAL_ADC_MODULE_ENABLED          */
/* #define HAL_CAN_MODULE_ENABLED          */
/* #define HAL_CAN_LEGACY_MODULE_ENABLED   */
/* #define HAL_CRC_MODULE_ENABLED          */
/* #define HAL_CRYP_MODULE_ENABLED         */
/* #define HAL_DAC_MODULE_ENABLED          */
/* #define HAL_DCMI_MODULE_ENABLED         */
#define HAL_DMA_MODULE_ENABLED
/* #define HAL_DMA2D_MODULE_ENABLED        */
/* #define HAL_ETH_MODULE_ENABLED          */
#define HAL_FLASH_MODULE_ENABLED
/* #define HAL_NAND_MODULE_ENABLED         */
/* #define HAL_NOR_MODULE_ENABLED          */
/* #define HAL_PCCARD_MODULE_ENABLED       */
/* #define HAL_SRAM_MODULE_ENABLED         */
/* #define HAL_SDRAM_MODULE_ENABLED        */
/* #define HAL_HASH_MODULE_ENABLED         */
#define HAL_GPIO_MODULE_ENABLED
/* #define HAL_I2C_MODULE_ENABLED          */
/* #define HAL_I2S_MODULE_ENABLED          */
/* #define HAL_IWDG_MODULE_ENABLED         */
/* #define HAL_LTDC_MODULE_ENABLED         */
#define HAL_PWR_MODULE_ENABLED
#define HAL_RCC_MODULE_ENABLED
/* #define HAL_RNG_MODULE_ENABLED          */
/* #define HAL_RTC_MODULE_ENABLED          */
/* #define HAL_SAI_MODULE_ENABLED          */
/* #define HAL_SD_MODULE_ENABLED           */
/* #define HAL_SPI_MODULE_ENABLED          */
/* #define HAL_TIM_MODULE_ENABLED          */
#define HAL_UART_MODULE_ENABLED
/* #define HAL_USART_MODULE_ENABLED        */
/* #define HAL_IRDA_MODULE_ENABLED         */
/* #define HAL_SMARTCARD_MODULE_ENABLED    */
/* #define HAL_WWDG_MODULE_ENABLED         */
/* #define HAL_PCD_MODULE_ENABLED          */
/* #define HAL_HCD_MODULE_ENABLED          */
#define HAL_CORTEX_MODULE_ENABLED
#define HAL_EXTI_MODULE_ENABLED

/* HSE/HSI values: board defaults (STM32F4 Discovery has an 8 MHz HSE crystal
 * but we do not configure the PLL in the smoke path, so HSE is informational). */
#if !defined(HSE_VALUE)
#define HSE_VALUE (8000000U)
#endif
#if !defined(HSE_STARTUP_TIMEOUT)
#define HSE_STARTUP_TIMEOUT (100U)
#endif
#if !defined(HSI_VALUE)
#define HSI_VALUE (16000000U)
#endif
#if !defined(LSI_VALUE)
#define LSI_VALUE (32000U)
#endif
#if !defined(LSE_VALUE)
#define LSE_VALUE (32768U)
#endif
#if !defined(LSE_STARTUP_TIMEOUT)
#define LSE_STARTUP_TIMEOUT (5000U)
#endif
#if !defined(EXTERNAL_CLOCK_VALUE)
#define EXTERNAL_CLOCK_VALUE (12288000U)
#endif

/* System configuration. */
#define VDD_VALUE (3300U)
#define TICK_INT_PRIORITY (0x0FU)
#define USE_RTOS 0U
#define PREFETCH_ENABLE 0U
#define INSTRUCTION_CACHE_ENABLE 1U
#define DATA_CACHE_ENABLE 1U

/* Register callbacks: off. */
#define USE_HAL_ADC_REGISTER_CALLBACKS 0U
#define USE_HAL_CAN_REGISTER_CALLBACKS 0U
#define USE_HAL_CEC_REGISTER_CALLBACKS 0U
#define USE_HAL_CRYP_REGISTER_CALLBACKS 0U
#define USE_HAL_DAC_REGISTER_CALLBACKS 0U
#define USE_HAL_DCMI_REGISTER_CALLBACKS 0U
#define USE_HAL_DFSDM_REGISTER_CALLBACKS 0U
#define USE_HAL_DMA2D_REGISTER_CALLBACKS 0U
#define USE_HAL_DSI_REGISTER_CALLBACKS 0U
#define USE_HAL_ETH_REGISTER_CALLBACKS 0U
#define USE_HAL_HASH_REGISTER_CALLBACKS 0U
#define USE_HAL_HCD_REGISTER_CALLBACKS 0U
#define USE_HAL_I2C_REGISTER_CALLBACKS 0U
#define USE_HAL_FMPI2C_REGISTER_CALLBACKS 0U
#define USE_HAL_I2S_REGISTER_CALLBACKS 0U
#define USE_HAL_IRDA_REGISTER_CALLBACKS 0U
#define USE_HAL_LPTIM_REGISTER_CALLBACKS 0U
#define USE_HAL_LTDC_REGISTER_CALLBACKS 0U
#define USE_HAL_MMC_REGISTER_CALLBACKS 0U
#define USE_HAL_NAND_REGISTER_CALLBACKS 0U
#define USE_HAL_NOR_REGISTER_CALLBACKS 0U
#define USE_HAL_PCCARD_REGISTER_CALLBACKS 0U
#define USE_HAL_PCD_REGISTER_CALLBACKS 0U
#define USE_HAL_QSPI_REGISTER_CALLBACKS 0U
#define USE_HAL_RNG_REGISTER_CALLBACKS 0U
#define USE_HAL_RTC_REGISTER_CALLBACKS 0U
#define USE_HAL_SAI_REGISTER_CALLBACKS 0U
#define USE_HAL_SD_REGISTER_CALLBACKS 0U
#define USE_HAL_SMARTCARD_REGISTER_CALLBACKS 0U
#define USE_HAL_SDRAM_REGISTER_CALLBACKS 0U
#define USE_HAL_SRAM_REGISTER_CALLBACKS 0U
#define USE_HAL_SPDIFRX_REGISTER_CALLBACKS 0U
#define USE_HAL_SMBUS_REGISTER_CALLBACKS 0U
#define USE_HAL_SPI_REGISTER_CALLBACKS 0U
#define USE_HAL_TIM_REGISTER_CALLBACKS 0U
#define USE_HAL_UART_REGISTER_CALLBACKS 0U
#define USE_HAL_USART_REGISTER_CALLBACKS 0U
#define USE_HAL_WWDG_REGISTER_CALLBACKS 0U

/* Assert selection: off. */
/* #define USE_FULL_ASSERT 1U */

#ifdef USE_FULL_ASSERT
  #define assert_param(expr) ((expr) ? (void)0 : assert_failed((uint8_t *)__FILE__, __LINE__))
  void assert_failed(uint8_t *file, uint32_t line);
#else
  #define assert_param(expr) ((void)0U)
#endif /* USE_FULL_ASSERT */

/* SPI CRC. */
#define USE_SPI_CRC 1U

/* Includes. */
#ifdef HAL_RCC_MODULE_ENABLED
  #include "stm32f4xx_hal_rcc.h"
#endif
#ifdef HAL_GPIO_MODULE_ENABLED
  #include "stm32f4xx_hal_gpio.h"
#endif
#ifdef HAL_DMA_MODULE_ENABLED
  #include "stm32f4xx_hal_dma.h"
#endif
#ifdef HAL_CORTEX_MODULE_ENABLED
  #include "stm32f4xx_hal_cortex.h"
#endif
#ifdef HAL_EXTI_MODULE_ENABLED
  #include "stm32f4xx_hal_exti.h"
#endif
#ifdef HAL_PWR_MODULE_ENABLED
  #include "stm32f4xx_hal_pwr.h"
#endif
#ifdef HAL_FLASH_MODULE_ENABLED
  #include "stm32f4xx_hal_flash.h"
#endif
#ifdef HAL_UART_MODULE_ENABLED
  #include "stm32f4xx_hal_uart.h"
#endif

#ifdef __cplusplus
}
#endif

#endif /* __STM32F4xx_HAL_CONF_H */
