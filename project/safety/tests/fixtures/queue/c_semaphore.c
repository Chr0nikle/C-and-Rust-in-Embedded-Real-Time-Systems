typedef unsigned int u32;

extern void *xQueueGenericCreate(unsigned int length, unsigned int item_size,
                                 unsigned char queue_type);
extern int xQueueGenericSend(void *queue, const void *item, unsigned int wait,
                             unsigned int position);

#define xSemaphoreCreateBinary() xQueueGenericCreate(1, 0, 1)
#define xSemaphoreGive(queue) xQueueGenericSend((queue), 0, 0, 0)

static void *s_sem;

void make(void) {
    s_sem = xSemaphoreCreateBinary();
}

int give(void) {
    return xSemaphoreGive(s_sem);
}
