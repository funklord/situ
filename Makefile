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

# Install location. DESTDIR is honoured for staged/package builds.
PREFIX		?= /usr/local
DESTDIR		?=

WARNFLAGS	:= -Wall -Wextra -Werror -Wconversion -Wsign-conversion
CFLAGS		?= -std=c11 -Os -g $(WARNFLAGS)
LDFLAGS		?=

BUILD_DIR	:= $(BUILD_ROOT)/$(ARCH)

# The one place the version is stated; situc.__version__ reads the same
# file and `make version-check` holds debian/changelog to it.
VERSION		:= $(shell cat VERSION)
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

.PHONY: all runtime compiler test test-c test-py check typecheck lint bench fuzz \ veryclean distclean style style-source style-docs hooks
	cross cross-test install uninstall clean help deb deb-check

all: runtime

help:
	@echo 'situ build targets:'
	@echo '  all        build the C runtime (default)'
	@echo '  test       run the test suite: python and generated C'
	@echo '  test-py    run the compiler test suite only'
	@echo '  test-c     build and run the C tests only'
	@echo '  check      everything: style, types, tests, cross'
	@echo '  style      indentation, ASCII and whitespace, plus project.md'
	@echo '  typecheck  mypy strict over situc, tools and tests'
	@echo '  lint       alias for style-source'
	@echo '  hooks      install the commit-message hook from tools/hooks/'
	@echo '  bench      what the offset cache costs, in all four backends'
	@echo '  fuzz       run every generated harness under libFuzzer + ASan'
	@echo '  cross      compile-only build for aarch64'
	@echo '  cross-test run generated accessors on aarch64 under emulation'
	@echo '  install    install situc and the runtime under PREFIX'
	@echo '  uninstall  remove what install put there'
	@echo '  deb        build situc and libsitu-dev .deb packages'
	@echo '  deb-check  build them, then install into a scratch root and run'
	@echo '  clean      remove the build tree'
	@echo ''
	@echo 'Variables: CC AR LD CFLAGS CROSS_COMPILE ARCH BUILD_ROOT PYTHON'
	@echo '           PREFIX DESTDIR (install)  DEB_MAINTAINER (deb)'

runtime:
	@$(MAKE) --no-print-directory -C runtime/c BUILD_DIR='$(BUILD_DIR)/runtime'

check: style typecheck lint test cross-test

test: test-py test-c

test-py:
	$(PYTHON) -m pytest tests -q

# The shipped Python runtime is checked too, and was not: `mypy situc tools
# tests` reads the compiler and its suite, and `runtime/python` is neither --
# so the module every generated module imports was the one nothing checked. It
# had four dead `type: ignore` comments, which strict mode calls errors.
typecheck:
	$(PYTHON) -m mypy situc tools tests
	$(PYTHON) -m mypy --strict runtime/python

# `style` replaced `lint_conventions.py`, and this target kept invoking the
# deleted file -- so `make check`, which lists both, could not run at all.
# Kept as an alias rather than removed: a target name is a surface other
# people and scripts type, and withdrawing one is a convention change rather
# than a repair. Whether `check` should list both is for whoever settles the
# target vocabulary.
lint: style-source

# Not part of `test`, and not a threshold. A wall-clock number belongs to the
# machine that took it (26.30), so this reports and asserts nothing.
bench:
	$(PYTHON) tools/bench.py

test-c: runtime
	@$(MAKE) --no-print-directory -C tests/generated BUILD_DIR='$(BUILD_DIR)/tests' test

# Not part of `test`: minutes rather than seconds, and a compiler `test` does
# not need. FUZZ_SECONDS is per harness.
fuzz:
	@$(MAKE) --no-print-directory -C tests/generated BUILD_DIR='$(BUILD_DIR)/tests' fuzz

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

