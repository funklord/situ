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
		   test_packet test_kernels test_sqlite test_bmp test_icmp
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
SCHEMAS		:= tests/schemas/header.situ tests/schemas/edges.situ \
		   examples/arp/arp.situ examples/ble/ble.situ examples/bmp/bmp.situ \
		   examples/cpio/cpio.situ \
		   examples/dns/dns.situ examples/dnsname/dnsname.situ examples/ethernet/ethernet.situ examples/http/http.situ \
		   examples/icmp/icmp.situ examples/ipv6ext/ipv6ext.situ examples/ipv4/ipv4.situ \
		   examples/message/message.situ examples/modbus/modbus.situ \
		   examples/mqtt/mqtt.situ \
		   examples/netlink/netlink.situ \
		   examples/ntp/ntp.situ \
		   examples/packet/packet.situ examples/protobuf/protobuf.situ \
		   examples/registers/registers.situ examples/rtc/rtc.situ examples/smtp/smtp.situ \
		   examples/sqlite/sqlite.situ \
		   examples/tcp/tcp.situ \
		   examples/telemetry/telemetry.situ examples/tiff/tiff.situ \
		   examples/udp/udp.situ std/kernels.situ
GEN_NAMES	:= $(notdir $(basename $(SCHEMAS)))
GEN_HEADERS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.h)
GEN_OBJS	:= $(GEN_NAMES:%=$(GEN_DIR)/%.o)

# Generated golden-vector suites and fuzz harnesses, built alongside the
# hand-written tests. The vector suites run under cmocka; the fuzz harness is
# built standalone so a harness nobody can compile cannot go unnoticed.
# Schemas with golden vectors beside them, in the `gen-tests` format. This was
# `header` alone, and one schema is a sample of the constructs that schema
# happens to have -- the argument FUZZ_SCHEMAS above already makes. `bmp` is
# here because its vectors are two files ImageMagick wrote (26.35).
VECTOR_SCHEMAS	:= header bmp arp ntp rtc ble netlink cpio
VECTOR_BINS	:= $(VECTOR_SCHEMAS:%=$(BUILD_DIR)/%_vectors)

# Fuzz harnesses, for every schema. This was `header` alone once: the harness
# declared `buf[SITU_X_SIZE_FIXED]` for every struct, and that macro exists
# only where a struct has one size, so `gen-fuzz` emitted C that did not
# compile for anything with a length field, a `[remaining]` tail or a
# delimiter -- and nothing noticed, because the one harness built here was
# over a schema that had none of them.
#
# The answer then was to add `edges`, one schema carrying the shapes that had
# been missing. Which held until a variant turned up: `gen-fuzz` reached for a
# sub-view a variant does not have, and nothing noticed, because neither of
# the two schemas built here had one.
#
# So: all of them, the way CHECK_BINS already does. An artifact compiled for a
# sample of schemas is tested for the constructs that sample happens to
# contain, and which constructs those are is not a thing anyone chose.
FUZZ_SCHEMAS	:= $(GEN_NAMES)
FUZZ_BINS	:= $(FUZZ_SCHEMAS:%=$(BUILD_DIR)/%_fuzz)

# The capability-conformance suites, one per schema, generated from the schema
# alone. These are what hold the emitted accessors to what the map claims.
CHECK_BINS	:= $(GEN_NAMES:%=$(BUILD_DIR)/%_checks)

# The conformance harness, generated and *run*. It calls the ABI of section
# 13.2a under the symbol a codec's `impl` binds, so `tests/schemas/edges.situ`
# -- which binds `doubling` to `my_doubling` -- gets a suite that links against
# `codec_impl.c` beside this file and executes.
#
# It had never run. The harness named `situ_codec_<codec>_encode`, which is not
# what the accessors call and not what any `impl` binds, so it could not be
# linked with an implementation this repository produces; compiling it was the
# most that could be said (26.35).
#
# `std/codecs.situ` still only compiles, and now emits a stated refusal per
# signature rather than a suite: it is a library of contracts with no `impl` at
# all, which is what it is for.
CODEC_SUITE	:= $(GEN_DIR)/codecs_codec_tests.o
CODEC_BINS	:= $(BUILD_DIR)/edges_codec_tests \
		   $(BUILD_DIR)/kernels_codec_tests

# Which generated objects each test needs. Linking all of them into every
# binary would collide: two schemas may both declare a `Header`, and each
# generates its own accessors for it.
OBJS_test_runtime	:=
OBJS_test_header	:= $(GEN_DIR)/header.o
OBJS_test_tiff		:= $(GEN_DIR)/tiff.o
OBJS_test_message	:= $(GEN_DIR)/message.o
OBJS_test_protobuf	:= $(GEN_DIR)/protobuf.o
OBJS_test_packet	:= $(GEN_DIR)/packet.o
OBJS_test_kernels	:= $(GEN_DIR)/kernels.o $(GEN_DIR)/kernels_derived.o
OBJS_test_sqlite	:= $(GEN_DIR)/sqlite.o
OBJS_test_bmp		:= $(GEN_DIR)/bmp.o
OBJS_test_icmp		:= $(GEN_DIR)/icmp.o

# The per-test object lists are named after the target, so the prerequisites
# have to be expanded a second time to see them. Without this a test linking a
# generated object nobody else needs fails at the linker rather than building
# it (which is how test_kernels first failed).
.SECONDEXPANSION:

.PHONY: all test clean generate fuzz

all: $(BINS) $(VECTOR_BINS) $(FUZZ_BINS) $(CHECK_BINS) $(CODEC_SUITE) \
	$(CODEC_BINS)

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

