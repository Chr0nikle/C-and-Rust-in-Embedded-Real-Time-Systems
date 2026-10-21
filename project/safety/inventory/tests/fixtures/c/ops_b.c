#include "ops_fixture.h"

void read_global(void) {
    unsigned int value = g_count;
    (void)value;
}
