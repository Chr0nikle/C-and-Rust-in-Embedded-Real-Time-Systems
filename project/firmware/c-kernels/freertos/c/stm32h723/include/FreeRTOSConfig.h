/* FreeRTOS configuration for STM32H723 (Cortex-M7). */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configUSE_PREEMPTION                    1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#define configUSE_TICKLESS_IDLE                 0
/* The Cortex-M7 core runs at 520 MHz SYSCLK (25 MHz HSE -> PLL1 M=5,N=104,P=1).
 * The SysTick timer is clocked by the core, so the reload is based on 520 MHz. */
#define configCPU_CLOCK_HZ                      (520000000UL)
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
/* Case 15 (sleep_accuracy) calls vTaskDelay; it is not compiled by default. */
#define INCLUDE_vTaskDelay                      1
#define configUSE_QUEUE_SETS                    0
#define configUSE_TASK_NOTIFICATIONS            1
#define configUSE_TICK_HOOK                     0
#define configUSE_IDLE_HOOK                     0

/* Case 21 tick_overhead: bracket the SysTick ISR body in port.c's
 * xPortSysTickHandler with DWT CYCCNT reads. The globals live in src/bench.c;
 * the flag keeps the hooks inert unless the case is running. */
extern volatile unsigned int g_tick_entry;
extern volatile unsigned int g_tick_last;
extern volatile unsigned int g_tick_ready;
extern volatile unsigned int g_tick_trace_active;
/* Case 22/23 CPU share: accumulate the tick-ISR cycles into g_cpu_isr_accum for
 * the duration of the fixed idle window. */
extern volatile unsigned int g_cpu_isr_active;
extern volatile unsigned long long g_cpu_isr_accum;
#define traceISR_ENTER() \
    do { if (g_tick_trace_active) { g_tick_entry = *(volatile unsigned int *)0xE0001004u; } } while (0)
#define traceISR_EXIT() \
    do { if (g_tick_trace_active) { \
        g_tick_last = *(volatile unsigned int *)0xE0001004u - g_tick_entry; \
        g_tick_ready = 1u; \
        if (g_cpu_isr_active) { g_cpu_isr_accum += g_tick_last; } \
    } } while (0)
#define traceISR_EXIT_TO_SCHEDULER() traceISR_EXIT()

/* Case 23 needs the idle task's accounted run time (which excludes worker
 * spin but includes the tick ISR, removed via g_cpu_isr_accum). The run-time
 * clock is the same DWT CYCCNT. */
#define configGENERATE_RUN_TIME_STATS           1
#define configUSE_TRACE_FACILITY                1
#define INCLUDE_uxTaskGetSystemState            1
#define INCLUDE_xTaskGetIdleTaskHandle          1
#define configRUN_TIME_COUNTER_TYPE             uint32_t
#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS() do { } while (0)
#define portGET_RUN_TIME_COUNTER_VALUE() \
    (*(volatile uint32_t *)0xE0001004u)
#define configUSE_MALLOC_FAILED_HOOK            1
#define configUSE_APPLICATION_TASK_TAG          0
#define configNUM_THREAD_LOCAL_STORAGE_POINTERS 0

/* Memory allocation: heap_4 (coalescing free list). */
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configSUPPORT_STATIC_ALLOCATION         0

/* Interrupt priorities (NVIC 4 bits). */
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY          (15U)
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY     (5U)
#define configKERNEL_INTERRUPT_PRIORITY                  (15U << 4)
#define configMAX_SYSCALL_INTERRUPT_PRIORITY             (5U << 4)

/* Port-specific. */
#define configUSE_SYSTICK                   1
#define configUSE_NEWLIB_REENTRANT          0
/* Indirect routing: stm32h7xx_it.c defines SVC_Handler/PendSV_Handler/SysTick_Handler
 * that forward to the FreeRTOS port handlers, so the vector-table slots hold the
 * wrapper addresses, not vPortSVCHandler/xPortPendSVHandler directly. Disable the
 * direct-installation validation that would otherwise assert on them. */
#define configCHECK_HANDLER_INSTALLATION    0

/* Optional: trace/metrics. Case 23 needs the idle task run-time counter. */
#define configGENERATE_RUN_TIME_STATS       1
#define configUSE_STATS_FORMATTING_FUNCTIONS 0

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
