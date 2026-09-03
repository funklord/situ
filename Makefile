# situ -- top-level build
#
# Sub-projects (runtime/c, test/generated) are self-contained and are never
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

.PHONY: all runtime compiler test test-c test-py check typecheck lint bench fuzz \ veryclean distclean style style-source style-docs hooks walk-c
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
	@echo '  typecheck  mypy over situc, walker, editor, tool and test'
	@echo '  lint       alias for style-source'
	@echo '  hooks      install the commit-message hook from tool/hooks/'
	@echo '  walk       the walker over an image: bin/situ-walk'
	@echo '  walk-c     build the embedded walker: situ-walk-c (0035)'
	@echo '  edit       read a message: bin/situ-edit, -tui (0034)'
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

# `runtime` is a prerequisite because the Python suite compiles and links C:
# a dozen tests build a generated module against `libsitu.a`, and nothing
# here made the archive before using it. `test-c` had the edge and `test-py`
# did not, and `test` runs the second one first -- so on any tree where the
# archive did not already exist, ten tests failed at the linker. It never
# happened on a machine that had built once, which is every machine this ran
# on until there was CI (26.87).
test-py: runtime
	$(PYTHON) -m pytest test -q -rs

# The shipped Python runtime is checked too, and was not: `mypy situc tools
# tests` reads the compiler and its suite, and `runtime/python` is neither --
# so the module every generated module imports was the one nothing checked. It
# had four dead `type: ignore` comments, which strict mode calls errors.
#
# `walker` is named for the same reason. It arrived checked only because the
# suite imports it, which is a coverage that disappears the moment a module
# has no test importing it -- the exact shape of the gap above.
#
# `editor` is named on arrival rather than after the same lesson a third
# time (0034).
#
# The oracle libraries are a hard prerequisite here, which is not the
# accommodation the suite makes: `test` skips an oracle whose tool is absent
# and prints that it did, while this refuses to run at all. That asymmetry is
# deliberate. A skipped test still reports honestly on what it did not do,
# whereas an unresolvable import makes the module `Any` -- so mypy would check
# `oracles.py` against nothing and say it was fine, which is the vacuous pass
# in its purest form.
#
# Configuring around it was measured rather than assumed. `ignore_missing_imports`
# for the two modules silences the import errors and leaves the `type: ignore`
# comments they needed reported as unused, because an `Any` module raises no
# `attr-defined` for them to suppress -- so a machine without the libraries
# still fails, with 2 errors instead of 8. Silencing those too needs
# `warn_unused_ignores` off for the file, which withdraws a real check on every
# machine, including the ones that have the libraries, to accommodate the ones
# that do not. Excluding `oracles.py` was rejected for the same reason as the
# `Any`: it reports success over a file nobody checked.
#
# So the requirement stays and only the diagnosis improves. Ten mypy errors
# about missing stubs read as defects in the code; a named prerequisite reads
# as what it is.
TYPECHECK_MODULES := pymodbus paho.mqtt.client

typecheck:
	@for m in $(TYPECHECK_MODULES); do \
		$(PYTHON) -c "import $$m" >/dev/null 2>&1 || { \
			echo "typecheck: needs the python module '$$m', which the" >&2; \
			echo "typecheck: oracles import and mypy must resolve to check" >&2; \
			echo "typecheck: them. Install: python3-pymodbus python3-paho-mqtt" >&2; \
			echo "typecheck: (\`make test\` skips these oracles instead; 22)" >&2; \
			exit 1; }; \
	done
	$(PYTHON) -m mypy situc walker editor tool test
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
	$(PYTHON) tool/bench.py

# The embedded walker (0035): the one 0026 was argued from, and the one a
# device links. Built with the same warnings as the generated code, because
# a reader that needs a relaxed set is one nobody can put in a build.
walk-c: $(BUILD_DIR)/situ-walk-c

$(BUILD_DIR)/situ-walk-c: walker/c/situ_walk.c walker/c/main.c walker/c/situ_walk.h
	@mkdir -p '$(@D)'
	$(CC) $(CFLAGS) -Iwalker/c walker/c/situ_walk.c walker/c/main.c \
		$(LDFLAGS) -o $@

test-c: runtime
	@$(MAKE) --no-print-directory -C test/generated BUILD_DIR='$(BUILD_DIR)/tests' test

# Not part of `test`: minutes rather than seconds, and a compiler `test` does
# not need. FUZZ_SECONDS is per harness.
fuzz:
	@$(MAKE) --no-print-directory -C test/generated BUILD_DIR='$(BUILD_DIR)/tests' fuzz

