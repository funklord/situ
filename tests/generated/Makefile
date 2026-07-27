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
TESTS		:= test_runtime test_header test_tiff test_message test_protobuf \
		   test_packet test_kernels
BINS		:= $(TESTS:%=$(BUILD_DIR)/%) $(TESTS:%=$(BUILD_DIR)/%_checked)

# Schemas whose generated accessors are exercised here. The code is generated
# into the build tree rather than committed, so a codegen change cannot leave a
# stale copy behind.
PYTHON		?= python3
SITUC		?= $(PYTHON) -m situc.cli
GEN_DIR		:= $(BUILD_DIR)/gen

# Schemas are named by their path relative to the repository root, so an
# example can be exercised where it lives rather than copied here to drift.
#
# Every example is here, not a sample: `gen-checks` derives its suite from the
# schema, so adding one costs nothing to maintain and buys executed generated
# code for whatever construct it uses. Before this, half the constructs in the
# language had generated code that compiled and had never run.
SCHEMAS		:= tests/schemas/header.situ \
		   examples/arp/arp.situ examples/bmp/bmp.situ \
		   examples/dns/dns.situ examples/ethernet/ethernet.situ \
		   examples/icmp/icmp.situ examples/ipv4/ipv4.situ \
		   examples/message/message.situ examples/ntp/ntp.situ \
		   examples/packet/packet.situ examples/protobuf/protobuf.situ \
		   examples/registers/registers.situ examples/tcp/tcp.situ \
		   examples/telemetry/telemetry.situ examples/tiff/tiff.situ \
		   examples/udp/udp.situ std/kernels.situ
GEN_NAMES	:= $(notdir $(basename $(SCHEMAS)))
GEN_HEADERS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.h)
GEN_OBJS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.o)

# Generated golden-vector suites and fuzz harnesses, built alongside the
# hand-written tests. The vector suites run under cmocka; the fuzz harness is
# built standalone so a harness nobody can compile cannot go unnoticed.
VECTOR_SCHEMAS	:= header
VECTOR_BINS	:= $(VECTOR_SCHEMAS:%=$(BUILD_DIR)/%_vectors)
FUZZ_BINS	:= $(VECTOR_SCHEMAS:%=$(BUILD_DIR)/%_fuzz)

# The capability-conformance suites, one per schema, generated from the schema
# alone. These are what hold the emitted accessors to what the map claims.
CHECK_BINS	:= $(GEN_NAMES:%=$(BUILD_DIR)/%_checks)

# The conformance harness generated from the standard signature library. It is
# compiled but not run: it needs implementations, and shipping those is the
# user's job. Compiling it is what proves the generator emits valid C for every
# signature in the library, which is where a codegen bug would otherwise hide.
CODEC_SUITE	:= $(GEN_DIR)/codecs_codec_tests.o

# Which generated objects each test needs. Linking all of them into every
# binary would collide: two schemas may both declare a `Header`, and each
# generates its own accessors for it.
OBJS_test_runtime	:=
OBJS_test_header	:= $(GEN_DIR)/header.o
OBJS_test_tiff		:= $(GEN_DIR)/tiff.o
OBJS_test_message	:= $(GEN_DIR)/message.o
OBJS_test_protobuf	:=
OBJS_test_packet	:= $(GEN_DIR)/packet.o
OBJS_test_kernels	:= $(GEN_DIR)/kernels.o $(GEN_DIR)/kernels_derived.o

# The per-test object lists are named after the target, so the prerequisites
# have to be expanded a second time to see them. Without this a test linking a
# generated object nobody else needs fails at the linker rather than building
# it (which is how test_kernels first failed).
.SECONDEXPANSION:

.PHONY: all test clean generate

all: $(BINS) $(VECTOR_BINS) $(FUZZ_BINS) $(CHECK_BINS) $(CODEC_SUITE)

generate: $(GEN_HEADERS)

