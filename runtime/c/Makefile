# libsitu -- minimal C runtime
#
# Self-contained: everything below has a working default, so this builds on
# its own. The parent overrides through the environment.

CROSS_COMPILE	?=

# GNU make defines CC=cc, AR=ar and LD=ld as built-in defaults, so `?=` never
# fires for any of them and a cross build would silently use the host tools.
# `origin` is what distinguishes a built-in from a deliberate user choice.
ifeq ($(origin CC),default)
CC		:= $(CROSS_COMPILE)gcc
endif
ifeq ($(origin AR),default)
AR		:= $(CROSS_COMPILE)ar
endif
ifeq ($(origin LD),default)
LD		:= $(CC)
endif

WARNFLAGS	:= -Wall -Wextra -Werror -Wconversion -Wsign-conversion
# -Os and section collection, as the root's build: built here alone -- by a
# consumer, or a test that needs only the library -- it was -O2.
# What the code requires, in every build, kept out of CFLAGS: a CFLAGS
# from the environment replaces a `?=` default whole, and dpkg-buildpackage
# exports its own (`-g -O2` and the hardening), so with everything in CFLAGS
# a package built straight from dpkg had no -std, no warnings and -O2
# (measured 2026-11-25). CFLAGS keeps only what a builder may choose: the
# optimisation and the debug info.
# This is the library the package ships, so it also takes CPPFLAGS, which
# is where Debian's _FORTIFY_SOURCE arrives.
REQUIRED_CFLAGS	:= -std=c11 -pedantic-errors -ffunction-sections -fdata-sections \
		   $(WARNFLAGS)
CFLAGS		?= -Os -g

BUILD_DIR	?= build
LIB		:= $(BUILD_DIR)/libsitu.a

SRCS		:= situ.c
OBJS		:= $(SRCS:%.c=$(BUILD_DIR)/%.o)
DEPS		:= $(OBJS:.o=.d)

.PHONY: all clean

all: $(LIB)

$(LIB): $(OBJS)
	@mkdir -p '$(@D)'
	$(AR) rcs $@ $^

$(BUILD_DIR)/%.o: %.c
	@mkdir -p '$(@D)'
	$(CC) $(REQUIRED_CFLAGS) $(CPPFLAGS) $(CFLAGS) -MMD -MP -I. -c $< -o $@

clean:
	rm -rf '$(BUILD_DIR)'

-include $(DEPS)
