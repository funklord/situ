"""No accessor for a sealed interior takes the plain view (26.571).

Section 14.3's stage gate is a type: `situ_<region>_t` has no public
constructor but `situ_<region>_open(view, verified, &out)`, so an accessor
that takes one cannot be called before the tag has verified. An accessor
that takes `situ_view_t` can be called with the view directly, and the
gate is then decoration.

Three member kinds were in that position and every other kind in the
backend was not:

	situ_m_sealed_v_get(situ_view_t view, uint64_t *out)      a varint
	situ_n_sealed_line_ptr(situ_view_t view)                  a scanned run
	situ_o_sealed_d_value(situ_view_t view)                   a text number

	situ_m_sealed_plain_scalar_get(situ_m_sealed_t gate)      every other kind

`line_ptr` is the sharp one: a pointer into the sealed plaintext with no
token at all. Measured against the tree before the fix, so it is older
than the change that found it, and latent only because no schema in this
repository has a self-delimiting member inside a coded region -- counted,
and it is zero.

The varint family now takes the gate. The two scanned families are
declined, which is what C++ already does, because their accessors thread
the view through a scan and an ungated pointer is not a thing to leave
standing while that is rewritten.

The assertion below is over the WHOLE emitted header rather than over
those three names: a kind that arrives later and reaches for the plain
view fails here without anybody adding it to a list.

C only, and not because it was the easiest to sweep. C is the one backend
whose interior accessors are free functions, so the gate can only be a
parameter and omitting it is expressible. C++ puts them in a nested class
with a private constructor, Python on a `Gate` subclass, Rust on a struct
whose field is private to the module -- there is no form of those in which
the member is reachable and the gate is not held. A sweep of them would
assert that a class exists, which is a presence check dressed as a
guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = """target buffer;
bit_order msb_first;
endian big;

codec aead {
	tag_bytes   = 16;
	nonce_bytes = 12;
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}

impl aead extern "my_aead";

varint_type vlen {
	encoding  = be128;
	max_bits  = 64;
	max_bytes = 9;
}

enum kind : u8 { one = 1, two = 2 }

"""

#: Every member kind this backend will put inside a sealed region, so that
#: the sweep below is over a population rather than over the three names
#: the fault was found under.
INTERIORS = {
	"scalar":        "u16 plain_scalar;",
	"enum":          "kind an_enum;",
	"byte_run":      "u8 run[4];",
	"wide_run":      "u16 wide[2];",
	"outer_sized":   "u8 sized[len];",
	"fixed_point":   "q16_16 fixed_point;",
	"packed_bcd":    "bcd4 packed;",
	"nul_text":      "u8 text[8] [nul_terminated];",
	"varint":        "vlen v;",
	"delimited_run": 'u8 line[] until "\\r\\n";',
	"text_number":   'decimal u16 d until "\\r\\n" max 8;',
}

#: What a C accessor for a sealed interior may take as its first argument.
#: `_open` is the factory and is the one function that must take the view,
#: since it is what turns one into a gate.
FACTORY = "open"


def _schema(interior: str) -> str:
	return (PREAMBLE + "struct m {\n\tu8 len;\n\tu8 nonce[12];\n\n"
	        f"\tsealed(aead, nonce = nonce) {{\n\t\t{interior}\n\t}}\n\n"
	        "\ttag u8[16];\n}\n")


def _c_header(interior: str) -> str:
	schema   = parse_text(_schema(interior), path="m.situ")
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "m")
	return built.header + "\n" + built.source


#: A definition of a function whose name says it belongs to the sealed
#: region, with whatever it takes first.
DEFINED = re.compile(
	r"^(?:static inline\s+)?[A-Za-z_][\w \t*]*?"
	r"\b(situ_m_sealed_\w+)\(([^,)]*)", re.MULTILINE)


@pytest.mark.parametrize("label,interior", sorted(INTERIORS.items()))
def test_no_c_accessor_for_a_sealed_interior_takes_a_view(label: str,
		interior: str) -> None:
	"""The population assertion, and `_open` is the named exception."""
	taking_view = [
		name for name, first in DEFINED.findall(_c_header(interior))
		if "situ_view_t" in first and not name.endswith(f"_{FACTORY}")]
	assert not taking_view, (
		f"{taking_view} reach the sealed interior without the gate type, so "
		f"section 14.3's token is decoration for them")


def test_the_sweep_finds_the_factory_it_excludes() -> None:
	"""The control: the pattern has to match something, and `_open` is what
	it is allowed to match. Without this the assertion above passes on a
	regex that matches nothing at all."""
	found = {name for name, _ in DEFINED.findall(_c_header("u16 w;"))}
	assert "situ_m_sealed_open" in found, found
	assert "situ_m_sealed_w_get" in found, found
