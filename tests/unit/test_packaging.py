"""The Debian packaging, checked without building a package.

`make deb` is not in the suite: it wants `dpkg-buildpackage`, and a packaging
step that runs on every commit is a packaging step people stop reading. What
is checked here is the part that rots silently -- the claims `debian/control`
makes about a tree that keeps changing, and the split between the two
packages.

Building and installing them is `make deb-check`, which unpacks both into a
scratch root and compiles a schema through the installed compiler against the
installed runtime. That needs the tools; this does not.

**These read `debian/`, and used to read `packaging/*.control`.** Those were
the hand-rolled dpkg-deb build's inputs; debhelper replaced that build and
0678bf9 removed them as unreferenced. They were referenced -- by this file --
and the eight failures that produced are the reason the search that declares
a file dead has to include the tests. A test is a reader like any other.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

import situc

ROOT      = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
DEBIAN    = ROOT / "debian"
PACKAGES  = ("situc", "libsitu-dev")

#: What debhelper expands, and what it expands to is not this file's business.
#: `${misc:Depends}` is the one every binary package carries; a test that
#: refused it would be a test against debhelper rather than against the
#: packaging.
SUBSTITUTED = "${misc:Depends}"


def stanzas() -> list[dict[str, str]]:
	"""`debian/control`, as deb822 paragraphs.

	Continuation lines are dropped: every field this file asks about is a
	single line, and a `Description` body is prose rather than a claim.
	"""
	found: list[dict[str, str]] = []
	current: dict[str, str] = {}
	for line in (DEBIAN / "control").read_text(encoding="ascii").splitlines():
		if not line.strip():
			if current:
				found.append(current)
				current = {}
			continue
		if line.startswith((" ", "\t")):
			continue
		key, _, value = line.partition(":")
		current[key] = value.strip()
	if current:
		found.append(current)
	return found


def source() -> dict[str, str]:
	"""The source paragraph: the one that carries `Source`."""
	found = next((one for one in stanzas() if "Source" in one), None)
	assert found is not None, "debian/control has no source paragraph"
	return found


def binary(name: str) -> dict[str, str]:
	found = next((one for one in stanzas() if one.get("Package") == name), None)
	assert found is not None, f"debian/control has no `{name}` paragraph"
	return found


def depends(name: str) -> list[str]:
	"""One package's dependencies, split and stripped."""
	held = binary(name).get("Depends", "")
	return [one.strip() for one in held.split(",") if one.strip()]


def test_the_source_paragraph_has_what_dpkg_requires() -> None:
	fields = source()
	for required in ("Source", "Maintainer", "Build-Depends"):
		assert required in fields, f"debian/control's source has no {required}"


@pytest.mark.parametrize("name", PACKAGES)
def test_each_binary_paragraph_has_what_dpkg_requires(name: str) -> None:
	fields = binary(name)
	for required in ("Package", "Architecture", "Description"):
		assert required in fields, f"`{name}` has no {required}"
	assert fields["Package"] == name


def test_the_version_is_not_written_into_the_packaging_twice() -> None:
	"""One version, in the `VERSION` file, and `debian/changelog` reads it.

	A number typed in two places is two numbers the first time either is
	bumped alone. `make version-check` asks this too and *skips* where
	`dpkg-parsechangelog` is absent, which is a check that reports success
	over nothing on exactly the machines least likely to have the tool. This
	needs no tool, so it does not skip.
	"""
	first  = (DEBIAN / "changelog").read_text(encoding="ascii").splitlines()[0]
	found  = re.match(r"\S+ \(([^)]+)\)", first)
	assert found is not None, f"debian/changelog's first line is {first!r}"
	assert found.group(1) == situc.__version__, (
		f"debian/changelog says {found.group(1)}, "
		f"situc.__version__ says {situc.__version__}")


def test_the_compiler_package_is_architecture_independent() -> None:
	"""It is Python, and the reason the packages are split.

	One package would have to claim the runtime's architecture, which would
	make the schema compiler uninstallable on every machine it was not built
	on -- for a tool whose purpose is generating code for targets other than
	the build host, the wrong way round.
	"""
	assert binary("situc")["Architecture"] == "all"
	assert binary("libsitu-dev")["Architecture"] == "any"


def test_the_compiler_needs_nothing_but_python() -> None:
	"""Section 24: the toolchain has to vendor into a build environment where
	`pip install` is not on the table.

	`${misc:Depends}` is debhelper's and is allowed; anything else named here
	is a second thing that environment would have to be given.
	"""
	held = [one for one in depends("situc") if one != SUBSTITUTED]
	assert len(held) == 1, f"situc depends on more than python3: {held}"
	assert held[0].startswith("python3")


