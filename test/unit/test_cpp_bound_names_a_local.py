"""A bound naming a sibling must not be captured by the emitter's own local.

C++ is the only backend where this can happen, and that is the whole of why
it happened here. C reaches a member through a prefixed free function taking
the view, Rust spells `self.at()` and Python `self.at` -- all three name the
receiver, so nothing an emitter declares can stand in front of one. A C++
member call may omit the receiver, so a local with the member's name wins
name lookup and the generated header stops compiling.

`check()` declares `std::uint32_t sink`, so `u8 sink;` with
`[max = sink]` beside it emitted `static_cast<std::int64_t>(sink())` inside
that function and gcc answered `'sink' cannot be used as a function`. The
fix qualifies the bound's read, which is always the emitting class's own
member -- `bound_terms.value` checks `is_own_member` before spelling it.

**Qualifying is still not a blanket rule.** The two `_value()` leaves beside
the one below stay bare, and are pinned bare by
`test_an_expression_may_name_a_varint[cpp]`: no emitter local ends in
`_value`, so there is nothing there to capture and nothing to fix. A change
with no failing case behind it is one this tree declines.

So the rule is a question rather than a spelling -- what is the receiver at
this site -- and only a site that has established the member belongs to the
emitting class may name `this`. Two spellings in one generated expression
are not sloppiness.

**The same capture through `framed()` is covered below**, and closing it
took understanding why qualifying that leaf had broken `example/dtls`. It
was never the qualifier: a nested `sealed_gate` has no enclosing object, so
`_in_gate` rewrites a bare member call to one on an object built from the
gate's own view, and `this->at()` came out as `this->record(raw_).at()`.
The rewrite strips the qualifier now -- a bare call and a qualified one are
the same member and must rewrite alike -- and both hazards are closed by
one leaf and one regex rather than by renaming 43 emitter locals.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

CXX     = shutil.which("g++") or shutil.which("clang++")
RUNTIME = ROOT / "runtime"

# `sink` is the local `check()` declares. Nothing in the corpus is called
# that, which is exactly why the capture survived: a fixture is the only way
# to ask the question.
SCHEMA = """target buffer;
endian big;

struct outer {
	u8  sink;
	u8  value  [max = sink];
	u8  body[value];
}
"""

PROBE = """#include "shadow.hpp"

int main(void)
{
	return 0;
}
"""


@pytest.mark.skipif(CXX is None, reason="needs a C++ compiler")
def test_a_bound_naming_a_local_still_reads_the_member(tmp_path: Path) -> None:
	"""Reverting the qualification makes this fail, which is why it is here.

	Measured both ways before it was committed: without `this->` gcc refuses
	the generated header at the `[max = sink]` comparison, and with it the
	header compiles under `-Wall -Wextra -Werror`.
	"""
	schema = tmp_path / "shadow.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	gen = tmp_path / "gen"
	subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "cpp", "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True, check=True)

	header = (gen / "shadow.hpp").read_text(encoding="ascii")
	# The comparison must read the member and not whatever `check` declared.
	assert "this->sink()" in header, header

	source = tmp_path / "probe.cpp"
	source.write_text(PROBE, encoding="ascii")

	assert CXX is not None
	compiled = subprocess.run(
		[CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsyntax-only",
		 f"-I{gen}", f"-I{RUNTIME / 'cpp'}", f"-I{RUNTIME / 'c'}",
		 str(source)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr


#: The locals `framed()` declares. A schema member of any of these names is
#: read there through a bare accessor call, which the local then captures.
#: `n` is absent on purpose: `framed` declares one only for a struct whose
#: shape reaches that branch, and the three below are the ones a minimal
#: schema reaches. The discriminant is what puts the read inside `framed`.
CAPTURING = ("at", "need", "have")

CAPTURE_SCHEMA = """target buffer;
endian big;

struct alpha {{ u16 a; }}
struct beta  {{ u32 b; }}

struct outer {{
	u8   {name};
	u8   len;
	u8   pad[len];
	variant body switch ({name}) {{
		case 0: alpha alpha;
		case 1: beta  beta;
		default: error;
	}}
}}
"""


@pytest.mark.skipif(CXX is None, reason="needs a C++ compiler")
@pytest.mark.parametrize("name", CAPTURING)
def test_a_member_named_like_an_emitter_local_still_compiles(
		name: str, tmp_path: Path) -> None:
	"""Measured failing for all three before the leaf was qualified.

	`framed()` holds `const std::uint32_t have`, `std::uint32_t at` and the
	out-parameter `need`, and the variant's discriminant is read there. Bare,
	gcc answered `'at' cannot be used as a function` -- and for `need`, whose
	type differs, the less obvious `expression cannot be used as a function`.

	The schema keeps the author's spelling, which is decision 0013's rule and
	section 25's: a member named for something the target already uses keeps
	its name and the emitter moves.
	"""
	schema = tmp_path / "shadow.situ"
	schema.write_text(CAPTURE_SCHEMA.format(name=name), encoding="ascii")

	gen = tmp_path / "gen"
	subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "cpp", "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True, check=True)

	source = tmp_path / "probe.cpp"
	source.write_text(PROBE, encoding="ascii")

	assert CXX is not None
	compiled = subprocess.run(
		[CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsyntax-only",
		 f"-I{gen}", f"-I{RUNTIME / 'cpp'}", f"-I{RUNTIME / 'c'}",
		 str(source)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