# One rule per schema, because they live in different directories and a single
# pattern rule cannot serve both. situc runs from the repository root so
# `python3 -m situc.cli` finds the package; GEN_DIR may be absolute (parent) or
# relative (standalone), so it is resolved before the directory change.
define situ_generate
$$(GEN_DIR)/$(notdir $(basename $(1))).h $$(GEN_DIR)/$(notdir $(basename $(1))).c: \
		$$(REPO_ROOT)/$(1) $$(SITUC_SOURCES)
	@mkdir -p '$$(GEN_DIR)'
	cd '$$(REPO_ROOT)' && $$(SITUC) build '$(1)' --out '$$(abspath $$(GEN_DIR))'
endef

REPO_ROOT	:= $(abspath ../..)

# Generated code depends on the generator, which make cannot see from the
# schema alone. Without this a codegen change leaves the whole suite running
# against stale output and passing -- which is exactly how the validator bug
# below survived a full `make test` after it was fixed.
SITUC_SOURCES	:= $(wildcard $(REPO_ROOT)/situc/*.py) \
		   $(wildcard $(REPO_ROOT)/situc/codegen/c/*.py)

$(foreach schema,$(SCHEMAS),$(eval $(call situ_generate,$(schema))))

# Which schema each generated name came from, so the checks rule can find it.
$(foreach schema,$(SCHEMAS),\
	$(eval SCHEMA_$(notdir $(basename $(schema))) := $(schema)))

$(GEN_DIR)/%.o: $(GEN_DIR)/%.c $(GEN_DIR)/%.h
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' -c $< -o $@

$(BUILD_DIR)/%_checked: %.c $(RUNTIME_LIB) $(GEN_OBJS) $$(OBJS_$$*)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -DSITU_CHECKED -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(OBJS_$*) $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%: %.c $(RUNTIME_LIB) $(GEN_OBJS) $$(OBJS_$$*)
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
		$(REPO_ROOT)/tests/schemas/%.vectors $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-tests 'tests/schemas/$*.situ' \
		'tests/schemas/$*.vectors' --out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/codecs_codec_tests.c: $(REPO_ROOT)/std/codecs.situ $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-codec-tests std/codecs.situ \
		--out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/codecs_codec_tests.o: $(GEN_DIR)/codecs_codec_tests.c
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' -c $< -o $@

$(GEN_DIR)/%_fuzz.c: $(REPO_ROOT)/tests/schemas/%.situ $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-fuzz 'tests/schemas/$*.situ' \
		--out '$(abspath $(GEN_DIR))'

# The derived implementations, generated beside the accessors from the same
# kernel descriptions. That they come from one source is the whole point of
# tier 2, so they are built from one command run.
$(GEN_DIR)/%_derived.c: $(GEN_DIR)/%.h $(SITUC_SOURCES)
	cd '$(REPO_ROOT)' && $(SITUC) gen-derived '$(SCHEMA_$*)' \
		--out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/%_derived.o: $(GEN_DIR)/%_derived.c
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' -c $< -o $@

$(GEN_DIR)/%_checks.c: $(GEN_DIR)/%.h $(SITUC_SOURCES)
	cd '$(REPO_ROOT)' && $(SITUC) gen-checks '$(SCHEMA_$*)' \
		--out '$(abspath $(GEN_DIR))'

$(BUILD_DIR)/%_checks: $(GEN_DIR)/%_checks.c $(GEN_DIR)/%.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/$*.o $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%_vectors: $(GEN_DIR)/%_vectors.c $(GEN_DIR)/%.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/$*.o $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

$(BUILD_DIR)/%_fuzz: $(GEN_DIR)/%_fuzz.c $(GEN_DIR)/%.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -DSITU_FUZZ_STANDALONE -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/$*.o $(RUNTIME_LIB) $(LDFLAGS) -o $@

test: all
	@for t in $(BINS) $(VECTOR_BINS) $(CHECK_BINS); do \
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
