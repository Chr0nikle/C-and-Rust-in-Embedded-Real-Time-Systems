struct Message {
    unsigned int a;
    unsigned int b;
};

extern void *xQueueGenericCreate(unsigned int length, unsigned int item_size,
                                 unsigned char queue_type);
extern int xQueueGenericSend(void *queue, const void *item, unsigned int wait,
                             unsigned int position);

static void *s_q;

void make(void) {
    s_q = xQueueGenericCreate(1, sizeof(struct Message), 0);
}

int send(struct Message *message) {
    return xQueueGenericSend(s_q, message, 0, 0);
}
