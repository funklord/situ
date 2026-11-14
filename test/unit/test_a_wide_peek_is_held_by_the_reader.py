"""An arm whose peeked discriminant is wider than a byte is built (26.1028).

Matroska's `wide_cluster` peeks all eight bytes of an EBML size: eight bytes
of ones (`01 FF..FF`) is a Cluster of unknown size, anything else one whose
size is stated. Its builders were refused -- "a peeked 64-bit
discriminant, and this writer checks the top of an arm's first byte" --
because no byte of the arm says which arm it is alone. The whole message
can: the reader's own getter for the peek is asked once it is written, and
must say this arm's case. C only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, _built,
                                                          _run)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_sized_cluster_with_an_eight_byte_size(tmp_path: Path) -> None:
	"""A Cluster holding one Timestamp element (ID E7, size 1, value 0),
	its size 3 written as an eight-byte EBML number; and the
	unknown-size marker refused as the sized arm (here the arm's own
	`required` refuses it first, its size being 2^56 - 1)."""
	_run(tmp_path, _built(tmp_path, "matroska"), "matroska", """
	static const uint8_t sized[11] = {
		0x01, 0, 0, 0, 0, 0, 0, 0x03, 0xE7, 0x81, 0x00 };
	static const uint8_t open[11] = {
		0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xE7, 0x81, 0x00 };
	uint8_t  buf[32];
	uint32_t wrote = 0;

	if (situ_wide_cluster_build_sized(buf, sizeof buf, sized, 11u, &wrote)
	    != SITU_OK || wrote != 11u || memcmp(buf, sized, 11u) != 0) {
		return 1;
	}
	if (situ_wide_cluster_build_sized(buf, sizeof buf, open, 11u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_bytes_of_the_other_arm_are_refused(tmp_path: Path) -> None:
	"""Where both arms hold the same struct, `01 02` is a valid message of
	the named arm -- and handed to the default arm's builder, only the
	reader's getter can say it is the wrong one."""
	import subprocess
	import sys

	from test_an_arm_sized_by_its_argument_is_written import ROOT

	schema = tmp_path / "wide.situ"
	schema.write_text("""target buffer;
endian big;
struct two {
	u8 x;
	u8 y;
}
struct wide {
	peek u16 k;
	variant b switch (k) {
		case 0x0102: two named;
		default:     two other;
	}
}
""", encoding = "ascii")
	out = tmp_path / "wide"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "drive.c").write_text("""#include "wide_build.h"
int main(void)
{
	static const uint8_t named[2] = { 0x01, 0x02 }, other[2] = { 0x03, 0x04 };
	uint8_t  buf[8];
	uint32_t wrote = 0;

	if (situ_wide_build_named(buf, sizeof buf, named, 2u, &wrote) != SITU_OK)
		return 1;
	if (situ_wide_build_other(buf, sizeof buf, other, 2u, &wrote) != SITU_OK)
		return 2;
	if (situ_wide_build_other(buf, sizeof buf, named, 2u, &wrote)
	    != SITU_ERR_CONSTRAINT)
		return 3;
	if (situ_wide_build_named(buf, sizeof buf, other, 2u, &wrote)
	    != SITU_ERR_CONSTRAINT)
		return 4;
	return 0;
}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{runtime}", f"-I{out}",
		 str(tmp_path / "drive.c"), str(out / "wide.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "drive")])
	assert ran.returncode == 0, f"check {ran.returncode} failed"


def test_python_refuses_the_other_arms_bytes_too(tmp_path: Path) -> None:
	"""26.1033: the other three ask their readers' getters as C does."""
	import subprocess
	import sys

	from test_an_arm_sized_by_its_argument_is_written import ROOT

	schema = tmp_path / "wide.situ"
	schema.write_text("""target buffer;
endian big;
struct two {
	u8 x;
	u8 y;
}
struct wide {
	peek u16 k;
	variant b switch (k) {
		case 0x0102: two named;
		default:     two other;
	}
}
""", encoding = "ascii")
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "python", "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(out / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(out / "run.py").write_text("""import sys
sys.path.insert(0, ".")
import wide_build
from situ_runtime import ConstraintError
for build, data in ((wide_build.build_wide_named, b"\\x01\\x02"),
                    (wide_build.build_wide_other, b"\\x03\\x04"),
                    (wide_build.build_wide_other, b"\\x01\\x02"),
                    (wide_build.build_wide_named, b"\\x03\\x04")):
	try:
		build(bytearray(8), data)
		print("built")
	except ConstraintError:
		print("refused")
""", encoding = "ascii")
	ran = subprocess.run([sys.executable, "-I", "run.py"], cwd = out,
	                     capture_output = True, text = True)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split() == ["built", "built", "refused", "refused"], \
		ran.stdout


def test_the_four_agree_where_only_the_getter_refuses(tmp_path: Path) -> None:
	"""`two` takes any two bytes, so the named arm handed `12 13` is
	refused by the peek check and by nothing else -- in all four alike,
	the agreement test comparing their bytes and verdicts."""
	import fourway
	from situc.codegen import plain_build
	from situc.layout import solve
	from situc.resolve import resolve
	from every_schema import load_schema
	from test_the_four_builders_agree import four_say

	if not fourway.COMPLETE:
		pytest.skip("needs gcc, g++ and rustc")
	schema = tmp_path / "wide.situ"
	schema.write_text("""target buffer;
endian big;
struct two {
	u8 x;
	u8 y;
}
struct wide {
	peek u16 k;
	variant b switch (k) {
		case 0x0102: two named;
		default:     two other;
	}
}
""", encoding = "ascii")
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	units    = plain_build.units(resolved,
	                             plain_build.shape(parsed, resolved))
	said     = four_say(schema, tmp_path, units, [])
	assert "wide.named refused" in said, said
	assert "wide.other 1213" in said, said
