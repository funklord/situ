"""`situc build --deps` -- what a build system has to know (26.596).

fmake asked for this on 2026-10-08 with a reproduction: a consumer kept
`#define SITU_STR_LEN_VALUE_MAX 255u` from a schema that had since said 7,
and the build reported success. Its freshness key held the schema's own
content and not what the schema imported, because there was no way to ask.

So the tests that matter here are not about the file's syntax. They are the
two about make: that a changed import rebuilds the header, and -- the
control -- that without the list it does not. A depfile make ignores looks
exactly like one it reads.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.cli import main

BUFFER = "target buffer;\nendian big;\n\n"

#: fmake's own reproduction, which is why these are its constants.
SHARED = BUFFER + "struct str {\n\tu16 len [max = 255];\n\tu8 v[len];\n}\n"
RECORD = (BUFFER + 'import "shared.situ";\n\n'
          + "struct record {\n\tu8 kind [max = 7];\n\tstr name;\n}\n")


def write(root: Path, name: str, text: str) -> Path:
	path = root / name
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="ascii")
	return path


def build(schema: Path, out: Path, deps: Path | None = None) -> int:
	argv = ["build", str(schema), "--out", str(out)]
	if deps is not None:
		argv += ["--deps", str(deps)]
	return main(argv)


def parts(deps: Path) -> tuple[list[str], list[str], list[str]]:
	"""The targets, the prerequisites, and the empty-ruled paths."""
	lines = deps.read_text(encoding="utf-8").splitlines()
	targets, _, prerequisites = lines[0].partition(": ")
	empty = [line[:-1] for line in lines[1:] if line.endswith(":")]
	return targets.split(), prerequisites.split(), empty


def test_a_schema_with_no_imports_names_itself(tmp_path: Path) -> None:
	"""One prerequisite rather than none. "Read one file" and "was never
	asked" are different facts and an empty list says neither."""
	schema = write(tmp_path, "lone.situ",
	               BUFFER + "struct lone { u8 x; }\n")
	deps = tmp_path / "out" / "lone.d"
	assert build(schema, tmp_path / "out", deps) == 0

	targets, prerequisites, empty = parts(deps)
	assert prerequisites == [str(schema.resolve())]
	assert empty == []
	assert any(name.endswith("lone.h") for name in targets)


def test_an_import_is_a_prerequisite(tmp_path: Path) -> None:
	write(tmp_path, "shared.situ", SHARED)
	schema = write(tmp_path, "record.situ", RECORD)
	deps = tmp_path / "out" / "record.d"
	assert build(schema, tmp_path / "out", deps) == 0

	_, prerequisites, _ = parts(deps)
	assert prerequisites == [str(schema.resolve()),
	                         str((tmp_path / "shared.situ").resolve())]


def test_a_transitive_import_is_a_prerequisite(tmp_path: Path) -> None:
	"""Two levels down. A consumer cannot be asked to know what its
	dependency needs, so neither can its build system."""
	write(tmp_path, "base.situ", BUFFER + "struct base { u8 b; }\n")
	write(tmp_path, "mid.situ",
	      BUFFER + 'import "base.situ";\n\nstruct mid { u8 m; }\n')
	schema = write(tmp_path, "top.situ",
	               BUFFER + 'import "mid.situ";\n\n'
	               + "struct top { base b; mid m; }\n")
	deps = tmp_path / "out" / "top.d"
	assert build(schema, tmp_path / "out", deps) == 0

	_, prerequisites, _ = parts(deps)
	assert set(prerequisites) == {
		str((tmp_path / name).resolve())
		for name in ("top.situ", "mid.situ", "base.situ")}


