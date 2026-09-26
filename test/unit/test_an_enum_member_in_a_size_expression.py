"""An enum member is a compile-time constant wherever it appears.

Section 10 permits "integer literals, `const` references" in a size
expression, and `expr._access` folds an enum member alongside them. So
`u8 a[kv.alpha]` built in all four backends -- the whole expression is
constant and never reaches a renderer.

`u8 a[kv.alpha + n]` does reach one, because a field in it means the
expression is emitted rather than folded, and the renderers were handed
`Env.consts` alone. The name was left over: C raised `UnknownName` out of
the emitter, and C++, Python and Rust each declined the member with a note
and emitted no length accessor (26.484).

**The assertion here is a relationship rather than a spelling.** The same
schema written `17 + n` is what the enum version must generate, byte for
byte apart from the include guard, because 17 is what `kv.alpha` is. That
cannot pass by accident and does not pin how the arithmetic is rendered.

The second test is why `named_constants` is filtered rather than merged.
`local_name` is a dotted PATH for a nested member, so `hdr.len` can name a
field and an enum member at once, and the renderers consult constants
before fields -- a straight merge would have handed back the enum's value
for a name that has always resolved to the field. Measured against the
commit before the fix: that schema generates identically.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.layout import solve
from situc.pack import Coverage, pack
from situc.parser import parse_text
from situc.resolve import resolve
from walker.image import load

CC = shutil.which("cc") or shutil.which("gcc")

PREAMBLE = "target buffer;\nendian big;\n\n"

#: `kv.alpha` is 17, so the control below is the same schema with 17 in it.
ENUM_SIZED = PREAMBLE + """enum kv : u8 {
\talpha = 17,
\tbeta  = 18,
}

struct s {
\tu8  n;
\tu8  a[kv.alpha + n];
\tu16 tail;
}
"""

LITERAL_SIZED = PREAMBLE + """enum kv : u8 {
\talpha = 17,
\tbeta  = 18,
}

struct s {
\tu8  n;
\tu8  a[17 + n];
\tu16 tail;
}
"""

#: An enum whose member spells a nested member's path. `hdr.len` is both.
SHADOWED = PREAMBLE + """enum hdr : u8 {
\tlen = 99,
}

struct inner { u8 len; }

