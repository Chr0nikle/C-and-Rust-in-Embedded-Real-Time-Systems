#define SLOT_N 1024
static void *slots[SLOT_N];
void **slot_blocks(void) {
    return slots;
}
