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
CFLAGS		?= -std=c11 -pedantic-errors -O2 -g $(WARNFLAGS)

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
	$(CC) $(CFLAGS) -MMD -MP -I. -c $< -o $@

clean:
	rm -rf '$(BUILD_DIR)'

-include $(DEPS)
