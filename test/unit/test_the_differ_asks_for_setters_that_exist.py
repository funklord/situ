"""The differ's write pass asks for setters the backends actually emit.

`differ.writes` decides which members the four-way drivers write, and each
backend's emitter decides which setters it defines. **Two places answering
one question**, which is the shape five of this week's faults had, and it
went wrong here in the direction that is hardest to see: the differ asked
C for `situ_au_e_block_set`, a setter for `entry`'s `parameter` reached
through a transparent block, and C obliged by storing at the nested
member's base. A parameter occupies no bytes, so that write landed on the
first byte of the member after it.

Both halves carried the same omission, so the drivers agreed and the
differential was green. 26.818 stopped the emitter writing it and 26.820
stopped the differ asking; what makes the pair stay closed is this, which
compares the two sets rather than trusting either.

It reads the C header because C is the backend whose setters are named
functions a compiler must find. The other three carry the member on a
view, so a missing setter there is an attribute error at the call rather
than a link failure -- the asymmetry 26.423 records, and the reason this
asks C.

**What it does NOT cover, measured rather than assumed.** Reverting
26.820's carve-out leaves this green, because the construct that produced
the fault -- an argued child inside a transparent block -- is declared in
`test_a_block_s_member_takes_its_arguments.py` and not in the corpus. That
file's `test_six_readers_agree` is what caught it and is where it stays
caught; this is the cheaper, earlier check for the CLASS over the 46
schemas that do exist. Stated because a gate that implies coverage it
lacks is worth less than one whose edge is written down: the sweep below
is only ever as wide as the corpus.
"""

from __future__ import annotations

import re

import pytest

from every_schema import SCHEMAS, ids, load_schema
from situc.codegen import differ
from situc.codegen.c import generate as generate_c
from situc.codegen.c.names import ident
from situc.layout import solve
from situc.resolve import resolve

#: `static inline void situ_<struct>_<local>_set(` and the error-returning
#: form beside it. The name is what a driver writes, so the name is what
#: this compares.
DEFINED = re.compile(r"static inline \w+[\s*]+(situ_\w+_set)\(")


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_write_the_differ_asks_has_a_setter(path) -> None:  # type: ignore[no-untyped-def]
	"""A driver that calls a function nothing defines does not compile, so
	this is a build failure caught before the build."""
	schema   = load_schema(path)
	resolved = resolve(schema, solve(schema))
	defined  = set(DEFINED.findall(generate_c(schema, resolved, "situ").header))

	# `structs_of` and `ident`, which are what the driver itself uses: the
	# driver covers fewer structs than the schema declares -- an MMIO
	# register's accessors take a `volatile uint8_t *` and are not in the
	# write pass at all -- and it builds the name with `ident`. Written
	# with `resolved.structs.values()` and an f-string first, which
	# reported every register in `example/register` as missing a setter
	# that was never meant to exist. A test that asks a different question
	# from the thing it is checking is the fault it is checking for.
	missing = []
	for struct in differ.structs_of(resolved):
		for ask in differ.writes(struct, resolved.structs):
			if ask.probe not in (differ.Probe.WRITE, differ.Probe.COVERED):
				continue
			name = ident("situ", struct.name, ask.local, "set")
			if name not in defined:
				missing.append(f"{struct.name}.{ask.local} -> {name}")
	assert not missing, (
		f"{path.name}: the differ asks the drivers to write these and C "
		f"defines no setter for them, so the C driver will not compile: "
		f"{missing}. Either the emitter should emit one or the differ "
		f"should not ask -- and which it is depends on whether the member "
		f"has a byte to write (26.818, 26.820)")


def test_the_comparison_is_not_vacuous() -> None:
	"""Two empty sets are equal, which the test above cannot tell.

	It needs the differ to be asking for writes at all, and the header to
	be defining setters at all, over a schema that has both.
	"""
	schema   = load_schema(next(p for p in SCHEMAS if p.name == "edges.situ"))
	resolved = resolve(schema, solve(schema))
	defined  = DEFINED.findall(generate_c(schema, resolved, "situ").header)
	asked    = [ask for struct in differ.structs_of(resolved)
	            for ask in differ.writes(struct, resolved.structs)
	            if ask.probe in (differ.Probe.WRITE, differ.Probe.COVERED)]
	assert len(defined) > 50, f"only {len(defined)} setters in the header"
	assert len(asked) > 20, f"the differ asks only {len(asked)} writes"
