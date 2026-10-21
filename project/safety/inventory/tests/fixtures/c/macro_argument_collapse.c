#include "ops_fixture.h"

#define BOTH(x) ((Node *)((void *)(x)))
#define USE2(x, y) ((void)(x), (void)(y))

Node *both(Node *p) {
    return BOTH((Node *)p);
}

void two_args(void *p, void *q) {
    USE2((char *)p, (int *)q);
}