# aarch64 has a cross compiler here but no cmocka build and no emulator, so
# the cross target compiles the runtime warning-clean and stops there.
# See doc/decision/0004-aarch64-compile-only.md.
#
# CROSS_COMPILE goes on the command line, not the environment, so that it
# outranks anything this level exported.
cross:
	@$(MAKE) --no-print-directory \
		CROSS_COMPILE='$(CROSS_AARCH64)' ARCH=aarch64 runtime

# Behavioural, not just warning-clean: the generated accessors are run on
# aarch64 under emulation, and compiled big endian with a static assertion on
# the byte-order marker. See doc/decision/0007-cross-architecture-testing.md.
cross-test:
	@$(MAKE) --no-print-directory -C test/cross BUILD_DIR='$(BUILD_DIR)/cross' check

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
	@# The walker is a second program, not a mode of the first: decision
	@# 0026 keeps the compiler and the interpreter apart, and installing
	@# them from one rule is as close as they get.
	find walker -name '*.py' -exec install -Dm644 '{}' \
		'$(DESTDIR)$(PREFIX)/lib/{}' \;
	install -Dm755 bin/situ-walk '$(DESTDIR)$(PREFIX)/bin/situ-walk'
	@# The editor and its frontends. `editor/` is pure stdlib, like the rest
	@# of the tool, and imports no compiler (0026, 0034).
	find editor -name '*.py' -exec install -Dm644 '{}' '$(DESTDIR)$(PREFIX)/lib/{}' \;
	install -Dm755 bin/situ-edit '$(DESTDIR)$(PREFIX)/bin/situ-edit'
	install -Dm755 bin/situ-edit-tui '$(DESTDIR)$(PREFIX)/bin/situ-edit-tui'
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
	rm -rf '$(DESTDIR)$(PREFIX)/lib/walker'
	rm -f '$(DESTDIR)$(PREFIX)/bin/situc' '$(DESTDIR)$(PREFIX)/bin/situ-walk'
	rm -f '$(DESTDIR)$(PREFIX)/include/situ.h'
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
#
# Two readers, and the second one is the point. This used to call
# `dpkg-parsechangelog` alone and print "skipped" when it was absent -- a
# check reporting success over nothing, on exactly the machine least likely
# to have dpkg-dev installed. The topmost changelog entry *is* the current
# version, by the format's own definition, so `sed` can read it and there is
# no machine where this has to give up.
#
# `dpkg-parsechangelog` is still asked where it exists, and the two are
# compared with each other rather than one being preferred. That is what
# keeps the fallback honest: a path that only ever runs where nothing can
# check it is a path that is wrong the first time it matters.
version-check:
	@file=$$(cat VERSION); \
	line=$$(sed -n '1s/^[^ ]* (\([^)]*\)).*/\1/p' debian/changelog); \
	if [ -z "$$line" ]; then \
		echo "version-check: debian/changelog line 1 names no version" >&2; \
		sed -n '1p' debian/changelog >&2; \
		exit 1; \
	fi; \
	tool=$$(dpkg-parsechangelog -SVersion 2>/dev/null); \
	if [ -n "$$tool" ] && [ "$$tool" != "$$line" ]; then \
		echo "version-check: dpkg-parsechangelog reads $$tool where" >&2; \
		echo "               line 1 reads $$line -- this rule's own" >&2; \
		echo "               fallback is wrong, not the changelog" >&2; \
		exit 1; \
	fi; \
	if [ "$$file" != "$$line" ]; then \
		echo "version-check: VERSION says $$file but" >&2; \
		echo "               debian/changelog says $$line" >&2; \
		exit 1; \
	fi; \
	if [ -n "$$tool" ]; then \
		echo "version-check: $$file, in step (both readers agree)"; \
	else \
		echo "version-check: $$file, in step (no dpkg-dev; read line 1)"; \
	fi

