typedef unsigned int u32;

extern void *xQueueGenericCreate(unsigned int length, unsigned int item_size,
                                 unsigned char queue_type);
extern int xQueueGenericSend(void *queue, const void *item, unsigned int wait,
                             unsigned int position);

static void *s_q;

void make(u32 item_size) {
    s_q = xQueueGenericCreate(1, item_size, 0);
}

int send(u32 value) {
    return xQueueGenericSend(s_q, &value, 0, 0);
}
