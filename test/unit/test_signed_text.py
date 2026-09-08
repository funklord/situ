"""A signed text number: one optional `-`, then digits, and nothing else.

The construct existed in one direction only. `decimal u16 count until "\\r\\n"`
was a magnitude, and a schema that wrote `decimal i32 n until ","` got a
reader that parsed the digits into an unsigned accumulator and range-checked
it against `0..2^32-1` -- so a field the schema DECLARED signed could not
hold a negative number, and nothing anywhere said so. The type was carried
into the map, into the wire signature and into the getter's return type, and
was ignored by the one thing that reads the bytes.

Three decisions make the grammar, and each of them is a canonicality
question rather than a parsing one (8.6.2):

  * **No `+`.** With it, `+5` and `5` are two spellings of one value, and a
    byte-exact layout that admits both cannot say which one it will write.
  * **No `-0`.** Zero written twice, for the same reason.
  * **No fixed width.** A sign costs a byte, so `decimal i32 n[6]` cannot
    both always carry the sign and be six bytes for every value. situc
    refuses the combination rather than choosing, because either choice is
    a canonicality the schema did not state. That refusal is what makes the
    signed form delimited by construction, which is in turn why its domain
    is the type's own rather than a digit count's.

The floor is derived from the ceiling everywhere it is needed rather than
carried beside it: in two's complement `min` is `-(max + 1)`, so the packed
check row keeps one field and there is no second number to be wrong. Both
walkers do that derivation, and this file is where the derivation is held to
the four backends' arithmetic.
"""

from __future__ import annotations

import subprocess

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve
from walker import report
from pathlib import Path
from typing import Callable

from walker.image import Image, load as load_image

from test_codegen_python import load as load_module
from test_walker_c import COMPILER, VALUES, _drive, c_verdict
from test_codegen_c import HOST_CC, RUNTIME, WARNINGS, emit as emit_c
from test_codegen_cpp import HOST_CXX
from test_codegen_cpp import RUNTIME as CPP_RUNTIME
from test_codegen_cpp import WARNINGS as CXX_WARNINGS
from test_codegen_cpp import emit as emit_cpp
from test_codegen_rust import RUSTC, build as build_rust

PREAMBLE = "target buffer;\nendian big;\n"

#: The smallest schema in which a sign can be right or wrong. `i16` rather
#: than `i32` so the boundary spellings are short enough to read.
BODY = 'struct s {\n\tdecimal  i16  n  until ",";\n\tu8  tail;\n}\n'

#: value, or the exception name, for each spelling. Every row is a decision
#: above rather than an arbitrary case: the two extremes, the two rejected
#: spellings, the empty field, a sign with nothing after it, and a `-` in
#: the middle where it is simply not a digit.
CASES = [
	(b"0,x",       0),
	(b"42,x",      42),
	(b"-42,x",     -42),
	(b"007,x",     7),
	(b"32767,x",   32767),
	(b"-32768,x",  -32768),
	(b"32768,x",   "ConstraintError"),
	(b"-32769,x",  "ConstraintError"),
	(b"-0,x",      "ConstraintError"),
	(b"+42,x",     "ConstraintError"),
	(b"-,x",       "ConstraintError"),
	(b",x",        "ConstraintError"),
	(b"1-2,x",     "ConstraintError"),
]


#: The two spellings the walker READS and the schema refuses. A walker's
#: read answers what a field holds; the domain is a `validate` question, and
#: conflating them would have this file asserting that a number cannot be
#: read because it is out of range -- which is not what either walker does,
#: for the reason `parse_digits` states in its own comment.
OUT_OF_DOMAIN = {b"32768,x": 32768, b"-32769,x": -32769}


def image_of(text: str) -> Image:
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	return load_image(pack(schema, resolved, metadata=True)[0])


# -- what the language admits ----------------------------------------------


