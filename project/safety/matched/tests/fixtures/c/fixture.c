#include "fake_freertos.h"

typedef struct {
    void (*queue_send)(void *ctx, unsigned value);
} fixture_port;

static QueueHandle_t q;

void wrapper_send(unsigned value) {
    if (q == 0) return;
    if (xQueueSend(q, &value, 0) != pdPASS) return;
}

void wrapper_receive(unsigned *out) {
    if (q == 0 || out == 0) return;
    if (xQueueReceive(q, out, 0) != pdPASS) return;
}

void wrapper_create(void) {
    q = xQueueCreate(1, sizeof(unsigned));
}

void wrapper_destroy(void) {
    if (q != 0) vQueueDelete(q);
}

void cast_send(unsigned value) {
    (void)xQueueGenericSend(q, (const void *)&value, 0, 0);
}

void same_api_a(unsigned value) {
    (void)xQueueSend(q, &value, 0);
}

void same_api_b(unsigned value) {
    (void)xQueueSend(q, &value, 0);
}

void indirect_call(void (*fn)(void)) {
    fn();
}

void unreferenced(unsigned value) {
    (void)xQueueSend(q, &value, 0);
}

void sample(fixture_port *port) {
    port->queue_send(0, 1);
}

#ifdef ALT_CFG
void cfg_only(unsigned value) {
    (void)xQueueSend(q, &value, 0);
}
#endif

void c_task_entry(void *arg) {
    unsigned value = 0;
    (void)arg;
    (void)xQueueSend(q, &value, 0);
}

void c_create_with_callback(void) {
    (void)xTaskCreate(c_task_entry, "fixture", 128, NULL, 1, NULL);
}

void c_create_with_stack_depth(unsigned priority) {
    (void)xTaskCreate(c_task_entry, "fixture", 256, NULL, priority, NULL);
}

void c_wanted_size(void) {
    (void)pvPortMallocX(64);
}

void c_loop_check(void) {
    while (xPortGetFreeHeapSize() > 0) {
        break;
    }
}

void c_subscript_pointer(void) {
    static void *blocks[2];
    vPortFree(blocks[0]);
}

void c_create_checked(void) {
    if (xTaskCreate(c_task_entry, "fixture", 128, NULL, 1, NULL) != pdPASS) {
        return;
    }
}

void c_create_unresolved_callback(void) {
    (void)xTaskCreate(0, "fixture", 128, NULL, 1, NULL);
}
