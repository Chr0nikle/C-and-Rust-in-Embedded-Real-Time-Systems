/* FreeRTOS + STM32 HAL smoke firmware for the stm32h723 bench.
 *
 * Implements UART protocol v1 (UART protocol v1) over USART1
 * (PA2/PA3, 115200 8N1):
 *   - sends the unsolicited `ready` frame at boot
 *   - answers `hello` while idle with `ready` plus the challenge echo
 *   - runs `start` (validated) as the full benchmark path: started ->
 *     measurement per sample -> complete (see bench.c)
 *   - logs `cancel` only (best effort, no frame)
 *
 * The benchmark adapter and sink live in bench.c and drive the shared
 * bench_run_suite from firmware/common/c.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#include "FreeRTOS.h"
#include "task.h"
#include "stm32h7xx_hal.h"
#include "bench.h"

#define PROTO_BOARD       "stm32h723"
#define PROTO_RTOS        "freertos"
#ifndef PROTO_KERNEL
#define PROTO_KERNEL      "freertos-kernel-v11.3.1"
#endif
#define PROTO_LANGUAGE    "c"
#ifndef PROTO_APPLICATION
#define PROTO_APPLICATION "freertos-stm32h723-bench"
#endif
#define PROTO_PROTOCOL    1

#define USART_BAUDRATE  115200U

#define FRAME_MAX       1024
/* Incoming host frames are small (hello/start/cancel); 16 bytes below FRAME_MAX
 * frees RAM for the case-28 arena without touching the TX buffer size. */
#define LINE_BUF_SIZE   (FRAME_MAX - 16)
#define SESSION_STACK   4096
#define SESSION_PRIO    5
#define RX_POLL_MS      100

#define MAX_CELLS    32
/* Largest suite advertised by any board (reserved protocol IDs 0-28). */
#define MAX_CASES    29
#define MAX_TYPE_LEN 16
#define MAX_STR_LEN  64

/* ------------------------------------------------------------------ TX */

/* USART1 (PA9/PA10) on APB2, 115200 8N1, via the HAL. Mirrors the known-good
 * blink UART config (AF7, very-high speed, pull-up, 16x oversampling). */
static UART_HandleTypeDef huart1;

static void uart1_init(void)
{
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_USART1_CLK_ENABLE();

    GPIO_InitTypeDef gpio = {0};
    gpio.Pin       = GPIO_PIN_9 | GPIO_PIN_10;
    gpio.Mode      = GPIO_MODE_AF_PP;
    gpio.Pull      = GPIO_PULLUP;
    gpio.Speed     = GPIO_SPEED_FREQ_VERY_HIGH;
    gpio.Alternate = GPIO_AF7_USART1;
    HAL_GPIO_Init(GPIOA, &gpio);

    huart1.Instance          = USART1;
    huart1.Init.BaudRate     = USART_BAUDRATE;
    huart1.Init.WordLength   = UART_WORDLENGTH_8B;
    huart1.Init.StopBits     = UART_STOPBITS_1;
    huart1.Init.Parity       = UART_PARITY_NONE;
    huart1.Init.Mode         = UART_MODE_TX_RX;
    huart1.Init.HwFlowCtl    = UART_HWCONTROL_NONE;
    huart1.Init.OverSampling = UART_OVERSAMPLING_16;
    (void)HAL_UART_Init(&huart1);
}

static char s_tx_buf[FRAME_MAX + 2];

#define TX_TIMEOUT_MS 2000

static void tx_string(const char *buf, size_t cap, size_t len)
{
    if (len + 1 > cap) {
        return;
    }
    /* Append the newline into the buffer tail, then transmit len+1 bytes.
     * s_tx_buf is FRAME_MAX+2 so the trailing '\n' has room. */
    ((char *)buf)[len] = '\n';
    (void)HAL_UART_Transmit(&huart1, (const uint8_t *)buf, len + 1,
                            TX_TIMEOUT_MS);
}

/* Exposed to bench.c: transmit a protocol frame that already includes its
 * trailing newline (bench.c builds `len + 1` bytes with the newline baked in).
 * The buffer must have room for len bytes; bench.c passes a frame of at most
 * FRAME_MAX plus the newline, which fits s_tx_buf's FRAME_MAX+2. */
void bench_tx(const char *buf, size_t len)
{
    if (len > sizeof(s_tx_buf)) {
        return;
    }
    (void)HAL_UART_Transmit(&huart1, (const uint8_t *)buf, len, TX_TIMEOUT_MS);
}