def test_a_fixed_width_signed_text_number_is_refused() -> None:
	"""The refusal that makes the signed form delimited by construction.

	Its absence is what let the whole defect exist: the type guard lived in
	the DELIMITER check, so it saw `decimal i32 n until ","` and never
	`decimal i32 n[6]`, and the fixed-width form is half the construct.
	"""
	with pytest.raises(SituError) as raised:
		parse_text(PREAMBLE + "struct b { decimal i32 n[6]; }\n")

	assert "signed text number with a fixed width" in str(raised.value)
	assert any("a sign costs a byte" in note for note in raised.value.diagnostic.notes)


def test_the_delimited_signed_form_is_accepted() -> None:
	"""The other half of the same guard, which a refusal alone would not
	show: a check that refused both forms would pass the test above and be
	wrong about the construct."""
	parse_text(PREAMBLE + BODY)


def test_an_unsigned_fixed_width_text_number_is_still_accepted() -> None:
	"""The population the refusal must NOT reach. `decimal u32 ino[8]` is
	cpio's whole header, and a guard keyed on `fixed` rather than on
	`signed and fixed` would have taken it."""
	parse_text(PREAMBLE + "struct b { decimal u32 ino[8]; }\n")


# -- what the bytes mean ---------------------------------------------------


@pytest.mark.parametrize("raw,want", CASES, ids=[c[0].decode() for c in CASES])
def test_the_python_backend_reads_the_grammar(
		tmp_path: Path, raw: bytes, want: int | str) -> None:
	module = load_module(tmp_path, BODY, PREAMBLE)
	view   = module.s.at(module.Message(bytearray(raw)), 0, len(raw))

	if isinstance(want, str):
		with pytest.raises(getattr(module, want)):
			view.n
	else:
		assert view.n == want


@pytest.mark.parametrize("raw,want", CASES, ids=[c[0].decode() for c in CASES])
def test_the_walker_agrees_about_the_verdict(
		raw: bytes, want: int | str) -> None:
	"""The fifth column, which reaches the number by another road entirely:
	the packed image's check rows rather than a generated accessor. It is
	the only thing here that exercises the floor being DERIVED from the
	ceiling -- the backends carry both numbers, and the image carries one.
	"""
	listing = report.listing(image_of(PREAMBLE + BODY), raw)
	verdict = next(line for line in listing.splitlines()
	               if line.startswith("validate "))

	assert verdict == ("validate 2" if isinstance(want, str)
	                   else "validate 0"), (
		f"{raw!r}: the walker says {verdict!r} and the backends say "
		f"{want!r}")


def test_the_non_failing_read_signs_too(tmp_path: Path) -> None:
	"""`_value` is the read the offset arithmetic uses, where nothing may
	raise. It returned a magnitude while the fallible getter returned a
	negative number, which is the disagreement that matters: every offset
	downstream of a signed text number would have been computed from a
	value no accessor would hand a caller."""
	module = load_module(tmp_path, BODY, PREAMBLE)
	message = bytearray(b"-42,x")
	view    = module.s.at(module.Message(message), 0, len(message))

	assert view.n_value == view.n == -42


# -- the spelling, which the sign is part of -------------------------------


def test_minimal_skips_the_sign() -> None:
	"""`-042` is non-minimal for the same reason `042` is. Held against the
	runtime rather than a backend because all six implementations of this
	rule have to agree on which check names a refusal, and this is the one
	they all mirror."""
	import sys
	sys.path.insert(0, "runtime/python")
	try:
		from situ_runtime import digits_minimal  # type: ignore[import-not-found]
	finally:
		sys.path.remove("runtime/python")

	assert digits_minimal(b"-42", 10)
	assert not digits_minimal(b"-042", 10)
	assert digits_minimal(b"42", 10)
	assert not digits_minimal(b"042", 10)
	# A bare sign is not a spelling of anything; the parse refuses it, and
	# so does this, because a caller asking either question first must get
	# the same answer.
	assert not digits_minimal(b"-", 10)


