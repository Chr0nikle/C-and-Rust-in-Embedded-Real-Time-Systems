/* FreeRTOS + ESP-IDF firmware for the esp32c6 bench.
 *
 * Implements UART protocol v1 (UART protocol v1) over the
 * USB-Serial-JTAG port:
 *   - sends the unsolicited `ready` frame at boot
 *   - answers `hello` while idle with `ready` plus the challenge echo
 *   - runs the benchmark suite on `start` (validate -> bench_run_suite ->
 *     started / measurement* / complete, or error on failure)
 *   - logs `cancel` only (best effort, no frame)
 *
 * The benchmark adapter lives in bench.c (esp_timer + FreeRTOS queue port,
 * bounded sample sink) and mirrors the verified STM32F4 adapter.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/usb_serial_jtag.h"

#include "bench.h"

#ifndef BENCH_BUILD_ID
#define BENCH_BUILD_ID "freertos-c6-bench-nogit"
#endif

#define PROTO_BOARD       "esp32c6"
#define PROTO_RTOS        "freertos"
#define PROTO_KERNEL      "esp-idf-5.5.5"
#ifdef BENCH_LANGUAGE_RUST
#define PROTO_LANGUAGE    "rust"
#define PROTO_APPLICATION "freertos-c6-rust"
/* Both languages share the C task paths, including cases 12-15. */
#define BENCH_CASE_LIST   "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,25,26,27,28]"
#else
#define PROTO_LANGUAGE    "c"
#define PROTO_APPLICATION "freertos-c6-bench"
#define BENCH_CASE_LIST   "[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,25,26,27,28]"
#endif
#define PROTO_PROTOCOL    1

#define TAG "proto_smoke"

#define TX_BUF_SIZE   4096
#define RX_BUF_SIZE   1024
#define FRAME_MAX     1024
#define LINE_BUF_SIZE (FRAME_MAX + 8)
#define SESSION_STACK 4096
#define SESSION_PRIO  5
#define RX_POLL_MS    100
#define TX_TIMEOUT_MS 2000

#define MAX_CELLS    32
/* Largest suite advertised by any board (reserved protocol IDs 0-27). */
#define MAX_CASES    27
#define MAX_TYPE_LEN 16
#define MAX_STR_LEN  64

/* ------------------------------------------------------------------ TX */

static char s_tx_buf[FRAME_MAX + 2];

static esp_err_t tx_string(char *buf, size_t cap, size_t len)
{
    if (len + 1 > cap) {
        return ESP_ERR_INVALID_SIZE;
    }
    buf[len] = '\n';
    int written = usb_serial_jtag_write_bytes(buf, len + 1,
                                              pdMS_TO_TICKS(TX_TIMEOUT_MS));
    if (written != (int)(len + 1)) {
        ESP_LOGE(TAG, "tx short: %d/%u", written, (unsigned)(len + 1));
        return ESP_ERR_TIMEOUT;
    }
    return usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(TX_TIMEOUT_MS));
}

/* Exposed to bench.c: transmit exactly `len` bytes (the buffer already
 * includes the trailing newline). */
void bench_tx(const char *buf, size_t len)
{
    if (len > FRAME_MAX + 1) {
        ESP_LOGE(TAG, "bench_tx overflow: %u bytes", (unsigned)len);
        return;
    }
    int written = usb_serial_jtag_write_bytes((void *)buf, len,
                                              pdMS_TO_TICKS(TX_TIMEOUT_MS));
    if (written != (int)len) {
        ESP_LOGE(TAG, "bench_tx short: %d/%u", written, (unsigned)len);
        return;
    }
    (void)usb_serial_jtag_wait_tx_done(pdMS_TO_TICKS(TX_TIMEOUT_MS));
}

