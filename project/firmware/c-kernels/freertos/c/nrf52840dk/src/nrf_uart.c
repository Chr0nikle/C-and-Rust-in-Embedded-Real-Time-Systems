/* Polling UARTE0 console for the nRF52840 DK.
 *
 * The UARTE has no data register: RX goes to RAM through EasyDMA. We keep a
 * one-byte RX window armed, copy each completed byte into a ring and re-arm.
 * (On this part the receiver must stay armed; STOPRX/RXTO are unreliable.)
 * TX is chunked into <=255-byte EasyDMA transfers. Bytes/DWT are at 64 MHz. */
#include "nrf_uart.h"

#define UARTE_BASE 0x40002000UL
#define UREG(off) (*(volatile uint32_t *)(UARTE_BASE + (off)))

#define TASKS_STARTRX 0x000UL
#define TASKS_STARTTX 0x008UL
#define EVENTS_ENDRX 0x110UL
#define EVENTS_ENDTX 0x120UL
#define EVENTS_ERROR 0x124UL
#define EVENTS_RXTO 0x144UL
#define ENABLE 0x500UL
#define PSELRTS 0x508UL
#define PSELTXD 0x50CUL
#define PSELCTS 0x510UL
#define PSELRXD 0x514UL
#define BAUDRATE 0x524UL
#define RXD_PTR 0x534UL
#define RXD_MAXCNT 0x538UL
#define TXD_PTR 0x544UL
#define TXD_MAXCNT 0x548UL
#define ERRORSRC 0x480UL

#define DMA_DISCONNECTED 0xFFFFFFFFUL
/* 115200 baud at the 64 MHz peripheral clock. */
#define BAUDRATE_115200 0x01D7E000UL

#define DWT_CTRL (*(volatile uint32_t *)0xE0001000UL)
#define DWT_CYCCNT (*(volatile uint32_t *)0xE0001004UL)
#define DEMCR (*(volatile uint32_t *)0xE000EDFCUL)

#define CYCLES_PER_MS 64000UL

/* Re-assert the trace/cycle-counter enable before every CYCCNT read: on the
 * nRF52840 the enable is not sticky (see bench.c dwt_cycles). */
static inline uint32_t uart_dwt_cycles(void) {
    DEMCR |= (1UL << 24);
    DWT_CTRL |= 1UL;
    return DWT_CYCCNT;
}

#define RX_RING 512
static uint8_t s_ring[RX_RING];
static volatile uint16_t s_head;
static volatile uint16_t s_tail;
static uint8_t s_rx_byte;

void nrf_uart_poll(void) {
    if (UREG(EVENTS_ENDRX) != 0UL) {
        UREG(EVENTS_ENDRX) = 0UL;
        uint16_t next = (uint16_t)((s_head + 1U) % RX_RING);
        if (next != s_tail) {
            s_ring[s_head] = s_rx_byte;
            s_head = next;
        }
        /* Re-arm the one-byte window. */
        UREG(RXD_PTR) = (uint32_t)(uintptr_t)&s_rx_byte;
        UREG(RXD_MAXCNT) = 1UL;
        UREG(TASKS_STARTRX) = 1UL;
    }
    if (UREG(EVENTS_ERROR) != 0UL) {
        UREG(EVENTS_ERROR) = 0UL;
        UREG(ERRORSRC) = UREG(ERRORSRC);
        /* Restart RX in case an error stopped the transfer. */
        UREG(RXD_PTR) = (uint32_t)(uintptr_t)&s_rx_byte;
        UREG(RXD_MAXCNT) = 1UL;
        UREG(TASKS_STARTRX) = 1UL;
    }
}

void nrf_uart_init(void) {
    DEMCR |= (1UL << 24);
    DWT_CYCCNT = 0UL;
    DWT_CTRL |= 1UL;

    UREG(PSELRTS) = DMA_DISCONNECTED;
    UREG(PSELCTS) = DMA_DISCONNECTED;
    UREG(PSELTXD) = 6UL; /* P0.06 */
    UREG(PSELRXD) = 8UL; /* P0.08 */
    UREG(BAUDRATE) = BAUDRATE_115200;
    UREG(ENABLE) = 8UL; /* ENABLE::ON */

    UREG(EVENTS_ENDRX) = 0UL;
    UREG(EVENTS_ENDTX) = 0UL;
    UREG(EVENTS_ERROR) = 0UL;
    UREG(EVENTS_RXTO) = 0UL;
    UREG(ERRORSRC) = UREG(ERRORSRC);

    s_head = 0U;
    s_tail = 0U;
    UREG(RXD_PTR) = (uint32_t)(uintptr_t)&s_rx_byte;
    UREG(RXD_MAXCNT) = 1UL;
    UREG(TASKS_STARTRX) = 1UL;
}

void nrf_uart_tx(const uint8_t *buf, size_t len) {
    while (len > 0U) {
        uint32_t chunk = (len > 255U) ? 255U : (uint32_t)len;
        UREG(EVENTS_ENDTX) = 0UL;
        UREG(TXD_PTR) = (uint32_t)(uintptr_t)buf;
        UREG(TXD_MAXCNT) = chunk;
        UREG(TASKS_STARTTX) = 1UL;

        uint32_t start = uart_dwt_cycles();
        while (UREG(EVENTS_ENDTX) == 0UL) {
            if ((uart_dwt_cycles() - start) > (CYCLES_PER_MS * 2000UL)) {
                break; /* guard: never block a run indefinitely */
            }
        }
        UREG(EVENTS_ENDTX) = 0UL;
        buf += chunk;
        len -= chunk;
    }
}

int nrf_uart_getc_timeout(uint32_t timeout_ms) {
    uint32_t budget = timeout_ms * CYCLES_PER_MS;
    uint32_t start = uart_dwt_cycles();
    for (;;) {
        nrf_uart_poll();
        if (s_tail != s_head) {
            uint8_t b = s_ring[s_tail];
            s_tail = (uint16_t)((s_tail + 1U) % RX_RING);
            return (int)b;
        }
        if ((uart_dwt_cycles() - start) >= budget) {
            return -1;
        }
    }
}
