#ifndef NRF_UART_H
#define NRF_UART_H

#include <stddef.h>
#include <stdint.h>

/* UARTE0 on the nRF52840 DK is wired to the J-Link VCOM (P0.06 TX / P0.08 RX)
 * at 115200 8N1. Polling EasyDMA (one-byte RX windows, chunked TX) keeps the
 * console independent of the FreeRTOS tick and of the benchmark workloads. */
void nrf_uart_init(void);

/* Blocking transmit; splits into <=255-byte EasyDMA chunks. */
void nrf_uart_tx(const uint8_t *buf, size_t len);

/* Receive one byte, waiting up to timeout_ms; returns -1 on timeout. Calls
 * nrf_uart_poll() internally, draining every completed one-byte window. */
int nrf_uart_getc_timeout(uint32_t timeout_ms);

/* Pump the one-byte RX window; call from the idle hook or before waiting. */
void nrf_uart_poll(void);

#endif /* NRF_UART_H */
