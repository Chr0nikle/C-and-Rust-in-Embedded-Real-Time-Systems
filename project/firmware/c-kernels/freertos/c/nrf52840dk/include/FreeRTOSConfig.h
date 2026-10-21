/* FreeRTOS configuration for the nRF52840 DK (Cortex-M4F). */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configUSE_PREEMPTION                    1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#define configUSE_TICKLESS_IDLE                 0
/* The nRF52840 boots on the 64 MHz HFINT clock; the SysTick timer is clocked
 * by the core, so the tick reload is based on 64 MHz. */
#define configCPU_CLOCK_HZ                      (64000000UL)
#define configTICK_RATE_HZ                      ((TickType_t)1000)
#define configMAX_PRIORITIES                    (28)
#define configMINIMAL_STACK_SIZE                ((unsigned short)128)
#define configTOTAL_HEAP_SIZE                   ((size_t)(100 * 1024))
#define configMAX_TASK_NAME_LEN                 (16)
#define configUSE_16_BIT_TICKS                  0
#define configIDLE_SHOULD_YIELD                 1
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             1
#define configUSE_COUNTING_SEMAPHORES           1
/* Software timers (benchmark case 9: timer callback dispatch latency). */
#define configUSE_TIMERS                        1
#define configTIMER_TASK_PRIORITY               (configMAX_PRIORITIES - 1)
#define configTIMER_QUEUE_LENGTH                10
#define configTIMER_TASK_STACK_DEPTH            (configMINIMAL_STACK_SIZE * 2)
/* Needed by the task_handoff and periodic_jitter benchmark cases. */
#define INCLUDE_vTaskDelete                     1
#define INCLUDE_uxTaskPriorityGet               1
#define INCLUDE_uxTaskGetStackHighWaterMark     1
#define INCLUDE_xTaskDelayUntil                 1
/* Needed by the sleep_accuracy benchmark case (case 15). */
#define INCLUDE_vTaskDelay                      1
#define configUSE_QUEUE_SETS                    0
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_TICK_HOOK                     0
#define configUSE_IDLE_HOOK                     0

/* Case 21 tick_overhead: bracket the SysTick ISR body with DWT CYCCNT reads. */
extern volatile unsigned int g_tick_entry;
extern volatile unsigned int g_tick_last;
extern volatile unsigned int g_tick_ready;
extern volatile unsigned int g_tick_trace_active;
#define traceISR_ENTER() \
    do { if (g_tick_trace_active) { g_tick_entry = *(volatile unsigned int *)0xE0001004u; } } while (0)
#define traceISR_EXIT() \
    do { if (g_tick_trace_active) { \
        g_tick_last = *(volatile unsigned int *)0xE0001004u - g_tick_entry; \
        g_tick_ready = 1u; \
    } } while (0)
#define traceISR_EXIT_TO_SCHEDULER() traceISR_EXIT()
#define configUSE_MALLOC_FAILED_HOOK            1
#define configUSE_APPLICATION_TASK_TAG          0
#define configNUM_THREAD_LOCAL_STORAGE_POINTERS 0

/* Memory allocation: heap_4 (coalescing free list). */
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configSUPPORT_STATIC_ALLOCATION         0

/* Interrupt priorities (nRF52840 implements 3 NVIC priority bits). */
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY          (7U)
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY     (2U)
#define configKERNEL_INTERRUPT_PRIORITY                  (7U << 5)
#define configMAX_SYSCALL_INTERRUPT_PRIORITY             (2U << 5)

/* Port-specific. */
#define configUSE_SYSTICK                   1
#define configUSE_NEWLIB_REENTRANT          0
/* The nRF startup vector table installs vPortSVCHandler/xPortPendSVHandler/
 * xPortSysTickHandler directly, so the direct-installation validation must be
 * off (it only recognises the exception entry points FreeRTOS exports). */
#define configCHECK_HANDLER_INSTALLATION    0

/* Optional: trace/metrics. */
#define configGENERATE_RUN_TIME_STATS       0
#define configUSE_STATS_FORMATTING_FUNCTIONS 0
/* The per-task memory collector enumerates live tasks at complete. This adds
 * uxTCBNumber/uxTaskNumber (8 B per task) and a small amount of flash; it does
 * not affect the timing cases. */
#define configUSE_TRACE_FACILITY            1
#define INCLUDE_uxTaskGetSystemState        1

/* Safety: stack overflow detection (method 2: canary word) and assert. */
#define configCHECK_FOR_STACK_OVERFLOW      2

/* configASSERT is implemented in main.c. Enable it so kernel sanity checks
 * are live. vAssertCalled is provided by the application. */
void vAssertCalled(const char *file, int line);
#define configASSERT( x ) \
    do { \
        if( ( x ) == 0 ) { vAssertCalled( __FILE__, __LINE__ ); } \
    } while( 0 )

#endif /* FREERTOS_CONFIG_H */