static size_t append_str(char *dst, size_t cap, size_t len,
                         const char *src, size_t src_len)
{
    if (len + src_len + 1 > cap) {
        return cap + 1; /* overflow sentinel */
    }
    memcpy(dst + len, src, src_len);
    return len + src_len;
}

static void send_ready(const char *challenge)
{
    char *p = s_tx_buf;
    size_t cap = sizeof(s_tx_buf);
    size_t len = 0;

    len = append_str(p, cap, len, "{\"protocol\":1,\"type\":\"ready\",",
                     sizeof("{\"protocol\":1,\"type\":\"ready\",") - 1);
    len = append_str(p, cap, len, "\"board\":\"stm32h723\",",
                     sizeof("\"board\":\"stm32h723\",") - 1);
    len = append_str(p, cap, len, "\"rtos\":\"freertos\",",
                     sizeof("\"rtos\":\"freertos\",") - 1);
    len = append_str(p, cap, len, "\"kernel\":\"freertos-kernel-v11.3.1\",",
                     sizeof("\"kernel\":\"freertos-kernel-v11.3.1\",") - 1);
    len = append_str(p, cap, len, "\"language\":\"c\",",
                     sizeof("\"language\":\"c\",") - 1);
    len = append_str(p, cap, len, "\"build_id\":\"",
                     sizeof("\"build_id\":\"") - 1);
    len = append_str(p, cap, len, BENCH_BUILD_ID,
                     strlen(BENCH_BUILD_ID));
    len = append_str(p, cap, len, "\",\"application\":\"",
                     sizeof("\",\"application\":\"") - 1);
    len = append_str(p, cap, len, PROTO_APPLICATION,
                     strlen(PROTO_APPLICATION));
    /* Case 11 (periodic_jitter_loaded) is implemented for both cores. Cases
     * 12-15 (semaphore_latency, mutex_uncontended, alloc_latency,
     * sleep_accuracy) share the bench_sem_/mutex_u_/alloc_/sleep_* shims, so
     * the Rust core advertises the full suite too. Case 17
     * (notification_latency) and phase-2 cases 16/17/21/25/26 are implemented
     * on both cores: the Rust adapter calls the same bench_sched_/notify_/prio_/
     * tick_/maxdis_* shims. */
#ifdef BENCH_LANGUAGE_RUST
    len = append_str(p, cap, len, "\",\"cases\":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28]",
                     sizeof("\",\"cases\":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28]") - 1);
#else
    len = append_str(p, cap, len, "\",\"cases\":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28]",
                     sizeof("\",\"cases\":[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28]") - 1);
#endif
    if (len > cap) {
        return;
    }
    if (challenge != NULL) {
        len = append_str(p, cap, len, ",\"challenge\":\"",
                         sizeof(",\"challenge\":\"") - 1);
        if (len > cap) {
            return;
        }
        len = append_str(p, cap, len, challenge, strlen(challenge));
        if (len > cap) {
            return;
        }
        len = append_str(p, cap, len, "\"", 1);
        if (len > cap) {
            return;
        }
    }
    p[len] = '}';
    tx_string(p, cap, len + 1);
}

static void send_error(const char *code, const char *message, const char *run_id)
{
    char *p = s_tx_buf;
    size_t cap = sizeof(s_tx_buf);
    size_t len = 0;

    len = append_str(p, cap, len, "{\"protocol\":1,\"type\":\"error\",",
                     sizeof("{\"protocol\":1,\"type\":\"error\",") - 1);
    if (len > cap) {
        return;
    }
    if (run_id != NULL) {
        len = append_str(p, cap, len, "\"run_id\":\"",
                         sizeof("\"run_id\":\"") - 1);
        if (len > cap) {
            return;
        }
        len = append_str(p, cap, len, run_id, strlen(run_id));
        if (len > cap) {
            return;
        }
        len = append_str(p, cap, len, "\",", 2);
        if (len > cap) {
            return;
        }
    }
    len = append_str(p, cap, len, "\"code\":\"",
                     sizeof("\"code\":\"") - 1);
    if (len > cap) {
        return;
    }
    len = append_str(p, cap, len, code, strlen(code));
    if (len > cap) {
        return;
    }
    len = append_str(p, cap, len, "\",\"message\":\"",
                     sizeof("\",\"message\":\"") - 1);
    if (len > cap) {
        return;
    }
    len = append_str(p, cap, len, message, strlen(message));
    if (len > cap) {
        return;
    }
    len = append_str(p, cap, len, "\"}", 2);
    if (len > cap) {
        return;
    }
    tx_string(p, cap, len);
}

