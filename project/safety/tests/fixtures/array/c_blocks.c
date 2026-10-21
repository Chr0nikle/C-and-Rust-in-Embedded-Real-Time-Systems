#define N 4
static void *slots[N];
void **blocks(void) {
    return slots;
}
