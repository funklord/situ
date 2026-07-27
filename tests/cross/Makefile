# Cross-architecture behavioural checks.
#
# Answers the question decision 0004 deferred to phase 4: do the generated
# byte-order conversion and unaligned loads behave the same on another
# architecture as on the host? Compiling both targets only ever proved that
# -Wconversion is satisfied, which is a different question.
#
# cmocka has no aarch64 build here, so the probe is a self-checking program
# with no test framework. It is built for the host too, so a failure is a real
# architectural difference rather than a bug in the probe.
#
# Self-contained: builds standalone with its own defaults.

CROSS_COMPILE	?=

ifeq ($(origin CC),default)
CC		:= $(CROSS_COMPILE)gcc
endif

WARNFLAGS	:= -Wall -Wextra -Werror -Wconversion -Wsign-conversion
CFLAGS		?= -std=c11 -O2 -g $(WARNFLAGS)

BUILD_DIR	?= build
REPO_ROOT	:= $(abspath ../..)
RUNTIME_INC	?= $(REPO_ROOT)/runtime/c

PYTHON		?= python3
SITUC		?= $(PYTHON) -m situc.cli

GEN_DIR		:= $(BUILD_DIR)/gen
SCHEMAS		:= tests/schemas/header.situ examples/tiff/tiff.situ
GEN_NAMES	:= $(notdir $(basename $(SCHEMAS)))
GEN_SRCS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.c)
GEN_HEADERS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.h)

# aarch64 toolchain and emulator. Both are optional: the targets that need them
# skip with a message rather than failing, so a machine without them can still
# run the rest of the suite.
A64_CC		?= aarch64-linux-gnu-gcc
A64_QEMU	?= qemu-aarch64

HAVE_A64	:= $(shell command -v $(A64_CC) 2>/dev/null)
HAVE_QEMU	:= $(shell command -v $(A64_QEMU) 2>/dev/null)

.PHONY: all check check-host check-aarch64 check-aarch64-be clean

all: check

check: check-host check-aarch64 check-aarch64-be

define situ_generate
$$(GEN_DIR)/$(notdir $(basename $(1))).h $$(GEN_DIR)/$(notdir $(basename $(1))).c: \
		$$(REPO_ROOT)/$(1)
	@mkdir -p '$$(GEN_DIR)'
	cd '$$(REPO_ROOT)' && $$(SITUC) build '$(1)' --out '$$(abspath $$(GEN_DIR))'
endef

$(foreach schema,$(SCHEMAS),$(eval $(call situ_generate,$(schema))))

# -- host -------------------------------------------------------------------

$(BUILD_DIR)/probe_host: probe.c $(GEN_SRCS) $(GEN_HEADERS)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		probe.c $(GEN_SRCS) '$(RUNTIME_INC)/situ.c' -o $@

check-host: $(BUILD_DIR)/probe_host
	@echo '--- host'
	@'$(abspath $(BUILD_DIR)/probe_host)'

# -- aarch64, little endian, run under emulation ----------------------------

$(BUILD_DIR)/probe_aarch64: probe.c $(GEN_SRCS) $(GEN_HEADERS)
	@mkdir -p '$(@D)'
	$(A64_CC) $(CFLAGS) -static -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		probe.c $(GEN_SRCS) '$(RUNTIME_INC)/situ.c' -o $@

ifeq ($(and $(HAVE_A64),$(HAVE_QEMU)),)
check-aarch64:
	@echo '--- aarch64: skipped ($(A64_CC) or $(A64_QEMU) not found)'
else
check-aarch64: $(BUILD_DIR)/probe_aarch64
	@echo '--- aarch64 (little endian, under $(A64_QEMU))'
	@$(A64_QEMU) '$(abspath $(BUILD_DIR)/probe_aarch64)'
endif

# -- aarch64, big endian, compile only --------------------------------------
#
# There is no big-endian glibc here, so this cannot be linked or run. It can be
# compiled freestanding, which is all the generated code and the runtime need:
# they include nothing but <stdint.h> and <stddef.h>.
#
# A _Static_assert carries the part that actually matters -- that the marker's
# host constant follows the build's endianness -- so this is a real check
# rather than a warning sweep.

$(GEN_DIR)/be_check.c: | $(GEN_HEADERS)
	@mkdir -p '$(GEN_DIR)'
	@printf '#include "tiff.h"\n' > $@
	@printf '_Static_assert(SITU_TIFF_HEADER_BYTE_ORDER_HOST ==\n' >> $@
	@printf '               SITU_TIFF_HEADER_BYTE_ORDER_BIG,\n' >> $@
	@printf '    "a big-endian build must select the big marker");\n' >> $@

ifeq ($(HAVE_A64),)
check-aarch64-be:
	@echo '--- aarch64 big endian: skipped ($(A64_CC) not found)'
else
check-aarch64-be: $(GEN_DIR)/be_check.c $(GEN_SRCS) $(GEN_HEADERS)
	@echo '--- aarch64 (big endian, compile only)'
	@$(A64_CC) $(CFLAGS) -mbig-endian -ffreestanding \
		-I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		-c '$(GEN_DIR)/be_check.c' -o '$(BUILD_DIR)/be_check.o'
	@for src in $(GEN_SRCS) '$(RUNTIME_INC)/situ.c'; do \
		$(A64_CC) $(CFLAGS) -mbig-endian -ffreestanding \
			-I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
			-c "$$src" -o '$(BUILD_DIR)/be_probe.o' || exit 1; \
	done
	@echo 'host marker constant follows the build; generated code compiles clean'
endif

clean:
	rm -rf '$(BUILD_DIR)'