/* ------------------------------------------------------------ decoding */

enum {
    CELL_NONE = 0,
    CELL_INT,
    CELL_STR,
    CELL_ARR
};

typedef struct {
    int kind;
    char name[12];
    uint64_t num;
    char str[MAX_STR_LEN + 1];
    uint32_t cells[MAX_CASES];
    size_t n_cells;
} frame_cell_t;

typedef struct {
    frame_cell_t cells[MAX_CELLS];
    size_t n_cells;
    int dup;
    int over;
} frame_t;

static int cell_index(const frame_t *f, const char *name)
{
    for (size_t i = 0; i < f->n_cells; i++) {
        if (strcmp(f->cells[i].name, name) == 0) {
            return (int)i;
        }
    }
    return -1;
}

static frame_cell_t *add_cell(frame_t *f, const char *name)
{
    if (cell_index(f, name) >= 0) {
        f->dup = 1;
        return NULL;
    }
    if (f->n_cells >= MAX_CELLS) {
        f->over = 1;
        return NULL;
    }
    frame_cell_t *c = &f->cells[f->n_cells++];
    memset(c, 0, sizeof(*c));
    strncpy(c->name, name, sizeof(c->name) - 1);
    c->name[sizeof(c->name) - 1] = '\0';
    return c;
}

static int parse_u64(const char **p, uint64_t *out)
{
    const char *s = *p;
    if (*s < '0' || *s > '9') {
        return -1;
    }
    uint64_t v = 0;
    while (*s >= '0' && *s <= '9') {
        if (v > (UINT64_MAX - (uint64_t)(*s - '0')) / 10) {
            return -1;
        }
        v = v * 10 + (uint64_t)(*s - '0');
        s++;
    }
    *out = v;
    *p = s;
    return 0;
}

static int parse_str(const char **p, char *dst, size_t cap)
{
    const char *s = *p;
    size_t n = 0;
    for (;;) {
        char c = *s;
        if (c == '"') {
            break;
        }
        if (c == '\0' || c < 0x20 || c > 0x7E) {
            return -1;
        }
        if (n + 1 >= cap) {
            return -1;
        }
        dst[n++] = c;
        s++;
    }
    dst[n] = '\0';
    *p = s + 1;
    return 0;
}

static int skip_ws(const char **p)
{
    while (**p == ' ' || **p == '\t' || **p == '\r' || **p == '\n') {
        (*p)++;
    }
    return 0;
}

static int parse_value(const char **p, frame_cell_t *c)
{
    if (skip_ws(p) != 0) {
        return -1;
    }
    char ch = **p;
    if (ch == '{' || ch == '-' ||
        (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z')) {
        return -1;
    }
    if (ch == '"') {
        c->kind = CELL_STR;
        (*p)++;
        return parse_str(p, c->str, sizeof(c->str));
    }
    if (ch == '[') {
        c->kind = CELL_ARR;
        const char *s = *p + 1;
        if (skip_ws(&s) != 0) {
            return -1;
        }
        if (*s == ']') {
            *p = s + 1;
            return 0;
        }
        for (;;) {
            uint64_t v;
            if (parse_u64(&s, &v) != 0 || v > UINT32_MAX) {
                return -1;
            }
            if (c->n_cells >= MAX_CASES) {
                return -1;
            }
            c->cells[c->n_cells++] = (uint32_t)v;
            if (skip_ws(&s) != 0) {
                return -1;
            }
            if (*s == ',') {
                s++;
                if (skip_ws(&s) != 0) {
                    return -1;
                }
                continue;
            }
            if (*s == ']') {
                *p = s + 1;
                return 0;
            }
            return -1;
        }
    }
    c->kind = CELL_INT;
    return parse_u64(p, &c->num);
}

static int parse_object(const char *line, size_t len, frame_t *f)
{
    const char *p = line;
    const char *end = line + len;

    if (p >= end || *p != '{') {
        return -1;
    }
    p++;
    for (;;) {
        if (p >= end) {
            return -1;
        }
        if (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
            p++;
            continue;
        }
        if (*p == '}') {
            return 0;
        }
        if (*p != '"') {
            return -1;
        }
        p++;
        char key[16];
        if (parse_str(&p, key, sizeof(key)) != 0) {
            return -1;
        }
        if (p >= end || *p != ':') {
            return -1;
        }
        p++;
        frame_cell_t *c = add_cell(f, key);
        if (c == NULL) {
            return -1;
        }
        if (parse_value(&p, c) != 0 || p >= end) {
            return -1;
        }
        if (*p != ',' && *p != '}') {
            return -1;
        }
        if (*p == ',') {
            p++;
        }
    }
}

static int line_is_object_candidate(const char *line, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        char c = line[i];
        if (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
            continue;
        }
        return c == '{';
    }
    return 0;
}

