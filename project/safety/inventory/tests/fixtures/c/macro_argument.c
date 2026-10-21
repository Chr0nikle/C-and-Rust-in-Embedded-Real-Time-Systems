#include "ops_fixture.h"

#define ID(x) (x)
#define CAST(x) ((void *)(x))

void macro_argument(void *p) {
    void *a = ID((char *)p);
    void *b = CAST(p);
    (void)a;
    (void)b;
}