static void send_ready(const char *challenge)
{
    /* Fixed fields are literal identifiers and the challenge is validated
     * printable ASCII without quote or backslash, so no JSON escaping is
     * required in this builder. */
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"ready\",\"board\":\"%s\","
                     "\"rtos\":\"%s\",\"kernel\":\"%s\",\"language\":\"%s\","
                     "\"build_id\":\"%s\",\"application\":\"%s\","
                     "\"cases\":" BENCH_CASE_LIST,
                     PROTO_PROTOCOL, PROTO_BOARD, PROTO_RTOS, PROTO_KERNEL,
                     PROTO_LANGUAGE, BENCH_BUILD_ID, PROTO_APPLICATION);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        ESP_LOGE(TAG, "ready frame overflow");
        return;
    }
    if (challenge != NULL) {
        n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                      ",\"challenge\":\"%s\"", challenge);
    }
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        ESP_LOGE(TAG, "ready frame overflow (challenge)");
        return;
    }
    s_tx_buf[n] = '}';
    (void)tx_string(s_tx_buf, sizeof(s_tx_buf), (size_t)(n + 1));
}

static void send_error(const char *code, const char *message,
                       const char *run_id)
{
    int n = snprintf(s_tx_buf, sizeof(s_tx_buf),
                     "{\"protocol\":%d,\"type\":\"error\",",
                     PROTO_PROTOCOL);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        return;
    }
    if (run_id != NULL) {
        n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                      "\"run_id\":\"%s\",", run_id);
    }
    n += snprintf(s_tx_buf + n, sizeof(s_tx_buf) - (size_t)n,
                  "\"code\":\"%s\",\"message\":\"%s\"}", code, message);
    if (n < 0 || (size_t)n >= sizeof(s_tx_buf)) {
        ESP_LOGE(TAG, "error frame overflow");
        return;
    }
    (void)tx_string(s_tx_buf, sizeof(s_tx_buf), (size_t)n);
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

/* v1 strings: printable ASCII, no escapes. */
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

/* Only lines whose first non-whitespace byte is '{' are protocol candidates;
 * everything else (boot text, logs) stays a log line and is ignored. */
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

/* Single-task, non-reentrant: a file-scope frame keeps the ~4 KB parse
 * working set out of the 4096-byte session task stack. */
static frame_t s_frame;

/* A repeated start while a benchmark is running is an error, never a second
 * concurrent run (protocol.md:20). The session task is the only caller of
 * handle_line, so a simple flag is sufficient. */
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
        ESP_LOGI(TAG, "hello: challenge %s", challenge);
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
    const char *run_id = cell_str(f, "run_id");
    ESP_LOGI(TAG, "cancel: run_id %s (log only)", run_id != NULL ? run_id : "?");
}

/* ------------------------------------------------------------- session */

static char s_line[LINE_BUF_SIZE];
static size_t s_line_len = 0;
static int s_line_over = 0;

static void feed_byte(uint8_t b)
{
    if (b == '\n' || b == '\r' || b == 0) {
        if (s_line_len > 0 || s_line_over) {
            if (s_line_over) {
                ESP_LOGW(TAG, "overlong frame discarded (%u bytes)",
                         (unsigned)s_line_len);
            } else {
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
        int n = usb_serial_jtag_read_bytes(&b, 1,
                                           pdMS_TO_TICKS(RX_POLL_MS));
        if (n == 1) {
            feed_byte(b);
        }
    }
}

/* -------------------------------------------------------------- boot */

void app_main(void)
{
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = TX_BUF_SIZE;
    cfg.rx_buffer_size = RX_BUF_SIZE;
    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK) {
        /* No driver: the ROM console fallback keeps the app alive, but no
         * protocol frames can be exchanged. */
        ESP_LOGE(TAG, "usb_serial_jtag driver install failed: %s",
                 esp_err_to_name(err));
        return;
    }
    ESP_LOGI(TAG, "driver installed: tx %u rx %u", TX_BUF_SIZE, RX_BUF_SIZE);
    ESP_LOGI(TAG, "application %s build_id %s", PROTO_APPLICATION,
             BENCH_BUILD_ID);

    send_ready(NULL);

    BaseType_t ok = xTaskCreate(session_task, "proto_ses", SESSION_STACK,
                                 NULL, SESSION_PRIO, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "session task creation failed");
        return;
    }
    ESP_LOGI(TAG, "session running; waiting for hello");
}