# -- the same grammar, read by C -------------------------------------------


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("raw,want", CASES, ids=[c[0].decode() for c in CASES])
def test_the_c_walker_parses_the_grammar(
		tmp_path: Path, raw: bytes, want: int | str) -> None:
	"""`situ_walk_read` on the member, which is the C walker's own
	`parse_digits` rather than a generated accessor.

	The READ, and so not the whole of `want`: this call answers what the
	field holds and deliberately not whether the schema admits it -- the
	domain is a `validate` question, asked below. `32768` in an `i16` is a
	number the walker reads and the schema refuses, and the two answers are
	both right about different questions.

	Held here rather than left to the walker/C differential over
	`test/schema/edges.situ`, because that differential does not reach this:
	its buffers are random bytes, and a `-` followed by digits and then the
	delimiter does not arise in them. Breaking the sign in the C walker
	leaves that whole file green, which is what a check with nothing in its
	population looks like from outside.
	"""
	schema   = parse_text(PREAMBLE + BODY)
	resolved = resolve(schema, solve(schema))
	blob, _  = pack(schema, resolved, metadata=True)

	spelled = raw.split(b",")[0]
	parses  = OUT_OF_DOMAIN.get(raw)

	first = _drive(tmp_path, blob, raw, VALUES)[0]
	expect = (str(parses) if parses is not None
	          else "refused" if isinstance(want, str) else str(want))
	assert first == expect, (
		f"{spelled!r}: the C walker reads {first!r}, expected {expect!r}")


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("raw,want", CASES, ids=[c[0].decode() for c in CASES])
def test_the_c_walker_agrees_about_the_verdict(
		tmp_path: Path, raw: bytes, want: int | str) -> None:
	"""And the domain, which is where the derived floor lives in C.

	The image carries the ceiling and nothing else; `-(want + 1)` is
	computed in the check. So this is the only assertion in the tree that
	holds C's derivation to the same arithmetic the Python walker does.
	"""
	schema   = parse_text(PREAMBLE + BODY)
	resolved = resolve(schema, solve(schema))
	blob, _  = pack(schema, resolved, metadata=True)

	assert c_verdict(tmp_path, blob, raw) == (
		"2" if isinstance(want, str) else "0")


