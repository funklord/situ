"""The Debian packaging, checked without building a package.

`make deb` is not in the suite: it wants `dpkg-deb`, and a packaging step that
runs on every commit is a packaging step people stop reading. What is checked
here is the part that rots silently -- the claims the control files make about
a tree that keeps changing, and the split between the two packages.

Building and installing them is `make deb-check`, which unpacks both into a
scratch root and compiles a schema through the installed compiler against the
installed runtime. That needs the tools; this does not.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

import situc

ROOT      = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
PACKAGES  = ("situc", "libsitu-dev")


def control(name: str) -> dict[str, str]:
	"""The control file's fields, with continuation lines dropped."""
	fields: dict[str, str] = {}
	for line in (PACKAGING / f"{name}.control").read_text(encoding="ascii").splitlines():
		if line.startswith(" "):
			continue
		key, _, value = line.partition(":")
		fields[key] = value.strip()
	return fields


@pytest.mark.parametrize("name", PACKAGES)
def test_the_control_file_has_what_dpkg_requires(name: str) -> None:
	fields = control(name)
	for required in ("Package", "Version", "Architecture", "Maintainer",
	                 "Description"):
		assert required in fields, f"{name}.control has no {required}"
	assert fields["Package"] == name


@pytest.mark.parametrize("name", PACKAGES)
def test_the_version_is_substituted_rather_than_written(name: str) -> None:
	"""One version, in `situc/__init__.py`.

	A number typed into a control file is a number that disagrees with
	`situc --version` the first time either is bumped alone.
	"""
	assert control(name)["Version"] == "@VERSION@"


def test_the_compiler_package_is_architecture_independent() -> None:
	"""It is Python, and the reason the packages are split.

	One package would have to claim the runtime's architecture, which would
	make the schema compiler uninstallable on every machine it was not built
	on -- for a tool whose purpose is generating code for targets other than
	the build host, the wrong way round.
	"""
	assert control("situc")["Architecture"] == "all"
	assert control("libsitu-dev")["Architecture"] == "@ARCH@"


def test_the_compiler_needs_nothing_but_python() -> None:
	"""Section 24: the toolchain has to vendor into a build environment where
	`pip install` is not on the table."""
	depends = control("situc").get("Depends", "")
	assert depends.startswith("python3")
	assert "," not in depends


def test_the_runtime_package_depends_on_nothing() -> None:
	"""A header and a static library. Depending on the compiler would make a
	target machine install a schema compiler it has no use for."""
	assert "Depends" not in control("libsitu-dev")


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

	# Digits are in the class because a manual page lives in `man1`, and a
	# character class without them silently truncated the path to
	# `/share/man/man` -- a check reporting a directory the rule never names.
	installed = {found.rstrip("/")
	             for found in re.findall(r"\$\(PREFIX\)(/[a-z0-9_/.]+)", rule)
	             if found.rstrip("/")}
	# `/lib` bare is the `find situc -exec install` line, whose destination
	# ends in `{}` and so reads as `$(PREFIX)/lib/` to a regex. It puts the
	# package under `/lib/situc`, which is named beside it.
	assert installed == {
		"/lib", "/lib/situc", "/lib/situc/_runtime/situ_runtime.py",
		"/share/situc/std", "/bin/situc",
		"/include/situ.h", "/lib/libsitu.a",
		"/share/man/man1/situc.1",
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

	assert control("situc")["Depends"] == f"python3 (>= {found.group(1)})"