deb-check: deb
	@$(call deb_discard,$(DEB_DIR)/root)
	@mkdir -p '$(DEB_DIR)/root'
	@for deb in '$(DEB_DIR)'/*.deb; do dpkg-deb -x "$$deb" '$(DEB_DIR)/root'; done
	@'$(DEB_DIR)/root/usr/bin/situc' --version
	@cd '$(DEB_DIR)/root' && ./usr/bin/situc build --target c \
		'$(CURDIR)/example/modbus/modbus.situ' --out . >/dev/null
	@$(CC) -std=c11 $(WARNFLAGS) -c '$(DEB_DIR)/root/modbus.c' \
		-I'$(DEB_DIR)/root/usr/include' -o '$(DEB_DIR)/root/modbus.o'
	@# `verify` runs the accessors in memory, so it needs the Python runtime
	@# the package installs beside the module. It was missing at first, and
	@# nothing noticed until it was run from a scratch root.
	@'$(DEB_DIR)/root/usr/bin/situc' verify \
		'$(CURDIR)/example/arp/arp.situ' '$(CURDIR)/example/arp/arp.vectors'
	@echo 'deb-check: the installed situc built a schema against the installed runtime'

clean:
	@$(MAKE) --no-print-directory -C runtime/c BUILD_DIR='$(BUILD_DIR)/runtime' clean
	@$(MAKE) --no-print-directory -C test/generated BUILD_DIR='$(BUILD_DIR)/tests' clean
	@$(MAKE) --no-print-directory -C test/cross BUILD_DIR='$(BUILD_DIR)/cross' clean
	rm -rf '$(BUILD_ROOT)'
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .mypy_cache .pytest_cache

# The shared style gate: one tool, copied verbatim from
# ~/.claude/tool/style_gate.py into every private project. It refuses to
# run against a collapsed file list, so a pass means it actually looked.
style: style-source style-docs

style-source:
	$(PYTHON) tool/style_gate.py check

# project.md is authoritative, so it is held to the tree: a heading
# that appears twice means whichever one you find, the other is the
# one with the answer.
style-docs:
	$(PYTHON) tool/style_gate.py docs

# The clean ladder, matching the sibling projects. `clean` already removes
# the build root; `veryclean` adds the packaging output, `distclean` the
# editor and tool droppings.
veryclean: clean
	rm -rf packaging/out dist

# **`distclean` no longer sweeps the tree for editor droppings.** `*~`,
# `*.swp` and `*.orig` are not build output: they belong to somebody's
# editor, and a `.orig` belongs to a merge they may be in the middle of.
# The sweep was also unbounded -- `find .` walks `.git`, and it was measured
# deleting files in there. `git clean -xdn` lists that class and is the
# person's call rather than the build system's.
#
# What is left is what the tooling here really wrote. The two names are
# grouped so that both are reached: `-a` binds tighter than `-o`, so
# `-name a -o -name b -type d -prune -exec rm` runs the action on b alone
# and silently leaves every a in place. A sibling had exactly that and
# removed no `__pycache__` at all. Both caches are named exactly and are
# disposable by construction; `.git` is pruned and every removal is
# printed, because a clean target that deletes silently cannot be checked.
distclean: veryclean
	@find . -name .git -prune -o \
	        \( -name __pycache__ -o -name .pytest_cache \) \
	        -type d -prune -print -exec rm -rf {} +

# The commit-msg hook lives in the tree so it is reviewable, survives a
# clone, and can be kept in sync. .git/hooks is untracked, so a hook that
# exists only there enforces a rule nobody can see and vanishes silently on
# a fresh clone.
# **Where the hooks live is git's question, not the filesystem's.** This
# asked `test -d .git`, which is false in a linked worktree and in a
# submodule checkout: there `.git` is a regular FILE naming the real
# gitdir, and both are git repositories. So `make hooks` refused with
# "not a git repository" inside one that is.
#
# Testing -e instead would only move the failure one line down, because
# the install writes into `.git/hooks/`, which is not a directory there
# either. Asking git answers both halves at once, and from a worktree it
# returns the MAIN repository's hooks directory -- which is the one git
# actually runs.
#
# git's absence is reported as its own thing rather than as "not a git
# repository", which would be a message naming a cause nothing tested.
hooks:
	@if ! command -v git >/dev/null 2>&1; then \
		echo "hooks: git is not installed, so there is nowhere to install to." >&2; \
		exit 1; \
	fi; \
	dir=$$(git rev-parse --git-common-dir 2>/dev/null); \
	if [ -z "$$dir" ]; then \
		echo "hooks: not a git repository, so there is nowhere to install to." >&2; \
		exit 1; \
	fi; \
	mkdir -p "$$dir/hooks"; \
	install -m 0755 tool/hooks/commit-msg "$$dir/hooks/commit-msg"; \
	echo "hooks: commit-msg installed from tool/hooks/ into $$dir/hooks/"
