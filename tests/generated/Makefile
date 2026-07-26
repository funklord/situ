# cmocka tests over the situ runtime and, from phase 4, over generated code.
#
# Self-contained: builds standalone against ../../runtime/c. The parent
# overrides RUNTIME_INC and RUNTIME_LIB through the environment so it can
# point this at a per-architecture build directory.

CROSS_COMPILE	?=

# See the note in runtime/c/Makefile: CC and LD have built-in defaults, so
# `?=` never fires and `origin` is the only way to tell a built-in from a
# deliberate choice.
ifeq ($(origin CC),default)
CC		:= $(CROSS_COMPILE)gcc
endif
ifeq ($(origin LD),default)
LD		:= $(CC)
endif

WARNFLAGS	:= -Wall -Wextra -Werror -Wconversion -Wsign-conversion
CFLAGS		?= -std=c11 -O2 -g $(WARNFLAGS)
LDFLAGS		?=

BUILD_DIR	?= build

# Standalone defaults: build the runtime here if the parent did not.
RUNTIME_INC	?= ../../runtime/c
RUNTIME_LIB	?= $(BUILD_DIR)/runtime/libsitu.a

CMOCKA_LIBS	?= -lcmocka

# Every test is compiled twice: the checked build exercises the bounds and
# generation assertions, the release build proves they compile out cleanly.
TESTS		:= test_runtime
BINS		:= $(TESTS:%=$(BUILD_DIR)/%) $(TESTS:%=$(BUILD_DIR)/%_checked)

.PHONY: all test clean

all: $(BINS)

$(BUILD_DIR)/%_checked: %.c $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -DSITU_CHECKED -I'$(RUNTIME_INC)' $< $(RUNTIME_LIB) \
		$(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%: %.c $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' $< $(RUNTIME_LIB) \
		$(LDFLAGS) $(CMOCKA_LIBS) -o $@

# Standalone fallback: only reached when the parent did not supply a library.
$(BUILD_DIR)/runtime/libsitu.a:
	@$(MAKE) --no-print-directory -C ../../runtime/c \
		BUILD_DIR='$(CURDIR)/$(BUILD_DIR)/runtime'

# BUILD_DIR may be absolute (parent) or relative (standalone), so resolve the
# invocation rather than blindly prefixing './'.
test: all
	@for t in $(BINS); do \
		echo "--- $$t"; \
		case "$$t" in /*) "$$t";; *) "./$$t";; esac || exit 1; \
	done

clean:
	rm -rf '$(BUILD_DIR)'