@pytest.mark.skipif(HOST_CC is None, reason="no C compiler")
def test_the_c_backend_reads_the_grammar(tmp_path: Path) -> None:
	"""The generated C accessor, which reaches `situ_parse_int` in the C
	runtime -- a fourth implementation of the grammar and the only one the
	tests above do not touch. The walker has its own parser and the three
	other backends have theirs, so nothing else in this file can fail when
	this one is wrong.

	One binary over every case rather than one per case: the table is the
	assertion, and a compile per row would cost thirteen builds to say the
	same thing.
	"""
	header, source = emit_c(BODY, PREAMBLE)
	(tmp_path / "unit.h").write_text(header, encoding="ascii")
	(tmp_path / "unit.c").write_text(source, encoding="ascii")

	(tmp_path / "probe.c").write_text("""#include <stdio.h>
#include <string.h>
#include "unit.h"

static const struct {
	const char *raw;
	uint32_t    len;
	int         ok;
	int32_t     want;
} cases[] = {
ROWS
};

int main(void)
{
	int bad = 0;

	for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		uint8_t buf[16];
		situ_msg_t msg;
		situ_view_t view;
		int16_t got = 0;
		situ_err_t e;

		memcpy(buf, cases[i].raw, cases[i].len);
		situ_msg_init(&msg, buf, cases[i].len);
		if (situ_s_view(&msg, 0, cases[i].len, &view) != SITU_OK) {
			printf("case %zu: no view\\n", i);
			bad = 1;
			continue;
		}

		e = situ_s_n_get(view, &got);
		if ((e == SITU_OK) != cases[i].ok) {
			printf("case %zu: err %d, wanted ok=%d\\n", i, (int)e,
			       cases[i].ok);
			bad = 1;
		} else if (cases[i].ok && got != cases[i].want) {
			printf("case %zu: %ld, wanted %ld\\n", i, (long)got,
			       (long)cases[i].want);
			bad = 1;
		}
	}
	return bad;
}
""".replace("ROWS", _rows(lambda want: f"INT16_C({want})")),
		encoding="ascii")

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[str(HOST_CC), *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_cpp_backend_reads_the_grammar(tmp_path: Path) -> None:
	"""The C++ accessor, which shares the C runtime's `situ_parse_int` and
	nothing else: its own `int64_t` holding, its own cast back down, and its
	own `[[nodiscard]] err` in place of an out-parameter and a code."""
	(tmp_path / "unit.hpp").write_text(emit_cpp(BODY, PREAMBLE),
	                                   encoding="ascii")
	(tmp_path / "main.cpp").write_text(_CPP_PROBE.replace("ROWS", _rows(
		lambda want: f"static_cast<std::int16_t>({want})")), encoding="ascii")

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[str(HOST_CXX), *[w for w in CXX_WARNINGS if w != "-fsyntax-only"],
		 f"-I{CPP_RUNTIME / 'c'}", f"-I{CPP_RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(CPP_RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_backend_reads_the_grammar(tmp_path: Path) -> None:
	"""The fourth reading of the grammar, and the only one with a parser of
	its own in a language that is not C: `situ_rt::parse_int` shares no code
	with `situ_parse_int` and has to agree with it byte for byte."""
	rows = ",\n".join(
		'\t(b"' + "".join(f"\\x{byte:02x}" for byte in raw) + '", '
		+ ("None" if isinstance(want, str) else f"Some({want}i16)")
		+ ")" for raw, want in CASES)

	built = build_rust(tmp_path, BODY, main=_RUST_PROBE.replace("ROWS", rows),
	                   preamble=PREAMBLE)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "out")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


def _rows(spell: Callable[[object], str]) -> str:
	out = []
	for raw, want in CASES:
		spelled = "".join(f"\\x{byte:02x}" for byte in raw)
		out.append(f'\t{{ "{spelled}", {len(raw)}u, '
		           f'{0 if isinstance(want, str) else 1}, '
		           f'{spell(0 if isinstance(want, str) else want)} }},')
	return "\n".join(out)


_CPP_PROBE = """#include <cstdio>
#include <cstring>
#include "unit.hpp"

static const struct {
	const char  *raw;
	std::uint32_t len;
	int           ok;
	std::int16_t  want;
} cases[] = {
ROWS
};

int main()
{
	int bad = 0;

	for (std::size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		std::uint8_t buf[16];
		std::memcpy(buf, cases[i].raw, cases[i].len);

		situ::rt::message msg(buf, cases[i].len);
		situ::s view;
		if (situ::s::at(msg, 0, cases[i].len, view) != situ::rt::err::ok) {
			std::printf("case %zu: no view\\n", i);
			bad = 1;
			continue;
		}

		std::int16_t got = 0;
		const bool ok = view.n(got) == situ::rt::err::ok;
		if (ok != (cases[i].ok != 0)) {
			std::printf("case %zu: ok=%d, wanted %d\\n", i, (int)ok,
			            cases[i].ok);
			bad = 1;
		} else if (ok && got != cases[i].want) {
			std::printf("case %zu: %ld, wanted %ld\\n", i, (long)got,
			            (long)cases[i].want);
			bad = 1;
		}
	}
	return bad;
}
"""

_RUST_PROBE = """const CASES: &[(&[u8], Option<i16>)] = &[
ROWS
];

fn main() {
	let mut bad = false;
	for (raw, want) in CASES {
		let mut buf = [0u8; 16];
		buf[..raw.len()].copy_from_slice(raw);
		let view = unit::S::new(&buf[..raw.len()]).expect("a view");
		let got  = view.n().ok();
		if got != *want {
			println!("{raw:?}: {got:?}, wanted {want:?}");
			bad = true;
		}
	}
	if bad {
		std::process::exit(1);
	}
}
"""