def test_a_path_below_the_working_directory_is_relative(
		tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Which is the shape a build actually runs in: make reads the file in
	the directory the build runs in, and a sibling in a subdirectory of the
	tree reads better as `sub/shared.situ` than as an absolute path."""
	write(tmp_path, "sub/shared.situ", SHARED)
	write(tmp_path, "record.situ",
	      RECORD.replace('"shared.situ"', '"sub/shared.situ"'))
	monkeypatch.chdir(tmp_path)
	assert build(Path("record.situ"), Path("gen"), Path("gen/record.d")) == 0

	targets, prerequisites, empty = parts(Path("gen/record.d"))
	assert prerequisites == ["record.situ", "sub/shared.situ"]
	assert empty == ["sub/shared.situ"]
	assert targets == ["gen/record.c", "gen/record.h"]


def test_a_std_import_keeps_its_absolute_path(
		tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""It is outside the consumer's tree by construction -- that is what
	`import std` is for -- so there is no relative spelling worth writing,
	and a consumer that cares only about its own tree drops it."""
	write(tmp_path, "uses.situ",
	      BUFFER + 'import std "std/kernels.situ";\n\n'
	      + "struct frame { u8 kind; u16 length; }\n")
	monkeypatch.chdir(tmp_path)
	assert build(Path("uses.situ"), Path("gen"), Path("gen/uses.d")) == 0

	_, prerequisites, _ = parts(Path("gen/uses.d"))
	library = [name for name in prerequisites if name != "uses.situ"]
	assert len(library) == 1
	assert Path(library[0]).is_absolute()
	assert Path(library[0]).name == "kernels.situ"


def test_every_prerequisite_but_the_schema_gets_an_empty_rule(
		tmp_path: Path) -> None:
	"""What `gcc -MP` emits. Without it, deleting an imported schema stops
	make at *No rule to make target* -- naming a file from a rule nobody
	wrote. With it, make runs situc and situc says which import it is."""
	write(tmp_path, "shared.situ", SHARED)
	schema = write(tmp_path, "record.situ", RECORD)
	deps = tmp_path / "out" / "record.d"
	assert build(schema, tmp_path / "out", deps) == 0

	_, prerequisites, empty = parts(deps)
	assert empty == prerequisites[1:]
	assert str(schema.resolve()) not in empty


def test_the_targets_are_files_that_were_written(tmp_path: Path) -> None:
	"""A depfile naming a target that does not exist would tell make the
	build had succeeded, so it is written after the outputs and not before."""
	schema = write(tmp_path, "lone.situ",
	               BUFFER + "struct lone { u8 x; }\n")
	deps = tmp_path / "out" / "lone.d"
	assert build(schema, tmp_path / "out", deps) == 0

	targets, _, _ = parts(deps)
	assert targets
	for name in targets:
		assert Path(name).is_file(), name


def test_a_build_that_fails_writes_no_dependency_list(
		tmp_path: Path) -> None:
	"""Make reads a depfile as a statement about work that happened. One
	left behind by a build that refused would say the opposite."""
	schema = write(tmp_path, "bad.situ", BUFFER + "struct bad { nope x; }\n")
	deps = tmp_path / "out" / "bad.d"
	assert build(schema, tmp_path / "out", deps) == 1
	assert not deps.exists()


def test_a_half_written_build_writes_no_dependency_list(
		tmp_path: Path) -> None:
	"""The reason the list is written after the outputs rather than before,
	and the only arm in which the order is observable: with one output
	unwritable, the generation gets part way and stops.

	A directory standing where a header should go is how that is arranged
	here. It is a stand-in for a full disk or a read-only tree, which are
	the real cases and are not arrangeable in a test.
	"""
	schema = write(tmp_path, "lone.situ",
	               BUFFER + "struct lone { u8 x; }\n")
	out = tmp_path / "out"
	(out / "lone.h").mkdir(parents=True)
	deps = out / "lone.d"

	with pytest.raises(OSError):
		build(schema, out, deps)
	assert not deps.exists()


def test_two_runs_write_the_same_bytes(tmp_path: Path) -> None:
	"""Sorted, so a build system can compare one run against the last."""
	write(tmp_path, "shared.situ", SHARED)
	schema = write(tmp_path, "record.situ", RECORD)
	first  = tmp_path / "out" / "one.d"
	second = tmp_path / "out" / "two.d"
	assert build(schema, tmp_path / "out", first) == 0
	assert build(schema, tmp_path / "out", second) == 0
	assert first.read_bytes() == second.read_bytes()


def test_a_space_in_a_path_is_escaped(tmp_path: Path) -> None:
	"""Make splits a rule on whitespace, so a path holding any has to say
	so -- and a build directory with a space in it is somebody's home."""
	write(tmp_path, "a dir/shared.situ", SHARED)
	schema = write(tmp_path, "record.situ",
	               RECORD.replace('"shared.situ"', '"a dir/shared.situ"'))
	deps = tmp_path / "out" / "record.d"
	assert build(schema, tmp_path / "out", deps) == 0

	text = deps.read_text(encoding="utf-8")
	assert "a\\ dir/shared.situ" in text


def test_without_the_option_nothing_is_written(tmp_path: Path) -> None:
	schema = write(tmp_path, "lone.situ",
	               BUFFER + "struct lone { u8 x; }\n")
	assert build(schema, tmp_path / "out") == 0
	assert list((tmp_path / "out").glob("*.d")) == []


@pytest.mark.skipif(shutil.which("make") is None, reason="no make")
def test_make_rebuilds_a_header_when_an_import_changes(
		tmp_path: Path) -> None:
	"""fmake's reproduction, driven by plain make, with the control it
	needs: the same edit against a makefile that does not read the list
	must leave the stale constant in place. Without that arm, a rebuild
	proves only that something was out of date.
	"""
	situc = Path(__file__).resolve().parents[2] / "bin" / "situc"
	recipe = (f"\tpython3 {situc} build $< --out gen --deps gen/record.d\n")
	write(tmp_path, "Makefile",
	      "all: gen/record.h\n\ngen/record.h: record.situ\n" + recipe
	      + "\n-include gen/record.d\n")
	write(tmp_path, "Makefile.control",
	      "all: gen/record.h\n\ngen/record.h: record.situ\n" + recipe)
	write(tmp_path, "shared.situ", SHARED)
	write(tmp_path, "record.situ", RECORD)

	def run(makefile: str) -> str:
		done = subprocess.run(["make", "-f", makefile], cwd=tmp_path,
		                      capture_output=True, text=True, timeout=300)
		assert done.returncode == 0, done.stdout + done.stderr
		return done.stdout + done.stderr

	def constant() -> str:
		text = (tmp_path / "gen" / "record.h").read_text(encoding="ascii")
		line = [one for one in text.splitlines()
		        if "STR_LEN_VALUE_MAX" in one]
		assert len(line) == 1, line
		return line[0].split()[-1]

	run("Makefile")
	assert constant() == "255u"

	# The edit fmake made, and the control arm: the schema named by the
	# rule has not changed, so make has nothing to go on without the list.
	(tmp_path / "shared.situ").write_text(
		SHARED.replace("max = 255", "max = 7"), encoding="ascii")
	assert "Nothing to be done" in run("Makefile.control")
	assert constant() == "255u"

	run("Makefile")
	assert constant() == "7u"