# situc is a Python program with no dependencies, so installing it is copying
# the package and a launcher that finds it. Section 24 requires it to run from
# a bare interpreter -- no pip, no virtualenv, no network -- because the build
# machine it has to vendor into may have none of them.
#
# `runtime` rather than `$(RUNTIME_LIB)`: the library is built by a sub-make and
# nothing at this level has a rule that produces that path, so naming the file
# made `make install` work only where a previous build had already left one
# there. From a clean tree it stopped with "No rule to make target", which is
# the first command a packager runs.
install: runtime
	install -d '$(DESTDIR)$(PREFIX)/lib/situc'
	find situc -name '*.py' -exec install -Dm644 '{}' '$(DESTDIR)$(PREFIX)/lib/{}' \;
	install -d '$(DESTDIR)$(PREFIX)/share/situc/std'
	install -m644 std/*.situ '$(DESTDIR)$(PREFIX)/share/situc/std'
	install -Dm644 runtime/python/situ_runtime.py \
		'$(DESTDIR)$(PREFIX)/lib/situc/_runtime/situ_runtime.py'
	@# VERSION ships beside the module: situc.__version__ reads it, and
	@# without it the installed package fails on import while the source
	@# tree works fine.
	install -Dm644 VERSION '$(DESTDIR)$(PREFIX)/lib/situc/VERSION'
	install -Dm755 bin/situc '$(DESTDIR)$(PREFIX)/bin/situc'
	install -Dm644 runtime/c/situ.h '$(DESTDIR)$(PREFIX)/include/situ.h'
	install -Dm644 '$(RUNTIME_LIB)' '$(DESTDIR)$(PREFIX)/lib/libsitu.a'
	@# The manual page ships from here rather than from debhelper, so that a
	@# source install gets `man situc` too. It was previously installed only
	@# by the deb, which left the one complete command reference reachable
	@# only to people who did not build from the tree.
	install -Dm644 packaging/situc.1 \
		'$(DESTDIR)$(PREFIX)/share/man/man1/situc.1'
	@echo 'installed situc to $(DESTDIR)$(PREFIX)/bin/situc'

uninstall:
	rm -rf '$(DESTDIR)$(PREFIX)/lib/situc' '$(DESTDIR)$(PREFIX)/share/situc'
	rm -f '$(DESTDIR)$(PREFIX)/bin/situc' '$(DESTDIR)$(PREFIX)/include/situ.h'
	rm -f '$(DESTDIR)$(PREFIX)/lib/libsitu.a'
	rm -f '$(DESTDIR)$(PREFIX)/share/man/man1/situc.1'

# -- packaging ---------------------------------------------------------------
#
# Two packages, because they do not have the same architecture: `situc` is
# Python and runs anywhere, `libsitu.a` is compiled objects and does not. One
# package would have to claim the narrower of the two, which would make the
# schema compiler uninstallable on every machine it was not built on -- for a
# tool whose whole purpose is generating code for targets other than the build
# host, exactly the wrong way round. See packaging/README.md.
#
# The version is read from the package rather than kept here, so `situc
# --version` and the `.deb` can never disagree.
DEB_VERSION	:= $(shell $(PYTHON) -c "import situc; print(situc.__version__)")
DEB_ARCH	:= $(shell dpkg --print-architecture 2>/dev/null || echo all)
DEB_MAINTAINER	?= situ maintainers <noreply@example.invalid>
DEB_DIR		:= $(BUILD_ROOT)/deb
DEB_STAGE	:= $(DEB_DIR)/stage

# The changelog needs a date, and `date -R` would put the build clock in the
# package -- two builds of one commit producing two different files. The last
# commit's own date is the honest answer and is reproducible; SOURCE_DATE_EPOCH
# wins where a packaging environment has set one.
DEB_DATE	:= $(shell \
	if [ -n "$$SOURCE_DATE_EPOCH" ]; then date -R -d "@$$SOURCE_DATE_EPOCH"; \
	else git log -1 --format=%cD 2>/dev/null || date -R; fi)

# Removing a directory wholesale is allowed for a staging tree this rule
# created and nothing else, and only after checking the path resolved to what
# it should: an unset or mistyped variable in `rm -rf $(VAR)` is precisely how
# a build target eats a source tree. Every removal below goes through here.
deb_discard = \
	test -n '$(1)' || { echo 'deb: empty path, refusing to remove'; exit 1; }; \
	case '$(1)' in \
		'$(DEB_DIR)'|'$(DEB_DIR)'/*) rm -rf '$(1)' ;; \
		*) echo 'deb: $(1) is not under $(DEB_DIR); refusing'; exit 1 ;; \
	esac

# Native Debian packaging: debian/ holds the metadata, debhelper does the
# work and splits the tree into situc and libsitu-dev.
# `$(DEB_DIR)` and not `$(BUILD_DIR)/deb`: the two are different directories
# whenever ARCH is set, which is always, since BUILD_DIR is BUILD_ROOT/ARCH.
# This rule wrote the packages to one and `deb-check`, `deb_discard` and the
# clean targets all named the other, so the verification step could not find
# what the build had just produced and every package survived `veryclean`.
deb: version-check
	@test -n "$(DEB_DIR)" || { echo "deb: DEB_DIR is empty, refusing" >&2; exit 1; }
	dpkg-buildpackage -b -us -uc
	@mkdir -p '$(DEB_DIR)'
	@for f in ../situc_$(VERSION)_*.deb ../libsitu-dev_$(VERSION)_*.deb \
	          ../situ_$(VERSION)_*.buildinfo ../situ_$(VERSION)_*.changes; do \
		[ -e "$$f" ] && mv -f "$$f" '$(DEB_DIR)/' || true; \
	done
	@ls -1 '$(DEB_DIR)'/*.deb

# The VERSION file is the source; debian/changelog is checked against it.
version-check:
	@file=$$(cat VERSION); \
	changelog=$$(dpkg-parsechangelog -SVersion 2>/dev/null); \
	if [ -z "$$changelog" ]; then \
		echo "version-check: skipped (dpkg-parsechangelog unavailable)"; \
	elif [ "$$file" != "$$changelog" ]; then \
		echo "version-check: VERSION says $$file but"; \
		echo "               debian/changelog says $$changelog"; \
		exit 1; \
	else \
		echo "version-check: $$file, in step"; \
	fi

deb-check: deb
	@$(call deb_discard,$(DEB_DIR)/root)
	@mkdir -p '$(DEB_DIR)/root'
	@for deb in '$(DEB_DIR)'/*.deb; do dpkg-deb -x "$$deb" '$(DEB_DIR)/root'; done
	@'$(DEB_DIR)/root/usr/bin/situc' --version
	@cd '$(DEB_DIR)/root' && ./usr/bin/situc build --target c \
		'$(CURDIR)/examples/modbus/modbus.situ' --out . >/dev/null
	@$(CC) -std=c11 $(WARNFLAGS) -c '$(DEB_DIR)/root/modbus.c' \
		-I'$(DEB_DIR)/root/usr/include' -o '$(DEB_DIR)/root/modbus.o'
	@# `verify` runs the accessors in memory, so it needs the Python runtime
	@# the package installs beside the module. It was missing at first, and
	@# nothing noticed until it was run from a scratch root.
	@'$(DEB_DIR)/root/usr/bin/situc' verify \
		'$(CURDIR)/examples/arp/arp.situ' '$(CURDIR)/examples/arp/arp.vectors'
	@echo 'deb-check: the installed situc built a schema against the installed runtime'

clean:
	@$(MAKE) --no-print-directory -C runtime/c BUILD_DIR='$(BUILD_DIR)/runtime' clean
	@$(MAKE) --no-print-directory -C tests/generated BUILD_DIR='$(BUILD_DIR)/tests' clean
	@$(MAKE) --no-print-directory -C tests/cross BUILD_DIR='$(BUILD_DIR)/cross' clean
	rm -rf '$(BUILD_ROOT)'
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .mypy_cache .pytest_cache

# The shared style gate: one tool, copied verbatim from
# ~/.claude/tools/style_gate.py into every private project. It refuses to
# run against a collapsed file list, so a pass means it actually looked.
style: style-source style-docs

style-source:
	$(PYTHON) tools/style_gate.py check

# project.md is authoritative, so it is held to the tree: a heading
# that appears twice means whichever one you find, the other is the
# one with the answer.
style-docs:
	$(PYTHON) tools/style_gate.py docs

# The clean ladder, matching the sibling projects. `clean` already removes
# the build root; `veryclean` adds the packaging output, `distclean` the
# editor and tool droppings.
veryclean: clean
	rm -rf packaging/out dist

distclean: veryclean
	find . -name '*~' -o -name '*.swp' -o -name '*.orig' | xargs -r rm -f
	find . -name .pytest_cache -type d -prune -exec rm -rf {} +

# The commit-msg hook lives in the tree so it is reviewable, survives a
# clone, and can be kept in sync. .git/hooks is untracked, so a hook that
# exists only there enforces a rule nobody can see and vanishes silently on
# a fresh clone.
hooks:
	@test -d .git || { echo "hooks: not a git repository" >&2; exit 1; }
	@install -m 0755 tools/hooks/commit-msg .git/hooks/commit-msg
	@echo "hooks: commit-msg installed from tools/hooks/"