static const char *cell_str(const frame_t *f, const char *name)
{
    int i = cell_index(f, name);
    if (i < 0 || f->cells[i].kind != CELL_STR) {
        return NULL;
    }
    return f->cells[i].str;
}

static const frame_cell_t *cell_get(const frame_t *f, const char *name)
{
    int i = cell_index(f, name);
    if (i < 0) {
        return NULL;
    }
    return &f->cells[i];
}

static frame_t s_frame;

/* Protocol v1 (protocol.md:20): a repeated start while a benchmark is
 * running is an error, never a second concurrent run. The session task is the
 * only caller of handle_line, so a simple flag is sufficient. */
static int s_bench_busy = 0;

static void handle_line(const char *line, size_t len)
{
    if (!line_is_object_candidate(line, len)) {
        return;
    }
    frame_t *f = &s_frame;
    memset(f, 0, sizeof(*f));
    if (parse_object(line, len, f) != 0 || f->dup || f->over) {
        send_error("bad_json", "malformed protocol frame", NULL);
        return;
    }
    const frame_cell_t *proto = cell_get(f, "protocol");
    if (proto == NULL || proto->kind != CELL_INT || proto->num != 1) {
        return;
    }
    const char *type = cell_str(f, "type");
    if (type == NULL ||
        (strcmp(type, "hello") != 0 && strcmp(type, "start") != 0 &&
         strcmp(type, "cancel") != 0)) {
        send_error("unknown_type", "type not recognized by device", NULL);
        return;
    }
    if (strcmp(type, "hello") == 0) {
        const char *challenge = cell_str(f, "challenge");
        if (challenge == NULL || challenge[0] == '\0' ||
            strlen(challenge) > MAX_STR_LEN) {
            send_error("bad_challenge", "challenge must be 1-64 printable",
                       NULL);
            return;
        }
        send_ready(challenge);
        return;
    }
    if (strcmp(type, "start") == 0) {
        /* A repeated start while a benchmark is running is an error, never a
         * second concurrent run (protocol.md:20). */
        if (s_bench_busy) {
            send_error("busy", "a benchmark is already running", NULL);
            return;
        }
        const char *run_id = cell_str(f, "run_id");
        const char *build_id = cell_str(f, "build_id");
        const frame_cell_t *cases = cell_get(f, "cases");
        const frame_cell_t *reps = cell_get(f, "repetitions");
        const frame_cell_t *warmup = cell_get(f, "warmup");

        if (run_id == NULL || build_id == NULL || cases == NULL ||
            reps == NULL || warmup == NULL) {
            send_error("missing_fields",
                       "start requires run_id, build_id, cases, "
                       "repetitions, warmup",
                       run_id);
            return;
        }
        if (cases->kind != CELL_ARR || cases->n_cells == 0 ||
            cases->n_cells > MAX_CASES) {
            send_error("bad_cases", "cases must be a non-empty array", run_id);
            return;
        }
        if (reps->kind != CELL_INT || reps->num == 0 ||
            reps->num > UINT32_MAX) {
            send_error("bad_repetitions", "repetitions must be a positive int",
                       run_id);
            return;
        }
        if (warmup->kind != CELL_INT || warmup->num > UINT32_MAX) {
            send_error("bad_warmup", "warmup must be a non-negative int",
                       run_id);
            return;
        }

        uint32_t reps32 = (uint32_t)reps->num;
        uint32_t warm32 = (uint32_t)warmup->num;

        /* Validate before acknowledging (protocol.md:20). */
        const char *err_code = NULL;
        const char *err_msg = NULL;
        if (bench_validate_start(run_id, build_id, cases->cells,
                                 cases->n_cells, reps32, warm32,
                                 &err_code, &err_msg) != 0) {
            send_error(err_code, err_msg, run_id);
            return;
        }

        /* Validation passed: run the suite. bench_run emits started,
         * per-sample measurements (flushed between cases), and complete.
         * The run is synchronous within the session task; mark busy for the
         * duration so a concurrent start is rejected. */
        s_bench_busy = 1;
        int rc = bench_run(run_id, build_id, cases->cells, cases->n_cells,
                           reps32, warm32);
        s_bench_busy = 0;
        if (rc != 0) {
            /* bench_run already emitted an error frame on a suite failure;
             * a frame-builder overflow here is unrecoverable but should not
             * leave the device in a worse state than a reset. */
            return;
        }
        return;
    }
    /* cancel: best effort, log only, no frame. */
    (void)cell_str(f, "run_id");
}