# And where its vectors are, which is beside it. The rule below hardcoded
# `tests/schemas/`, so a vector file could only exist for a schema in that one
# directory -- the same shape as the compile checks that globbed `examples/`
# and never read `edges.situ` (26.31).
$(foreach name,$(VECTOR_SCHEMAS),\
	$(eval VECTORS_$(name) := $(basename $(SCHEMA_$(name))).vectors))

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
$(GEN_DIR)/%_vectors.c: $(REPO_ROOT)/$$(SCHEMA_$$*) \
		$(REPO_ROOT)/$$(VECTORS_$$*) $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-tests '$(SCHEMA_$*)' \
		'$(VECTORS_$*)' --out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/codecs_codec_tests.c: $(REPO_ROOT)/std/codecs.situ $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-codec-tests std/codecs.situ \
		--out '$(abspath $(GEN_DIR))'

$(GEN_DIR)/codecs_codec_tests.o: $(GEN_DIR)/codecs_codec_tests.c
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' -c $< -o $@

# The runnable one: `edges.situ` binds `doubling` to `my_doubling`, which
# `codec_impl.c` implements against 13.2a's ABI.
$(GEN_DIR)/edges_codec_tests.c: $(REPO_ROOT)/tests/schemas/edges.situ \
		$(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-codec-tests tests/schemas/edges.situ \
		--out '$(abspath $(GEN_DIR))'

$(BUILD_DIR)/edges_codec_tests: $(GEN_DIR)/edges_codec_tests.c codec_impl.c \
		$(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< codec_impl.c $(RUNTIME_LIB) $(LDFLAGS) $(CMOCKA_LIBS) -o $@

# And the tier-2 half: the property tests for every codec `std/kernels.situ`
# describes, run against the implementations `gen-derived` writes from the same
# descriptions. Nothing supplies anything here -- both sides are generated,
# which is what a derived codec means.
$(GEN_DIR)/kernels_codec_tests.c: $(REPO_ROOT)/std/kernels.situ $(SITUC_SOURCES)
	@mkdir -p '$(GEN_DIR)'
	cd '$(REPO_ROOT)' && $(SITUC) gen-codec-tests std/kernels.situ \
		--out '$(abspath $(GEN_DIR))'

$(BUILD_DIR)/kernels_codec_tests: $(GEN_DIR)/kernels_codec_tests.c \
		$(GEN_DIR)/kernels_derived.o $(RUNTIME_LIB)
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$< $(GEN_DIR)/kernels_derived.o $(RUNTIME_LIB) $(LDFLAGS) \
		$(CMOCKA_LIBS) -o $@

$(GEN_DIR)/%_fuzz.c: $(GEN_DIR)/%.h $(SITUC_SOURCES)
	cd '$(REPO_ROOT)' && $(SITUC) gen-fuzz '$(SCHEMA_$*)' \
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
	@for t in $(BINS) $(VECTOR_BINS) $(CHECK_BINS) $(CODEC_BINS); do \
		echo "--- $$t"; \
		case "$$t" in /*) "$$t";; *) "./$$t";; esac || exit 1; \
	done
	@# The fuzz harnesses get a handful of inputs each, enough to catch a
	@# harness that no longer runs. Fuzzing them properly is `make fuzz`,
	@# which needs a compiler with libFuzzer and takes as long as it is told.
	@for t in $(FUZZ_BINS); do \
		echo "--- $$t (smoke)"; \
		for i in 0 1 2 3 4 5 6 7; do \
			head -c 32 /dev/urandom | { case "$$t" in /*) "$$t";; *) "./$$t";; esac; } \
				|| exit 1; \
		done; \
	done

# -- fuzzing, for real ------------------------------------------------------
#
# The smoke run above proves a harness still executes. It is eight inputs, and
# eight inputs from `/dev/urandom` reach nothing: a length field is a 1-in-2^16
# guess and a varint is worse. This builds each harness against libFuzzer and
# an address sanitizer and runs it for a bounded time, which is what section 22
# means by fuzzing.
#
# Separate from `test` because it takes minutes rather than seconds, and
# because it needs a compiler `test` does not: clang, or anything else that
# understands `-fsanitize=fuzzer`.
# Whichever clang is installed. Debian ships `clang-19` and no `clang` unless
# the metapackage is there, and a target that fails with "No such file or
# directory" for a tool the machine has is a target nobody runs twice.
FUZZ_CC		?= $(firstword $(shell for c in clang clang-19 clang-18 clang-17; \
			do command -v $$c && break; done) clang)
FUZZ_SECONDS	?= 30
FUZZ_DIR	:= $(BUILD_DIR)/fuzz
FUZZ_CFLAGS	:= -g -O1 -fsanitize=fuzzer,address -fno-omit-frame-pointer
FUZZ_RUNNERS	:= $(FUZZ_SCHEMAS:%=$(FUZZ_DIR)/%)

$(FUZZ_DIR)/%: $(GEN_DIR)/%_fuzz.c $(GEN_DIR)/%.c
	@mkdir -p '$(@D)'
	$(FUZZ_CC) $(FUZZ_CFLAGS) -I'$(RUNTIME_INC)' -I'$(GEN_DIR)' \
		$^ '$(RUNTIME_INC)/situ.c' -o $@

fuzz: $(FUZZ_RUNNERS)
	@for t in $(FUZZ_RUNNERS); do \
		corpus="$(FUZZ_DIR)/corpus/$$(basename $$t)"; \
		mkdir -p "$$corpus"; \
		echo "--- $$t ($(FUZZ_SECONDS)s)"; \
		"$$t" -max_total_time=$(FUZZ_SECONDS) -print_final_stats=1 \
			-artifact_prefix="$$corpus/" "$$corpus" || exit 1; \
	done
	@echo 'no crash in $(words $(FUZZ_RUNNERS)) harnesses'

clean:
	rm -rf '$(BUILD_DIR)'
