"""The CMake entry point, which section 24 promised and nothing had written.

"Both CMake and GNU Make, maintained in parallel, as separate and independently
usable entry points" is what the section said, and `git log --all` had never
seen a `CMakeLists.txt`. So the claim was a promise, and the remedy section 0
prescribes is a check rather than a better promise: these run CMake.

Three things are worth checking and they are different things. That the runtime
builds from the top level and installs where `make install` puts it. That the
runtime builds *on its own*, which is section 24's rule for every sub-project
here -- a consumer vendoring `runtime/c` gets a working build with nothing from
above it. And that `situ_generate()` works from a consuming project, which is
the part a user actually wants from a build system they already use: a schema
becomes a library target, and editing the schema rebuilds what reads it.

Writing them found the bug the file could not have: `find_package(Python3
3.11 ...)` found a real 3.11, and `situc` did not run on it (see
`test_every_module_parses_at_the_declared_floor`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT

CMAKE = shutil.which("cmake")

pytestmark = pytest.mark.skipif(CMAKE is None, reason="no cmake")


def cmake(*arguments: str) -> subprocess.CompletedProcess[str]:
	assert CMAKE is not None
	result = subprocess.run([CMAKE, *arguments], capture_output=True, text=True)
	assert result.returncode == 0, result.stdout + result.stderr
	return result


def test_the_top_level_build_makes_the_runtime_and_installs_it(
		tmp_path: Path) -> None:
	"""The same four things `make install` puts under a prefix: the launcher,
	the package, the header and the archive."""
	build  = tmp_path / "build"
	prefix = tmp_path / "prefix"

	cmake("-S", str(ROOT), "-B", str(build),
	      f"-DCMAKE_INSTALL_PREFIX={prefix}")
	cmake("--build", str(build))
	cmake("--install", str(build))

	assert (prefix / "lib" / "libsitu.a").is_file()
	assert (prefix / "include" / "situ.h").is_file()
	assert (prefix / "bin" / "situc").is_file()
	assert (prefix / "lib" / "situc" / "cli.py").is_file()
	assert (prefix / "share" / "situc" / "std" / "codecs.situ").is_file()


@pytest.mark.skipif(shutil.which("make") is None, reason="no make")
def test_the_two_installers_put_the_same_files_there(tmp_path: Path) -> None:
	"""Section 24 says CMake and GNU Make are maintained in parallel as
	independently usable entry points, which makes their install lists two
	descriptions of one thing -- and this repository's recurring finding is
	that two such lists drift (26.31, 26.34).

	The lists agree today. That is the moment to check it: a module added to
	`situc/` reaches both by glob, and anything else -- a new standard schema
	directory, a second header -- reaches whichever one its author edited.
	"""
	staged = tmp_path / "make"
	prefix = tmp_path / "cmake"
	build  = tmp_path / "build"

	subprocess.run(
		["make", "-C", str(ROOT), "install",
		 f"DESTDIR={staged}", "PREFIX=/usr/local",
		 f"BUILD_ROOT={tmp_path / 'obj'}"],
		capture_output=True, text=True, check=True)

	cmake("-S", str(ROOT), "-B", str(build), f"-DCMAKE_INSTALL_PREFIX={prefix}")
	cmake("--build", str(build))
	cmake("--install", str(build))

	def listing(root: Path) -> set[str]:
		return {str(path.relative_to(root))
		        for path in root.rglob("*") if path.is_file()}

	from_make  = listing(staged / "usr" / "local")
	from_cmake = listing(prefix)

	assert from_make == from_cmake, (
		"the two installers disagree:\n"
		f"  only `make install`: {sorted(from_make - from_cmake)}\n"
		f"  only CMake:         {sorted(from_cmake - from_make)}")
	assert "bin/situc" in from_make		# and neither is empty


def test_the_runtime_builds_on_its_own(tmp_path: Path) -> None:
	"""Section 24's rule for every sub-project: self-contained, with the parent
	injecting through cache variables rather than through include files. A
	consumer that vendors `runtime/c` and nothing else gets a working build."""
	build = tmp_path / "runtime"

	cmake("-S", str(ROOT / "runtime" / "c"), "-B", str(build))
	cmake("--build", str(build))

	assert (build / "libsitu.a").is_file()


CONSUMER = """\
cmake_minimum_required(VERSION 3.16)
project(consumer C)

add_subdirectory("{root}" situ)
situ_generate(udp_schema SCHEMA "{root}/examples/udp/udp.situ" TARGET c)

add_executable(app main.c)
target_link_libraries(app PRIVATE udp_schema)
"""

MAIN = """\
#include "udp.h"

int main(void)
{
	uint8_t raw[8] = { 0x04, 0xd2, 0x00, 0x50, 0x00, 0x18, 0x00, 0x00 };
	situ_view_t view = { raw, sizeof raw, 0 };

	if (situ_udp_header_source_port_get(view) != 1234u) { return 1; }
	if (situ_udp_header_length_get(view) != 24u)        { return 2; }
	return 0;
}
"""


def test_a_consuming_project_generates_and_links(tmp_path: Path) -> None:
	"""`situ_generate()` end to end: a schema in, a library target out, an
	executable that reads real bytes through it.

	It runs the program, because a generated header that compiles and returns
	the wrong number is the failure this project spends its pages on."""
	source = tmp_path / "src"
	source.mkdir()
	(source / "CMakeLists.txt").write_text(CONSUMER.format(root=ROOT),
	                                       encoding="ascii")
	(source / "main.c").write_text(MAIN, encoding="ascii")

	build = tmp_path / "build"
	cmake("-S", str(source), "-B", str(build))
	cmake("--build", str(build))

	assert subprocess.run([str(build / "app")]).returncode == 0


def test_the_schema_is_a_dependency_of_what_reads_it(tmp_path: Path) -> None:
	"""Editing the schema regenerates. Without the `DEPENDS`, a consumer keeps
	building against the accessors of a schema they have already changed --
	which is the failure mode of checking generated code in, arriving in the
	build system that was supposed to avoid it."""
	source = tmp_path / "src"
	source.mkdir()
	schema = tmp_path / "one.situ"
	schema.write_text("target buffer;\nendian big;\n"
	                  "struct one { u16 a; }\n", encoding="ascii")
	(source / "CMakeLists.txt").write_text(f"""\
cmake_minimum_required(VERSION 3.16)
project(consumer C)
add_subdirectory("{ROOT}" situ)
situ_generate(one_schema SCHEMA "{schema}" TARGET c)
""", encoding="ascii")

	build = tmp_path / "build"
	cmake("-S", str(source), "-B", str(build))
	cmake("--build", str(build))

	generated = build / "situ" / "one_schema" / "one.h"
	assert "situ_one_a_get" in generated.read_text(encoding="ascii")
	assert "situ_one_b_get" not in generated.read_text(encoding="ascii")

	schema.write_text("target buffer;\nendian big;\n"
	                  "struct one { u16 a; u16 b; }\n", encoding="ascii")
	cmake("--build", str(build))

	assert "situ_one_b_get" in generated.read_text(encoding="ascii")
