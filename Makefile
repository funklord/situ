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

.PHONY: all runtime compiler test test-c test-py check typecheck lint bench fuzz \ veryclean distclean style style-source style-docs hooks
	cross cross-test install uninstall clean help deb deb-check

all: runtime

help:
	@echo 'situ build targets:'
	@echo '  all        build the C runtime (default)'
	@echo '  test       run the full test suite: python, mypy, generated C'
	@echo '  test-py    run the compiler test suite only'
	@echo '  test-c     build and run the C tests only'
	@echo '  check      mypy strict over situc/'
	@echo '  lint       source convention checks (indent, ASCII, whitespace)'
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

lint:
	$(PYTHON) tools/lint_conventions.py

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
	install -Dm755 bin/situc '$(DESTDIR)$(PREFIX)/bin/situc'
	install -Dm644 runtime/c/situ.h '$(DESTDIR)$(PREFIX)/include/situ.h'
	install -Dm644 '$(RUNTIME_LIB)' '$(DESTDIR)$(PREFIX)/lib/libsitu.a'
	@echo 'installed situc to $(DESTDIR)$(PREFIX)/bin/situc'

uninstall:
	rm -rf '$(DESTDIR)$(PREFIX)/lib/situc' '$(DESTDIR)$(PREFIX)/share/situc'
	rm -f '$(DESTDIR)$(PREFIX)/bin/situc' '$(DESTDIR)$(PREFIX)/include/situ.h'
	rm -f '$(DESTDIR)$(PREFIX)/lib/libsitu.a'

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

deb: runtime
	@$(call deb_discard,$(DEB_DIR))
	@mkdir -p '$(DEB_DIR)'
	@# Stage once, through the same install rule a user runs, then split the
	@# tree between the two packages. A packaging bug and an install bug
	@# cannot be different bugs this way.
	@$(MAKE) --no-print-directory install PREFIX=/usr DESTDIR='$(DEB_STAGE)'
	@mkdir -p '$(DEB_STAGE)-situc/usr' '$(DEB_STAGE)-libsitu-dev/usr'
	@mv '$(DEB_STAGE)/usr/bin' '$(DEB_STAGE)-situc/usr/'
	@mv '$(DEB_STAGE)/usr/share' '$(DEB_STAGE)-situc/usr/'
	@mkdir -p '$(DEB_STAGE)-situc/usr/lib'
	@mv '$(DEB_STAGE)/usr/lib/situc' '$(DEB_STAGE)-situc/usr/lib/'
	@mkdir -p '$(DEB_STAGE)-libsitu-dev/usr/lib' \
		'$(DEB_STAGE)-libsitu-dev/usr/include'
	@mv '$(DEB_STAGE)/usr/include/situ.h' \
		'$(DEB_STAGE)-libsitu-dev/usr/include/'
	@mv '$(DEB_STAGE)/usr/lib/libsitu.a' '$(DEB_STAGE)-libsitu-dev/usr/lib/'
	@# Anything left behind is something install writes and neither package
	@# claims, which is a packaging bug and is reported as one rather than
	@# shipped as a hole.
	@find '$(DEB_STAGE)' -type f -printf 'unpackaged: %p\n' -quit | grep . \
		&& { find '$(DEB_STAGE)' -type f -printf '  %P\n'; exit 1; } || true
	@$(call deb_discard,$(DEB_STAGE))
	@# The manual page belongs to the compiler package only.
	@mkdir -p '$(DEB_STAGE)-situc/usr/share/man/man1'
	@gzip -9nc 'packaging/situc.1' \
		> '$(DEB_STAGE)-situc/usr/share/man/man1/situc.1.gz'
	@for pkg in situc libsitu-dev; do \
		mkdir -p "$(DEB_STAGE)-$$pkg/DEBIAN" \
		         "$(DEB_STAGE)-$$pkg/usr/share/doc/$$pkg"; \
		sed -e 's|@VERSION@|$(DEB_VERSION)|' \
		    -e 's|@ARCH@|$(DEB_ARCH)|' \
		    -e 's|@MAINTAINER@|$(DEB_MAINTAINER)|' \
		    'packaging/'"$$pkg"'.control' \
		    > "$(DEB_STAGE)-$$pkg/DEBIAN/control"; \
		install -m644 'packaging/copyright' \
		    "$(DEB_STAGE)-$$pkg/usr/share/doc/$$pkg/copyright"; \
		printf '%s (%s) unstable; urgency=medium\n\n  * %s\n\n -- %s  %s\n' \
		    "$$pkg" '$(DEB_VERSION)' 'Built from the situ source tree.' \
		    '$(DEB_MAINTAINER)' '$(DEB_DATE)' \
		    | gzip -9nc \
		    > "$(DEB_STAGE)-$$pkg/usr/share/doc/$$pkg/changelog.gz"; \
		find "$(DEB_STAGE)-$$pkg" -type d -exec chmod 755 {} +; \
		find "$(DEB_STAGE)-$$pkg/usr/share" -type f -exec chmod 644 {} +; \
		dpkg-deb --root-owner-group --build \
		    "$(DEB_STAGE)-$$pkg" '$(DEB_DIR)' >/dev/null || exit 1; \
	done
	@$(call deb_discard,$(DEB_STAGE)-situc)
	@$(call deb_discard,$(DEB_STAGE)-libsitu-dev)
	@ls -1 '$(DEB_DIR)'/*.deb

# The packages are only worth anything if what comes out of them runs. This
# unpacks both into a scratch root and compiles a schema through the installed
# compiler against the installed runtime -- no root, and nothing touching the
# machine's own /usr.
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
