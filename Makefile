# situ -- top-level build
#
# Sub-projects (runtime/c, tests/generated) are self-contained and are never
# handed an include file from here. Configuration reaches them through the
# environment only, which is what keeps them independently usable: cd into
# runtime/c and `make` works with its own defaults.

PYTHON		?= python3
BUILD_ROOT	?= $(CURDIR)/build

# Toolchain. CROSS_COMPILE is the usual prefix convention; ARCH only names the
# build directory and is not otherwise interpreted. Nothing is compiled at this
# level, so the toolchain is propagated rather than used here.
CROSS_COMPILE	?=
ARCH		?= host
CROSS_AARCH64	?= aarch64-linux-gnu-

WARNFLAGS	:= -Wall -Wextra -Werror -Wconversion -Wsign-conversion
CFLAGS		?= -std=c11 -O2 -g $(WARNFLAGS)
LDFLAGS		?=

BUILD_DIR	:= $(BUILD_ROOT)/$(ARCH)
RUNTIME_DIR	:= $(CURDIR)/runtime/c
RUNTIME_INC	:= $(RUNTIME_DIR)
RUNTIME_LIB	:= $(BUILD_DIR)/runtime/libsitu.a

# GNU make supplies built-in defaults for CC, AR and LD, so `?=` never fires
# for them and an unconditional export would clobber each sub-project's own
# CROSS_COMPILE handling with the host tools. Propagate only what the user
# actually chose, and let CROSS_COMPILE carry the rest.
ifneq ($(origin CC),default)
export CC
endif
ifneq ($(origin AR),default)
export AR
endif
ifneq ($(origin LD),default)
export LD
endif

export CROSS_COMPILE CFLAGS LDFLAGS
export RUNTIME_INC RUNTIME_LIB

.PHONY: all runtime compiler test test-c test-py check lint cross cross-test clean help

all: runtime

help:
	@echo 'situ build targets:'
	@echo '  all        build the C runtime (default)'
	@echo '  test       run the full test suite: python, mypy, generated C'
	@echo '  test-py    run the compiler test suite only'
	@echo '  test-c     build and run the C tests only'
	@echo '  check      mypy strict over situc/'
	@echo '  lint       source convention checks (indent, ASCII, whitespace)'
	@echo '  cross      compile-only build for aarch64'
	@echo '  cross-test run generated accessors on aarch64 under emulation'
	@echo '  clean      remove the build tree'
	@echo ''
	@echo 'Variables: CC AR LD CFLAGS CROSS_COMPILE ARCH BUILD_ROOT PYTHON'

runtime:
	@$(MAKE) --no-print-directory -C runtime/c BUILD_DIR='$(BUILD_DIR)/runtime'

test: test-py check lint test-c cross-test

test-py:
	$(PYTHON) -m pytest tests -q

check:
	$(PYTHON) -m mypy situc tools tests

lint:
	$(PYTHON) tools/lint_conventions.py

test-c: runtime
	@$(MAKE) --no-print-directory -C tests/generated BUILD_DIR='$(BUILD_DIR)/tests' test

# aarch64 has a cross compiler here but no cmocka build and no emulator, so
# the cross target compiles the runtime warning-clean and stops there.
# See docs/decisions/0004-aarch64-compile-only.md.
#
# CROSS_COMPILE goes on the command line, not the environment, so that it
# outranks anything this level exported.
cross:
	@$(MAKE) --no-print-directory \
		CROSS_COMPILE='$(CROSS_AARCH64)' ARCH=aarch64 runtime

# Behavioural, not just warning-clean: the generated accessors are run on
# aarch64 under emulation, and compiled big endian with a static assertion on
# the byte-order marker. See docs/decisions/0007-cross-architecture-testing.md.
cross-test:
	@$(MAKE) --no-print-directory -C tests/cross BUILD_DIR='$(BUILD_DIR)/cross' check

clean:
	@$(MAKE) --no-print-directory -C runtime/c BUILD_DIR='$(BUILD_DIR)/runtime' clean
	@$(MAKE) --no-print-directory -C tests/generated BUILD_DIR='$(BUILD_DIR)/tests' clean
	@$(MAKE) --no-print-directory -C tests/cross BUILD_DIR='$(BUILD_DIR)/cross' clean
	rm -rf '$(BUILD_ROOT)'
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .mypy_cache .pytest_cache