def test_the_runtime_package_depends_on_nothing() -> None:
	"""A header and a static library. Depending on the compiler would make a
	target machine install a schema compiler it has no use for."""
	held = [one for one in depends("libsitu-dev") if one != SUBSTITUTED]
	assert not held, f"libsitu-dev depends on {held}"


def test_every_installed_path_belongs_to_exactly_one_package() -> None:
	"""The `deb` rule splits what `make install` staged, and anything it does
	not move is a file no package ships. The rule fails loudly on a leftover;
	this says which paths it is meant to know about, so adding one to
	`install` without adding it here is a visible edit rather than a silent
	hole.
	"""
	makefile = (ROOT / "Makefile").read_text(encoding="ascii")
	rule     = makefile[makefile.index("install: runtime"):]
	rule     = rule[:rule.index("\nuninstall:")]

	# Digits and hyphens are in the class because a manual page lives in
	# `man1` and the walker is `situ-walk`. Each was missing once and each
	# silently truncated a path -- `/share/man/man`, then `/bin/situ` -- so
	# the check reported a directory the rule never names rather than
	# failing. A character class is a claim about what a path may contain.
	installed = {found.rstrip("/")
	             for found in re.findall(r"\$\(PREFIX\)(/[a-z0-9_/.-]+)", rule)
	             if found.rstrip("/")}
	# `/lib` bare is the `find situc -exec install` line, whose destination
	# ends in `{}` and so reads as `$(PREFIX)/lib/` to a regex. It puts the
	# package under `/lib/situc`, which is named beside it.
	assert installed == {
		"/lib", "/lib/situc", "/lib/situc/_runtime/situ_runtime.py",
		"/share/situc/std", "/bin/situc",
		"/include/situ.h", "/lib/libsitu.a",
		"/share/man/man1/situc.1",
		"/bin/situ-walk",
		"/bin/situ-edit", "/bin/situ-edit-tui",
	}, "install writes somewhere the deb rule has not been told about"


def test_the_manual_page_names_every_subcommand() -> None:
	"""A manual page listing eleven of thirteen commands is worse than none:
	the two it leaves out read as unsupported."""
	from situc.cli import build_parser

	page     = (PACKAGING / "situc.1").read_text(encoding="ascii")
	commands = {name for action in build_parser()._actions
	            if isinstance(action, argparse._SubParsersAction)
	            for name in action.choices}

	missing = {name for name in commands if f"\n.B {name}\n" not in page}
	assert not missing, f"situc.1 does not document {sorted(missing)}"


def test_the_copyright_file_does_not_invent_a_licence() -> None:
	"""There is no LICENSE in this tree, and the packaging says so rather than
	picking one. Delete this test when a licence is added -- and the stanza
	with it."""
	text = (PACKAGING / "copyright").read_text(encoding="ascii")
	assert not (ROOT / "LICENSE").exists(), \
		"a LICENSE exists now; packaging/copyright must carry it"
	assert "License: UNDECIDED" in text


def test_the_version_is_a_version() -> None:
	# Two components or more: the version is whatever VERSION says, and
	# "1.0" is a version. Requiring exactly three asserted a habit rather
	# than a rule -- Debian is happy with either.
	assert re.fullmatch(r"\d+(\.\d+)+", situc.__version__)


def test_the_package_declares_the_python_floor_the_project_declares() -> None:
	"""Three statements of one number, and a `.deb` is the one that bites.

	A package whose `Depends` floor is lower than the code's real floor
	installs cleanly and then refuses to start -- which is a live bug in a
	sibling project right now, found by an evaluation in `suggestions/`. situ
	has had the underlying version of it too: it claimed 3.11 for six phases
	while a PEP 701 f-string made it 3.12.

	This only holds the declarations to each other.
	`test_every_module_parses_at_the_declared_floor` is what holds the *code*
	to them, and it skips where the interpreter is absent -- so on a machine
	without the floor installed, the `Depends` line is an unverified claim and
	this test does not pretend otherwise.
	"""
	pyproject = (ROOT / "pyproject.toml").read_text(encoding="ascii")
	found     = re.search(r'^python_version\s*=\s*"(\d+\.\d+)"', pyproject, re.M)
	assert found is not None

	held = [one for one in depends("situc") if one != SUBSTITUTED]
	assert held == [f"python3 (>= {found.group(1)})"]

	# And `Build-Depends`, which is the floor the *build* runs at. A source
	# package that builds under an interpreter older than the code's floor
	# fails at the point `situc` is first invoked, which under debhelper is
	# somewhere inside `dh_auto_build` rather than anywhere that names a
	# version. Nothing checked this while the two floors lived in separate
	# files.
	assert f"python3 (>= {found.group(1)})" in source()["Build-Depends"]