struct outer {
\tinner  hdr;
\tu8     body[hdr.len];
\tu16    tail;
}
"""


def _emit(tmp_path: Path, text: str, stem: str, target: str) -> str:
	schema = tmp_path / f"{stem}.situ"
	schema.write_text(text, encoding="ascii")
	out = tmp_path / f"{stem}-{target}"
	done = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", target, "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stdout + done.stderr
	suffix = {"c": "h", "cpp": "hpp", "python": "py", "rust": "rs"}[target]
	return (out / f"{stem}.{suffix}").read_text(encoding="ascii")


@pytest.mark.parametrize("target", ["c", "cpp", "python", "rust"])
def test_an_enum_member_folds_like_the_literal_it_is(
		target: str, tmp_path: Path) -> None:
	"""Generated output must not depend on how the constant was spelled."""
	by_enum    = _emit(tmp_path, ENUM_SIZED, "byenum", target)
	by_literal = _emit(tmp_path, LITERAL_SIZED, "byenum", target)

	assert by_enum == by_literal, (
		f"{target}: `kv.alpha + n` and `17 + n` generate different code, "
		f"and 17 is what `kv.alpha` is")


@pytest.mark.parametrize("target", ["c", "cpp", "python", "rust"])
def test_a_member_path_beats_an_enum_member_of_the_same_spelling(
		target: str, tmp_path: Path) -> None:
	"""`hdr.len` names the field, as it did before enum members were added.

	Not a preference: the renderers ask their constant table before their
	field table, so an unfiltered merge would silently change which value a
	working schema reads. Keeping the field is what makes the change
	additive -- a name that resolved before resolves the same way.
	"""
	source = _emit(tmp_path, SHADOWED, "shadow", target)

	# Not "99 appears nowhere": the enum DECLARES 99, and three backends
	# also emit it in a membership predicate, so a whole-file search says
	# only that an enum was declared. What must not happen is the value
	# reaching the member it would have sized.
	leaked = [line for line in source.splitlines()
	          if "body" in line and "99" in line]
	assert not leaked, (
		f"{target}: the enum member's value reached `body`, where the "
		f"member `hdr.len` should have:\n  " + "\n  ".join(leaked))


@pytest.mark.skipif(CC is None, reason="needs a C compiler")
def test_the_enum_sized_member_compiles(tmp_path: Path) -> None:
	"""The C emitter used to raise rather than emit, so this is the half a
	text comparison cannot make: the header a consumer gets builds."""
	schema = tmp_path / "byenum.situ"
	schema.write_text(ENUM_SIZED, encoding="ascii")
	out = tmp_path / "gen"
	subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "c", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True, check=True)

	probe = tmp_path / "probe.c"
	probe.write_text('#include "byenum.h"\nint main(void){return 0;}\n',
	                 encoding="ascii")
	assert CC is not None
	built = subprocess.run(
		[CC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsyntax-only",
		 f"-I{out}", f"-I{ROOT / 'runtime' / 'c'}", str(probe)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr


# ---------------------------------------------------------------------------
# The packer half
# ---------------------------------------------------------------------------

def _packed(text: str, metadata: bool = True) -> tuple[bytes, Coverage]:
	schema = parse_text(text)
	return pack(schema, resolve(schema, solve(schema)), metadata=metadata)


def test_the_packer_encodes_an_enum_member_in_a_size_program() -> None:
	"""The half 26.484 left open and 26.506 could not reach.

	`Program.compile` looked the name up as a path, found no placement,
	and raised -- which `situc pack --coverage` reported as *`s.a`: no
	placement for `kv.alpha`*. The struct came back with `validatable`
	clear, so both walkers abstained: honest, and less than the four
	backends say about the same schema.

	The fallback is consulted only where the resolver has already failed,
	which is what keeps it additive -- `consts` is asked BEFORE the
	resolver and an enum member after it, so no name that resolves today
	can change meaning. `test_a_member_path_beats_an_enum_member...`
	above is the same guarantee for the renderers; the one below is this
	one's.
	"""
	blob, coverage = _packed(ENUM_SIZED)

	assert not coverage.unencodable, (
		f"the packer still declines it: {coverage.unencodable}")
	assert coverage.expressions >= 1, "no size program was encoded"

	image = load(blob)
	assert all(struct.validatable for struct in image.structs), (
		"the struct is still not validatable, so both walkers abstain")


def test_the_packed_program_reads_the_same_as_the_literal() -> None:
	"""A relationship again, not a value.

	`kv.alpha + n` and `17 + n` must produce the same image, because 17 is
	what `kv.alpha` is -- the same assertion the four backends get above,
	applied to the bytecode. It cannot pass by accident and it pins
	nothing about how the program is encoded.

	Without metadata, so the comparison is the layout and the bytecode
	rather than two different name pools: the enum spelling carries the
	string `kv` and the literal one does not, and that difference is not
	the question.
	"""
	by_enum, _    = _packed(ENUM_SIZED, metadata=False)
	by_literal, _ = _packed(LITERAL_SIZED, metadata=False)

	assert by_enum == by_literal, (
		"`kv.alpha + n` and `17 + n` pack to different images, and 17 is "
		"what `kv.alpha` is")


def test_a_member_path_still_beats_an_enum_member_in_the_packer() -> None:
	"""The additive guarantee, asserted rather than argued.

	`hdr.len` names a nested field and an enum member at once. The
	renderers needed a filter because their constant table is asked
	before their field table; the packer needs none because the order is
	the other way -- but "needs none" is a claim about control flow, and
	this is the case that would catch it being wrong.

	Held to the image the packer produced BEFORE the fallback existed:
	byte for byte, since a schema whose every name already resolved must
	be unaffected by a table consulted only where resolution fails.

	**This one passes against the old packer too, and that is the point.**
	The two tests above fail there, which is what makes them regression
	tests; this pins an invariant the change must not move, so a version
	of it that went red on the old code would be asserting the opposite of
	what it is for.
	"""
	blob, coverage = _packed(SHADOWED, metadata=False)

	assert not coverage.unencodable, coverage.unencodable
	# 99 is the enum member's value and the field is one byte at offset 0.
	# A program that pushed 99 would be sizing `body` from the enum, which
	# is the wrong answer rather than a refused build.
	assert b"\x63\x00\x00\x00\x00\x00\x00\x00" not in blob, (
		"the enum member's value reached the size program, where the "
		"field `hdr.len` should have")
