#
# firmware/nuttx/c/Makefile
#

include $(TOPDIR)/Make.defs

# RTOS bench built-in application info

PROGNAME  = $(CONFIG_EXAMPLES_RTOS_BENCH_PROGNAME)
PRIORITY  = $(CONFIG_EXAMPLES_RTOS_BENCH_PRIORITY)
STACKSIZE = $(CONFIG_EXAMPLES_RTOS_BENCH_STACKSIZE)
MODULE    = $(CONFIG_EXAMPLES_RTOS_BENCH)

# RTOS bench application sources: main.c and port.c live here; the shared
# runner and header live in firmware/common/c.

MAINSRC = main.c port.c ../../../common/c/src/runner.c

CFLAGS += -I$(TOPDIR)/firmware/common/c/include

include $(TOPDIR)/Application.mk
