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

**Qualifying is not a rule that generalises to every emitted call, and the
attempt to make it one is what this docstring exists to stop.** Two sites
next to this one refuse it, for different reasons. `_over_fields` emits
`record(raw_).length()` inside a nested `sealed_gate`, where `record`
constructs a view of the enclosing struct rather than naming a member of the
gate, so `this->` there is a hard error -- `example/dtls` is the schema that
says so. And the two `_value()` leaves beside it are pinned bare by
`test_an_expression_may_name_a_varint[cpp]`; no emitter local ends in
`_value`, so there is nothing there to capture and nothing to fix.

So the rule is not a spelling but a question -- what is the receiver at this
site -- and only a site that has established the member belongs to the
emitting class may name `this`. Two spellings side by side in one generated
expression are not sloppiness.

**What this does not cover**, because the fix does not reach it: the same
capture through `framed()`, which declares `at`, `n` and `have`. A
discriminant named `at` still generates a header that will not compile, and
qualifying its leaf is the edit that breaks `dtls`. That one wants the
emitter's locals renamed to the trailing-underscore form it already uses for
`raw_` and `which_`, which is a sweep with its own proof rather than a line.
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