/* ------------------------------------------------------------- session */

static char s_line[LINE_BUF_SIZE];
static size_t s_line_len = 0;
static int s_line_over = 0;

static void feed_byte(uint8_t b)
{
    if (b == '\n' || b == '\r' || b == 0) {
        if (s_line_len > 0 || s_line_over) {
            if (!s_line_over) {
                s_line[s_line_len] = '\0';
                handle_line(s_line, s_line_len);
            }
            s_line_len = 0;
            s_line_over = 0;
        }
        return;
    }
    if (s_line_len + 1 < sizeof(s_line)) {
        s_line[s_line_len++] = (char)b;
    } else {
        s_line_over = 1;
    }
}

static void session_task(void *arg)
{
    (void)arg;
    uint8_t b;
    for (;;) {
        if (HAL_UART_Receive(&huart1, &b, 1, RX_POLL_MS) == HAL_OK) {
            feed_byte(b);
        }
    }
}

/* ---------------------------------------------------- safety hooks */

static void fault_latch(uint32_t code, uint32_t value)
{
    /* Park the offending value in a volatile so a debugger can read it after
     * the break. Latch the fault reason in R0/R1 then break. */
    volatile uint32_t latched_code = code;
    volatile uint32_t latched_value = value;
    (void)latched_code;
    (void)latched_value;
    __BKPT(0);
    for (;;) {
    }
}

void vAssertCalled(const char *file, int line)
{
    (void)file;
    (void)line;
    __BKPT(0);
    for (;;) {
    }
}

extern volatile int g_frag_probe_active;
void vApplicationMallocFailedHook(void)
{
    if (g_frag_probe_active) {
        return; /* case 24 intentionally probes sizes that must fail */
    }
    fault_latch(0xA110C, 0);
}

void vApplicationStackOverflowHook(TaskHandle_t xTask, char *pcTaskName)
{
    (void)pcTaskName;
    fault_latch(0xA110D, (uint32_t)(uintptr_t)xTask);
}

/* ---------------------------------------------------------- clock init */

/* Configure HSE (25 MHz crystal) -> PLL1 -> 520 MHz SYSCLK (VOS0). The Cube H7
 * template SystemInit() does not configure a PLL, so SystemCoreClock would
 * otherwise stay on HSI and the USART1 baud divisor would be wrong. The
 * FK723M1-ZGT6 board HSE is 25 MHz.
 *   HSE 25 MHz / PLLM 5 = 5 MHz
 *   5 MHz * PLLN 104     = 520 MHz VCO
 *   520 MHz / PLLP 1     = 520 MHz SYSCLK   (DIVP1=1 is valid on H72x/73x)
 *   AHB /2 = 260 MHz (HCLK), APB1 /4 = 65 MHz, APB2 /4 = 65 MHz (PCLK)
 * 520 MHz matches Zephyr/NuttX/FreeRTOS-in-Rust on this board, so all four H7
 * paths run at the same clock (the stm32h7xx-hal VOS0 maximum for H72x/73x).
 * VOS0 + HCLK 260 needs 3 flash wait states (CubeH7 stm32h7xx_ll_utils.c). */
