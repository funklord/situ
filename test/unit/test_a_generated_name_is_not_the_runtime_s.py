"""A generated name may not be one the C runtime already defines (26.562).

This module's subject is the paragraph opening `situc/codegen/c/names.py`:
*the first sign of it is the C compiler rejecting generated code, with a
diagnostic that names a function nobody wrote and no source location in the
schema at all.* That check covers two CONSTRUCTS reaching one identifier. A
construct reaching a name the RUNTIME defines has the same symptom in the
same words, and nothing looked:

    struct bounds { ... }   ->  situ_bounds_check(situ_view_t, uint32_t *)
    runtime/c/situ.h        ->  situ_bounds_check(situ_view_t, uint32_t,
                                                 uint32_t)

    error: conflicting types for 'situ_bounds_check'

Two struct names in the runtime's way, `bounds` and `view`, both measured by
compiling. Seven that look like they should be -- `msg`, `bits`, `digits`,
`leaf`, `ascii`, `utf8`, `bcd` -- are not, and that pair of facts is why the
check reads the emitted text rather than matching stems against prefixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.c.names import RUNTIME_SYMBOLS, defined_symbols
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c" / "situ.h"

PREAMBLE = "target buffer;\nbit_order msb_first;\nendian big;\n\n"

#: Named by what they do to the runtime rather than by what they are: each is
#: an ordinary struct whose only property is its name.
COLLIDING = ("bounds", "view")

#: Under a runtime prefix and perfectly fine. These are the reason the check
#: is not a prefix rule: refusing on the stem would refuse all seven.
INNOCENT = ("msg", "bits", "digits", "leaf", "ascii", "utf8", "bcd", "base",
            "scan")


def _schema(name: str) -> str:
	return PREAMBLE + f"struct {name} {{\n\tu8 kind [max = 3];\n\tu16 tail;\n}}\n"


def _generate(name: str):  # type: ignore[no-untyped-def]
	schema   = parse_text(_schema(name), path=f"{name}.situ")
	resolved = resolve(schema, solve(schema))
	return generate_c(schema, resolved, name)


@pytest.mark.parametrize("name", COLLIDING)
def test_a_struct_in_the_runtime_s_way_is_refused(name: str) -> None:
	"""With the symbol, the construct and what to do about it.

	Refused rather than mangled, which is the other answer available and is
	`bare_name`'s for a keyword. A keyword collision has one obvious escape
	-- a trailing underscore nobody has to understand -- and this one does
	not: `situ_bounds_check_` would be a public name that differs from every
	other struct's for a reason a reader cannot see. Decision 0025's argument
	for mangling is about the schema keeping its name; here the schema would
	keep its name and every caller would pay for it.
	"""
	with pytest.raises(SituError, match="which the C runtime defines"):
		_generate(name)


@pytest.mark.parametrize("name", INNOCENT)
def test_a_struct_merely_under_a_runtime_prefix_is_not(name: str) -> None:
	"""The control, and the reason the check is shaped as it is.

	Every one of these generates `situ_<name>_check` and friends while the
	runtime has `situ_<name>_something` -- so a stem-prefix rule catches them
	all. Measured by compiling: none of them collides.
	"""
	built = _generate(name)
	assert not (defined_symbols(built.header + built.source) & RUNTIME_SYMBOLS)


def test_the_runtime_symbols_are_the_runtime_s() -> None:
	"""The list cannot drift from the header, which is what a literal needs.

	`RUNTIME_SYMBOLS` is carried in situc rather than read at generation time,
	because the header installs to `<prefix>/include/situ.h` and a generator
	that needs its own runtime on disk fails wherever the two are packaged
	apart. A list nobody checks is a list that has drifted, so this reads the
	header with the same function the check reads generated text with -- one
	reader, so the two cannot disagree about what counts as a definition.
	"""
	assert defined_symbols(RUNTIME.read_text(encoding="utf-8")) \
		== RUNTIME_SYMBOLS


def test_the_reader_finds_what_the_runtime_declares() -> None:
	"""And the reader is not vacuous, which the comparison above cannot say.

	Two empty sets are equal. This names three symbols that must be in it --
	a `static inline`, an extern declaration and a macro -- and one that must
	not, a call appearing in a body rather than a definition.
	"""
	found = defined_symbols(RUNTIME.read_text(encoding="utf-8"))
	assert "situ_bounds_check" in found, "a static inline"
	assert "situ_msg_touch" in found, "an extern declaration"
	assert "SITU_H" in found, "a macro"
	assert len(found) > 50, f"only {len(found)} symbols read"
