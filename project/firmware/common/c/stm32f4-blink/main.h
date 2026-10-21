/* Header for the STM32F4-Discovery LL blink (flash-verification) firmware.
 *
 * LL driver includes mirror the STM32CubeF4 Templates_LL example; only the
 * subset needed here is actually compiled (see Makefile).
 */
#ifndef __MAIN_H
#define __MAIN_H

#include "stm32f4xx_ll_bus.h"
#include "stm32f4xx_ll_rcc.h"
#include "stm32f4xx_ll_system.h"
#include "stm32f4xx_ll_utils.h"
#include "stm32f4xx_ll_pwr.h"
#include "stm32f4xx_ll_exti.h"
#include "stm32f4xx_ll_gpio.h"
#include "stm32f4xx_ll_cortex.h"
#include "stm32f4xx_ll_usart.h"
#include "stm32f4xx_ll_tim.h"
#include "stm32f4xx_ll_dma.h"

/* ==============   BOARD SPECIFIC CONFIGURATION CODE BEGIN    ============== */
/**
 * @brief LED3
 */
#define LED3_PIN                           LL_GPIO_PIN_13
#define LED3_GPIO_PORT                     GPIOD
#define LED3_GPIO_CLK_ENABLE()             LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOD)

/**
 * @brief LED4
 */
#define LED4_PIN                           LL_GPIO_PIN_12
#define LED4_GPIO_PORT                     GPIOD
#define LED4_GPIO_CLK_ENABLE()             LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOD)

/**
 * @brief LED5
 */
#define LED5_PIN                           LL_GPIO_PIN_14
#define LED5_GPIO_PORT                     GPIOD
#define LED5_GPIO_CLK_ENABLE()             LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOD)

/**
 * @brief LED6
 */
#define LED6_PIN                           LL_GPIO_PIN_15
#define LED6_GPIO_PORT                     GPIOD
#define LED6_GPIO_CLK_ENABLE()             LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOD)

/* USART2 (PA2 = TX, PA3 = RX), 115200 8N1. Matches the UM1472 §7.3.3
 * recommendation for reaching the ST-Link VCP / external USB-serial dongle.
 * USART2 sits on APB1 (PCLK1 = 168 MHz / 4 = 42 MHz). */
#define UART_TX_GPIO_PORT                  GPIOA
#define UART_TX_PIN                        LL_GPIO_PIN_2
#define UART_TX_GPIO_CLK_ENABLE()          LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOA)
#define UART_RX_GPIO_PORT                  GPIOA
#define UART_RX_PIN                        LL_GPIO_PIN_3
#define UART_RX_GPIO_CLK_ENABLE()          LL_AHB1_GRP1_EnableClock(LL_AHB1_GRP1_PERIPH_GPIOA)
#define UART_USART                         USART2
#define UART_USART_CLK_ENABLE()            LL_APB1_GRP1_EnableClock(LL_APB1_GRP1_PERIPH_USART2)
#define UART_BAUDRATE                      115200U
/* ==============   BOARD SPECIFIC CONFIGURATION CODE END      ============== */

#endif /* __MAIN_H */
