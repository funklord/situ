"""A `pad_to(n)` inside a variant arm is read by nobody (26.848).

A pad MEMBER gets the honest form in all four backends:

    # arms.<pad> is reserved: no accessor, and
    # validate() holds it to the pattern the schema declares.

The ARM form got a scalar accessor that read ONE byte of the span while
bounds-checking all of it -- `pad_to(4)` after one byte reads `raw[1]` of
three. And `situc diff` PROBED it, in all four languages, so the four-way
differential compared that accessor and the four agreed: on one byte of
three, which is agreement on a wrong answer and the one thing a
differential cannot report.

A pad is a reserved RUN that nothing is meant to read, and the extent
arithmetic after the arm does not need one either -- a pad's length comes
from `pad_to`, not from the data. So there is no accessor to fix; there is
an accessor not to emit.

**The run form of the CHECK is a separate piece** and 26.848 records it:
the packer's kind 13 scans the aligned span, and neither walker renders a
span kind for an arm, so all six agree in saying nothing about a pad arm's
bytes meanwhile.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fourway						# noqa: E402

from collections.abc import Callable			# noqa: E402
from typing import Protocol				# noqa: E402

from situc import ast					# noqa: E402
from situc.resolve import ResolvedSchema		# noqa: E402

from situc.codegen.c.emit import generate as gen_c	# noqa: E402
from situc.codegen.cpp.emit import generate as gen_cpp	# noqa: E402
from situc.codegen.python.emit import generate as gen_py	# noqa: E402
from situc.codegen.rust.emit import generate as gen_rs	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

SCHEMA = """target buffer;
endian big;

struct arms {
	u8 kind;
	variant body switch (kind) {
		case 1:  pad_to(4);
		default: u8  other;
	}
	u8 tail;
}
"""

#: What an accessor for the pad would be called, in each backend's spelling.
#: The arm is anonymous, so the name comes from the placement -- `<pad>`
#: sanitised -- which is what `_arm_member` interpolates.
NAMED = ("body_pad", "situ_arms_body_pad_get")

class Emitted(Protocol):
	"""What every backend's `generate` returns: files, whatever else.

	Spelled out for `test_invariants`' reason, which applies here
	unchanged: the four return four different `Generated` types, and a
	bare dict of them infers as `object`, which typechecks nothing.
	"""

	def files(self) -> dict[str, str]: ...


Emit = Callable[[ast.Schema, ResolvedSchema, str], Emitted]

BACKENDS: dict[str, Emit] = {
	"c": gen_c, "cpp": gen_cpp, "python": gen_py, "rust": gen_rs,
}


def _sources() -> dict[str, str]:
	parsed = parse_text(SCHEMA)
	resolved = resolve(parsed, solve(parsed))
	return {name: "\n".join(gen(parsed, resolved, "unit").files().values())
	        for name, gen in BACKENDS.items()}


def _without_comments(target: str, source: str) -> str:
	"""The generated text with its comment lines dropped.

	A list of definition SPELLINGS is the obvious detector and it is one
	that inherits a backend's habits: written out, it missed C++, whose
	accessor reads `[[nodiscard]] ::situ::rt::err body_pad(std::uint8_t &)`
	and matched none of the seven forms. Three detectors were wrong that
	way in one night. So the question is asked the way it is meant -- does
	the name appear anywhere a compiler would read -- and the decline,
	which does name the member, is in a comment.
	"""
	out = []
	for line in source.splitlines():
		bare = line.strip()
		if target == "python" and bare.startswith(("#", '"""', "'")):
			continue
		if target == "rust" and bare.startswith(("//", "///")):
			continue
		if target in ("c", "cpp") and bare.startswith(("/*", "*", "//",
		                                               "/**")):
			continue
		out.append(line)
	return "\n".join(out)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_offers_an_accessor_for_a_pad_arm(target: str) -> None:
	code = _without_comments(target, _sources()[target])
	for name in NAMED:
		assert name not in code, (
			target, name,
			[one for one in code.splitlines() if name in one][:3])


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_the_decline_is_named_rather_than_silent(target: str) -> None:
	"""A missing accessor nobody mentions is indistinguishable from an
	emitter that forgot, which is why every other decline in these files
	says so in the generated text."""
	source = _sources()[target]
	assert "<pad>" in source, (target, "the pad is not mentioned at all")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_drivers_build_without_it(tmp_path: Path) -> None:
	"""`situc diff` has to agree about the decline, or its driver names a
	function nothing defines -- which is f2be5841's failure in another
	construct, and the reason that one could not be compared at all."""
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command

	for name, argv in command.items():
		answers = fourway.answers(argv, b"\x01\x00\x00\x00\x5A", tmp_path)
		assert "-- arms" in answers, (name, answers)
		assert "body_pad" not in answers, (name, answers)
