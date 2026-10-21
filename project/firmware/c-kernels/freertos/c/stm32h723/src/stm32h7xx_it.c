/* Interrupt handlers for the stm32h723 bench.
 *
 * Wires the FreeRTOS-CM4F port into the standard STM32Cube exception vectors
 * and provides the conventional `Error_Handler` weak definition so any HAL
 * assert path has a target even when `USE_FULL_ASSERT` is off.
 */
#include "stm32h7xx_hal.h"
#include "stm32h7xx_it.h"
#include "FreeRTOS.h"
#include "task.h"
#include "portmacro.h"

/* Forward declarations for the FreeRTOS-CM4F port handlers (declared in
 * port.c but not in portmacro.h). */
void vPortSVCHandler(void);
void xPortPendSVHandler(void);
void xPortSysTickHandler(void);

/* ----------------------------------------------------------------- NMI */
void NMI_Handler(void)
{
    for (;;) {
    }
}

/* ------------------------------------------------------------ HardFault */
void HardFault_Handler(void)
{
    __BKPT(0);
    for (;;) {
    }
}

/* --------------------------------------------------------- MemoryManage */
void MemManage_Handler(void)
{
    for (;;) {
    }
}

/* ------------------------------------------------------------ BusFault */
void BusFault_Handler(void)
{
    for (;;) {
    }
}

/* --------------------------------------------------------- UsageFault */
void UsageFault_Handler(void)
{
    for (;;) {
    }
}

/* ------------------------------------------------------------------ SVC */
void SVC_Handler(void)
{
    vPortSVCHandler();
}

/* ----------------------------------------------------------- DebugMon */
void DebugMon_Handler(void)
{
    for (;;) {
    }
}

/* --------------------------------------------------------------- PendSV */
void PendSV_Handler(void)
{
    xPortPendSVHandler();
}

/* ------------------------------------------------------------- SysTick */
void SysTick_Handler(void)
{
    xPortSysTickHandler();
}

/* ------------------------------------------------------------ error */
void Error_Handler(void)
{
    __BKPT(0);
    for (;;) {
    }
}