static void rcc_520mhz(void)
{
    RCC_OscInitTypeDef osc = {0};
    RCC_ClkInitTypeDef clk = {0};

    /* VOS0 (high-performance scaling), as in the CubeH7 NUCLEO-H723ZG reference. */
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE0);
    while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {
    }

    osc.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    osc.HSEState       = RCC_HSE_ON;          /* 25 MHz crystal */
    osc.HSIState       = RCC_HSI_OFF;
    osc.CSIState       = RCC_CSI_OFF;
    osc.PLL.PLLState   = RCC_PLL_ON;
    osc.PLL.PLLSource  = RCC_PLLSOURCE_HSE;
    osc.PLL.PLLM       = 5;
    osc.PLL.PLLN       = 104;
    osc.PLL.PLLFRACN   = 0;
    osc.PLL.PLLP       = 1;
    osc.PLL.PLLR       = 2;
    osc.PLL.PLLQ       = 4;
    osc.PLL.PLLVCOSEL  = RCC_PLL1VCOWIDE;
    osc.PLL.PLLRGE     = RCC_PLL1VCIRANGE_2;  /* 4 - 8 MHz input */
    if (HAL_RCC_OscConfig(&osc) != HAL_OK) {
        for (;;) {
        }
    }

    clk.ClockType = RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK |
                    RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_PCLK1 |
                    RCC_CLOCKTYPE_PCLK2 | RCC_CLOCKTYPE_D3PCLK1;
    clk.SYSCLKSource   = RCC_SYSCLKSOURCE_PLLCLK;
    clk.SYSCLKDivider  = RCC_SYSCLK_DIV1;
    clk.AHBCLKDivider  = RCC_HCLK_DIV2;   /* HCLK  = 260 MHz */
    clk.APB3CLKDivider = RCC_APB3_DIV4;   /* PCLK3 = 65 MHz */
    clk.APB1CLKDivider = RCC_APB1_DIV4;   /* PCLK1 = 65 MHz */
    clk.APB2CLKDivider = RCC_APB2_DIV4;   /* PCLK2 = 65 MHz */
    clk.APB4CLKDivider = RCC_APB4_DIV4;   /* PCLK4 = 65 MHz */
    if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_3) != HAL_OK) {
        for (;;) {
        }
    }
}

/* -------------------------------------------------------------- boot */

/* Stop the SysTick timer AND its NVIC IRQ line. HAL_Init() enables the
 * SysTick and wires HAL's tick handler; the FreeRTOS port reconfigures and
 * re-enables SysTick only inside vTaskStartScheduler(). Until the scheduler
 * is running, the kernel's task lists are only partially initialized
 * (populated during the first xTaskCreate), so any pre-scheduler tick entering
 * xPortSysTickHandler() can dereference a zeroed delayed-list link and fault.
 * Clear both the ENABLE bit and the NVIC IRQ so no tick can fire pre-scheduler.
 */
static void systick_mask(void)
{
    SysTick->CTRL = 0UL;          /* clear ENABLE + TICKINT + CLKSOURCE */
    NVIC_ClearPendingIRQ(SysTick_IRQn);
    NVIC_DisableIRQ(SysTick_IRQn);
    __DSB();
    __ISB();
}

/* Boot sentinel for debug inspection: 0=enter, 1=after HAL_Init+mask,
 * 2=after rcc, 3=after uart1_init, 4=after send_ready, 5=task created. */
volatile uint32_t __attribute__((used)) g_boot_sentinel;

int main(void)
{
    g_boot_sentinel = 1;
    HAL_Init();

    rcc_520mhz();          /* HAL_RCC_ClockConfig re-enables SysTick via HAL_InitTick */
    SystemCoreClockUpdate();
    systick_mask();        /* mask AFTER the clock config so the mask sticks */
    g_boot_sentinel = 2;

    bench_init();          /* enable DWT CYCCNT now that the core clock is 520 MHz */

    /* Enable the CCM data RAM clock: the benchmark sample sink lives in
     * CCMRAM (see bench.c and the .ccmram linker section). Its reset value is
     * normally already set, but make it explicit before the sink is used. */

    uart1_init();
    g_boot_sentinel = 3;

    send_ready(NULL);
    g_boot_sentinel = 4;

    BaseType_t ok = xTaskCreate(session_task, "proto_ses", SESSION_STACK,
                                  NULL, SESSION_PRIO, NULL);
    if (ok != pdPASS) {
        for (;;) {
        }
    }
    /* Re-enable the SysTick NVIC IRQ line for the FreeRTOS port. The port's
     * vPortSetupTimerInterrupt() reconfigures the CTRL register (including
     * TICKINT + ENABLE) but assumes the NVIC IRQ line is already enabled. */
    NVIC_EnableIRQ(SysTick_IRQn);
    vTaskStartScheduler();
    for (;;) {
    }
}
