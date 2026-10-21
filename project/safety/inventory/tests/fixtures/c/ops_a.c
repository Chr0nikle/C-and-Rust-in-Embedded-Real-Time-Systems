#include "ops_fixture.h"
#include "third_party_macros.h"

extern void *pvPortMalloc(unsigned int size);
extern void vPortFree(void *ptr);
extern void *xQueueGenericCreate(unsigned int length, unsigned int item_size,
                                  unsigned char queue_type);
extern void bench_rust_notify(void *task);

#define PROJECT_PTR_CAST(x) ((Node *)(x))
#define PROJECT_DEREF(x) (*(x))

unsigned int g_count;
unsigned int *g_ptr;
char s_line[16];
unsigned int s_line_len;
Node *g_node;
unsigned int g_array[4];
Node *g_ptr_array[2];
Outer g_struct;

void pointer_ops(Node *node, unsigned int *p, unsigned int *array) {
    unsigned int x = *p;
    Node *unused = node;
    x += node->value;
    p = p + 1;
    p++;
    x += ((unsigned char *)p)[0];
    x += array[1];
    Value value;
    value.i = (int)x;
    g_count = x;
    g_count += 1;
    x += g_count;
    (void)unused;
}

void conversion_ops(Node *p) {
    Node *a = (Node *)p;
    Node *b = (Node *)0;
    Node *c = (Node *)(unsigned long)p;
    unsigned long d = (unsigned long)p;
    Node *e = p;
    Node *f = PROJECT_PTR_CAST(p);
    Node *g = TP_PTR_CAST(p);
    Node *h = (Node *)(void *)p;
    Node *i = (Node *)p; Node *j = (Node *)p;
#if 0
    Node *dead = (Node *)p;
    (void)dead;
#endif
    (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g; (void)h; (void)i; (void)j;
}

void arithmetic_ops(int *p, int *q, int n) {
    p++;
    ++p;
    p--;
    --p;
    p += 2;
    p -= 2;
    p + 1;
    p - 1;
    n++;
    n += 2;
    int x = p[1];
    (void)q;
    (void)x;
}

void compound_overlap(void) {
    g_ptr += 1;
}

void global_ops(unsigned int value, unsigned int index) {
    g_count = 1;
    unsigned int x = g_count;
    g_count++;
    ++g_count;
    g_count += 2;
    s_line[s_line_len] = (char)value;
    s_line[s_line_len++] = (char)value;
    g_node->value = (int)value;
    *g_ptr = value;
    (void)&g_count;
    g_struct.inner.value = (int)value;
    g_array[index] = value;
    g_array[g_array[0]] = value;
    g_ptr_array[index]->value = (int)value;
    g_ptr[index] = value;
    g_struct.values[index] = value;
    (void)x;
}

void *allocate_value(void) {
    return pvPortMalloc(16);
}

void release_value(void *ptr) {
    vPortFree(ptr);
}

void kernel_call(void) {
    xQueueGenericCreate(1, 4, 0);
}

void wrapper_call(void) {
    bench_rust_notify((void *)g_node);
}

void indirect_call(void (*fn)(void)) {
    fn();
}

void second_tu_helper(void) {
    g_ptr = &g_count;
}
