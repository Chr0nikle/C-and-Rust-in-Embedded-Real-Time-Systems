#include "ops_fixture.h"

#define MULTI_CAST(x) ((Node *)((void *)(x)))

Node *nested_macro(Node *p) {
    return MULTI_CAST(p);
}
