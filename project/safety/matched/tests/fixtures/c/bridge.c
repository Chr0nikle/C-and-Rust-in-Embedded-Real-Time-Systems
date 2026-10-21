#include "fake_freertos.h"

void bridge_reset(void) {
}

int bridge_send(QueueHandle_t queue, const void *item, unsigned len) {
    return xQueueGenericSend(queue, item, len, 0);
}

void *bridge_alloc(unsigned size) {
    return pvPortMalloc(size);
}
