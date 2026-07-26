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
TESTS		:= test_runtime test_header test_tiff test_message
BINS		:= $(TESTS:%=$(BUILD_DIR)/%) $(TESTS:%=$(BUILD_DIR)/%_checked)

# Schemas whose generated accessors are exercised here. The code is generated
# into the build tree rather than committed, so a codegen change cannot leave a
# stale copy behind.
PYTHON		?= python3
SITUC		?= $(PYTHON) -m situc.cli
GEN_DIR		:= $(BUILD_DIR)/gen

# Schemas are named by their path relative to the repository root, so an
# example can be exercised where it lives rather than copied here to drift.
SCHEMAS		:= tests/schemas/header.situ examples/tiff/tiff.situ \
		   examples/message/message.situ
GEN_NAMES	:= $(notdir $(basename $(SCHEMAS)))
GEN_HEADERS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.h)
GEN_OBJS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.o)

# Generated golden-vector suites and fuzz harnesses, built alongside the
# hand-written tests. The vector suites run under cmocka; the fuzz harness is
# built standalone so a harness nobody can compile cannot go unnoticed.
VECTOR_SCHEMAS	:= header
VECTOR_BINS	:= $(VECTOR_SCHEMAS:%=$(BUILD_DIR)/%_vectors)
FUZZ_BINS	:= $(VECTOR_SCHEMAS:%=$(BUILD_DIR)/%_fuzz)

# Which generated objects each test needs. Linking all of them into every
# binary would collide: two schemas may both declare a `Header`, and each
# generates its own accessors for it.
OBJS_test_runtime	:=
OBJS_test_header	:= $(GEN_DIR)/header.o
OBJS_test_tiff		:= $(GEN_DIR)/tiff.o
OBJS_test_message	:= $(GEN_DIR)/message.o

.PHONY: all test clean generate

all: $(BINS) $(VECTOR_BINS) $(FUZZ_BINS)

generate: $(GEN_HEADERS)

# One rule per schema, because they live in different directories and a single
# pattern rule cannot serve both. situc runs from the repository root so
# `python3 -m situc.cli` finds the package; GEN_DIR may be absolute (parent) or
# relative (standalone), so it is resolved before the directory change.
define situ_generate
$$(GEN_DIR)/$(notdir $(basename $(1))).h $$(GEN_DIR)/$(notdir $(basename $(1))).c: \
		$$(REPO_ROOT)/$(1)
	@mkdir -p '$$(GEN_DIR)'
	cd '$$(REPO_ROOT)' && $$(SITUC) build '$(1)' --out '$$(abspath $$(GEN_DIR))'
endef

REPO_ROOT	:= $(abspath ../..)

$(foreach schema,$(SCHEMAS),$(eval $(call situ_generate,$(schema))))

$(GEN_DIR)/%.o: $(GEN_DIR)/%.c $(GEN_DIR)/%.h
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' -c $< -o $@

$(BUILD_DIR)/%_checked: %.c $(RUNTIME_LIB) $(GEN_OBJS)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -DSITU_CHECKED -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(OBJS_$*) $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%: %.c $(RUNTIME_LIB) $(GEN_OBJS)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(OBJS_$*) $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

# Standalone fallback: only reached when the parent did not supply a library.
$(BUILD_DIR)/runtime/libsitu.a:
	@$(MAKE) --no-print-directory -C ../../runtime/c \
		BUILD_DIR='$(CURDIR)/$(BUILD_DIR)/runtime'

# BUILD_DIR may be absolute (parent) or relative (standalone), so resolve the
# invocation rather than blindly prefixing './'.
$(GEN_DIR)/%_vectors.c: $(REPO_ROOT)/tests/schemas/%.situ \
		$(REPO_ROOT)/tests/schemas/%.vectors
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-tests 'tests/schemas/$*.situ' \
		'tests/schemas/$*.vectors' --out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/%_fuzz.c: $(REPO_ROOT)/tests/schemas/%.situ
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-fuzz 'tests/schemas/$*.situ' \
		--out '$(abspath $(GEN_DIR))'

$(BUILD_DIR)/%_vectors: $(GEN_DIR)/%_vectors.c $(GEN_DIR)/%.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/$*.o $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%_fuzz: $(GEN_DIR)/%_fuzz.c $(GEN_DIR)/%.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -DSITU_FUZZ_STANDALONE -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/$*.o $(RUNTIME_LIB) $(LDFLAGS) -o $@

test: all
	@for t in $(BINS) $(VECTOR_BINS); do \
		echo "--- $$t"; \
		case "$$t" in /*) "$$t";; *) "./$$t";; esac || exit 1; \
	done
	@# The fuzz harnesses get a handful of inputs each, enough to catch a
	@# harness that no longer runs. Real fuzzing is a CI job, not a test run.
	@for t in $(FUZZ_BINS); do \
		echo "--- $$t (smoke)"; \
		for i in 0 1 2 3 4 5 6 7; do \
			head -c 32 /dev/urandom | { case "$$t" in /*) "$$t";; *) "./$$t";; esac; } \
				|| exit 1; \
		done; \
	done

clean:
	rm -rf '$(BUILD_DIR)'
