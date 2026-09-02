"""The C++ backend (section 26.16).

The claim worth testing is not that the C++ compiles -- that is necessary and
easy -- but that it describes the same bytes as the C. Two backends over one
layout that disagree would be worse than one backend, because a schema would
then mean two things.

So the substantial test compiles both headers into one program and compares
them field by field on the same buffer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.cpp.names import PREFIXES, SUFFIXES
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

RUNTIME  = ROOT / "runtime"
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
LIBSITU  = ROOT / "build" / "host" / "runtime" / "libsitu.a"

CPP_USE = '#include "unit.hpp"\nint main()\n{\n\tstd::uint8_t buf[64] = {0};\n\tsitu::rt::message msg(buf, sizeof buf);\n\tsitu::s p;\n\tif (situ::s::at(msg, 0, p) != situ::rt::err::ok) { return 1; }\n\n\tstd::uint16_t seen = 0;\n\tif (p.with_sealed(true, [&](situ::s::sealed_gate g) {\n\t\tseen = g.inner_kind();\n\t}) != situ::rt::err::ok) { return 1; }\n\n\treturn seen == 0 ? 0 : 1;\n}\n'
CPP_FORGE = '#include "unit.hpp"\nint main()\n{\n\tstd::uint8_t buf[64] = {0};\n\tsitu_view_t raw{buf, sizeof buf, 0};\n\tsitu::s::sealed_gate forged(raw);\n\treturn static_cast<int>(forged.inner_kind());\n}\n'
#: The same reach with the gate waived: no token, no callback, and the
#: covered setter still taking the message.
CPP_WAIVED = '#include "unit.hpp"\nint main()\n{\n\tstd::uint8_t buf[64] = {0};\n\tsitu::rt::message msg(buf, sizeof buf);\n\tsitu::s p;\n\tif (situ::s::at(msg, 0, p) != situ::rt::err::ok) { return 1; }\n\n\tp.set_sealed_inner_kind(msg, 7);\n\treturn p.sealed_inner_kind() == 7 ? 0 : 1;\n}\n'

COMPOSE = '#include "unit.hpp"\nint main()\n{\n\talignas(4) volatile std::uint8_t block[8] = {0};\n\tsitu::ctrl r(block);\n\n\tr.write(r.read().with_enable(1).with_mode(5));\n\tauto w = r.read();\n\tif (w.enable() != 1 || w.mode() != 5) { return 1; }\n\n\tr.trigger_start();\n\tif (r.read().raw() != 0x2u) { return 1; }\n\n\tr.write(situ::ctrl::word(0xFFFFFFFFu));\n\tr.clear_error();\n\tif (r.read().raw() != 0x40u) { return 1; }\n\treturn 0;\n}\n'

SEALED_VARIABLE = """codec aead {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl aead extern "my_aead";

struct h { u8 v; u16 length; }
struct s {
	u8   hop;
	authenticated { h hdr; u8 nonce[12]; }
	sealed(aead, nonce = nonce) {
		u16  kind;
		u8   body[hdr.length];
	}
	tag  u8[16];
}
"""

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: `-Werror` because without it this list was decoration: every compile below
#: asserts `returncode == 0`, and a warning does not change that. The C build
#: has had `-Werror` since phase 4 (see the top-level Makefile's WARNFLAGS) and
#: the C++ checks were reading the same flags without the one that enforces
#: them. Nothing in the tree warns today, which is the moment to fix it.
#: `-pedantic-errors` is the difference between asking for C++17 and being
#: held to it. Without it the backend emitted `(const std::uint8_t[]){0x3A}`
#: for every delimited scan -- a compound literal, which is C99 and only a GNU
#: extension in C++ -- and three schemas' headers were not the C++17 section
#: 22 claims while every compile here passed. gcc and clang both accept the
#: extension silently; a conforming compiler in strict mode does not.
WARNINGS = ["-std=c++17", "-pedantic-errors",
	"-O1", "-Wall", "-Wextra", "-Wconversion",
	"-Wsign-conversion", "-Werror", "-fno-exceptions", "-fno-rtti"]


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_cpp(schema, resolved, "unit").header


def emit_materialized(body: str, preamble: str = PREAMBLE) -> str:
	"""The second accessor family as well (decision 0022)."""
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_cpp(schema, resolved, "unit", materialize=True).header


def compiles(tmp_path: Path, body: str, extra: str = "",
		preamble: str | None = None,
		materialize: bool = False) -> subprocess.CompletedProcess[str]:
	"""Generate the header, compile it, and hand back the result.

	`preamble=""` for a schema carrying its own target and endianness, which
	is every worked example: the default is for the fragments written here.
	"""
	schema   = parse_text((PREAMBLE if preamble is None else preamble) + body)
	resolved = resolve(schema, solve(schema))

	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit",
		             materialize=materialize).header, encoding="ascii")

	main = extra or '#include "unit.hpp"\nint main() { return 0; }\n'
	(tmp_path / "main.cpp").write_text(main, encoding="ascii")

	assert HOST_CXX is not None
	return subprocess.run(
		[HOST_CXX, *WARNINGS, "-fsyntax-only",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp")],
		capture_output=True, text=True, check=False)


# -- what it emits ----------------------------------------------------------


def test_an_enum_becomes_an_enum_class() -> None:
	"""Scoped, and with the declared backing type: the width is mandatory so
	the layout is fixed, not so callers have to remember it."""
	header = emit("""enum kind : u8 { hello = 1, goodbye = 2 }
	struct s { kind k; }
	""")

	assert "enum class kind : std::uint8_t {" in header
	assert "hello = 1," in header


def test_a_byte_array_carries_its_length() -> None:
	"""The C backend hands out a bare pointer and a `_COUNT` macro, and nothing
	makes a caller use the second with the first. This is what C++ buys."""
	header = emit("struct s { u8 octets[4]; }")

	assert "::situ::rt::bytes octets() const noexcept" in header
	assert "::situ::rt::bytes(raw_.base + 0, 4)" in header


def test_errors_cannot_be_ignored() -> None:
	"""Every fallible operation is [[nodiscard]]. In C the return value of
	`validate` is as ignorable as any other int."""
	header = emit("struct s { u8 v [must_eq = 1]; }")

	assert header.count("[[nodiscard]]") >= 3
	assert "[[nodiscard]] ::situ::rt::err validate() const noexcept" in header


def test_a_field_may_share_its_name_with_its_type() -> None:
	"""IPv4 has a `protocol` field of type `protocol`, and an unqualified use
	inside the class would change what the name means partway through it --
	which C++ rejects outright. The hazard is not the author's to avoid."""
	header = emit("""enum protocol : u8 { tcp = 6 }
	struct s { protocol protocol; }
	""")

	assert "::situ::protocol protocol() const" in header


def test_the_runtime_does_not_squat_on_schema_names() -> None:
	"""Generated code lives in `situ`; the runtime lives in `situ::rt`. A schema
	is free to declare `struct message`, and it would collide otherwise."""
	header = emit("struct message { u8 a; }\nstruct view { u8 b; }")

	assert "class message : public ::situ::rt::view {" in header
	assert "class view : public ::situ::rt::view {" in header


def test_a_variable_member_inside_a_gate_is_reachable() -> None:
	"""Its length is read through the gate's own view: the field that sizes it
	is plaintext at the same offsets, so only the view differs."""
	header = emit(SEALED_VARIABLE)

	assert "::situ::rt::bytes body() const noexcept" in header
	assert "raw_.base + (" in header
	# Through the gate's view, not the struct's.
	body = header[header.index("bytes body()"):]
	assert "base()" not in body[:body.index("}")]
	assert "raw_.base" in body[:body.index("}")]


# -- what it compiles to ----------------------------------------------------


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
@pytest.mark.parametrize("body", [
	"struct s { u8 a; u16 b; u32 c; }",
	"struct s [allow_straddle] { u4 a; u4 b; bit c; u13 d; u2 e; }",
	"struct inner { u16 x; }\nstruct s { u8 tag; inner nested; }",
	"enum k : u8 { one = 1 }\nstruct s { k kind; u8 rest[3]; }",
	"struct s { q16_16 gain; bcd4 counter; }",
	"struct s { u8 name[8] [nul_terminated, encoding = utf8]; }",
	"struct s { u8 v [must_eq = 1]; reserved u8 [must_be_zero]; }",
	# A covered write takes the message here now, where this backend used to
	# emit the plain setter and mark nothing at all.
	"struct s { u8 hop; authenticated { u16 seq; } tag u8[16]; }",
	# An invariant, and a struct carrying both kinds of obligation at once --
	# which is the only place the shared bit numbering can be wrong.
	"struct s { u16 total; u8 a; u32 b; }\ninvariant s.total == size(s.a) + size(s.b);",
	"struct s { u16 n; u8 a; authenticated { u16 q; } tag u8[4]; }"
	"\ninvariant s.n == size(s.a);",
	# Section 8.6: delimited members, a text number driving an array, and a
	# run of records. Each compiles here because each broke differently the
	# first time -- the count expression refused a text driver, the run was
	# classified as a fixed array of one, and the extent method summed spans
	# declared after it.
	'struct s { u8 magic[4]; u8 line[] until "\\r\\n"; u16 count; }',
	'struct s { u8 f[] until "," [quoted = "\\""]; u8 rest[remaining]; }',
	'struct s { decimal u16 n until "\\r\\n" max 8; u8 body[n]; }',
	'struct kv { u8 k[] until ": "; u8 v[] until "\\r\\n"; }\n'
	'struct blk { kv entries[] until "\\r\\n"; u8 payload[remaining]; }',
	# Section 8.6.4: optional whitespace and a case-insensitive token, which
	# split what had been one number into framing and value.
	'struct h { u8 n[] until ":" [case_insensitive]; u8 v[] until "\\r\\n" [trim]; }\n'
	'struct r { h fields[] until "\\r\\n"; u8 body[remaining]; }',
	'struct s { decimal u16 n until "\\r\\n" [trim, minimal]; u8 b[n]; }',
	# Section 19.4: one build reading several versions of one protocol.
	'struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; u16 c [since = 3]; }',
	# A struct named `msg` shadowed the generated factory's own parameter in
	# C++, so any schema using that name emitted a header that did not compile.
	"struct msg { u8 a; u16 b; }",
	# A nested struct with no single size, and a member after it. Every
	# backend got this wrong in a different way and none of them compiled or
	# ran it before.
	"struct inner { u8 n; u8 body[n]; }\nstruct outer { inner a; u16 tail; }",
	# Section 8.6.6: a run ending on a condition, whose element is sized by
	# arithmetic over one of its own fields.
	"struct e { u8 next; u8 len; u8 d[(len + 1) * 8 - 2]; }\n"
	"struct s { e chain[] while (next == 43) max 8; u8 rest[remaining]; }",
])
def test_it_compiles_clean(tmp_path: Path, body: str) -> None:
	result = compiles(tmp_path, body)

	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


@pytest.mark.skipif(HOST_CXX is None or not LIBSITU.exists(),
	reason="no host C++ compiler or runtime not built")
def test_both_backends_describe_the_same_bytes(tmp_path: Path) -> None:
	"""The claim that matters. Two backends over one layout that disagreed
	would be worse than one, because the schema would then mean two things.

	Both headers go into one program, over one buffer, and every field is
	compared -- including a write through the C++ side read back through C.
	"""
	body = """enum protocol : u8 { tcp = 6, udp = 17 }
	struct addr { u8 octets[4]; }
	struct hdr [allow_straddle] {
		u4        version;
		u4        ihl;
		u16       total;
		bit       flag;
		u15       offset;
		protocol  proto;
		u8        ttl;
		addr      source;
	}
	"""
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))

	emitted = generate_c(schema, resolved, "unit")
	for name, text in emitted.files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")

	(tmp_path / "main.cpp").write_text('''
#include <cstdio>
#include <cstring>
#include "unit.hpp"
extern "C" {
#include "unit.h"
}
int main()
{
	std::uint8_t buf[13];
	for (unsigned i = 0; i < sizeof buf; i++) {
		buf[i] = static_cast<std::uint8_t>(i * 11 + 3);
	}

	situ::rt::message msg(buf, sizeof buf);
	situ::hdr cpp;
	if (situ::hdr::at(msg, 0, cpp) != situ::rt::err::ok) { return 1; }

	situ_msg_t cmsg;
	situ_view_t cview;
	situ_msg_init(&cmsg, buf, sizeof buf);
	if (situ_hdr_view(&cmsg, 0, &cview) != SITU_OK) { return 1; }

	int bad = 0;
	#define SAME(f) do { \\
		unsigned long long a = (unsigned long long)cpp.f(); \\
		unsigned long long b = (unsigned long long)situ_hdr_##f##_get(cview); \\
		if (a != b) { std::printf("%s: %llu != %llu\\n", #f, a, b); bad++; } \\
	} while (0)
	SAME(version); SAME(ihl); SAME(total); SAME(flag);
	SAME(offset); SAME(proto); SAME(ttl);
	#undef SAME

	situ_view_t csrc;
	situ_hdr_source_view(cview, &csrc);
	situ::addr source;
	if (cpp.source(source) != situ::rt::err::ok) { return 1; }
	if (std::memcmp(source.octets().data(),
	                situ_addr_octets_ptr(csrc), 4) != 0) { bad++; }
	if (source.octets().size() != 4) { bad++; }

	cpp.set_ttl(64);
	if (situ_hdr_ttl_get(cview) != 64) { bad++; }

	return bad;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	build = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(tmp_path / "unit.c"), str(LIBSITU),
		 "-o", str(tmp_path / "run")],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	run = subprocess.run([str(tmp_path / "run")], capture_output=True,
	                     text=True, check=False)
	assert run.returncode == 0, f"backends disagree:\n{run.stdout}"


# -- dynamic layout ---------------------------------------------------------


def test_a_variable_struct_is_told_its_extent() -> None:
	"""Nothing in the bytes says where the frame ends, so the caller supplies
	it -- and that is the one bounds check everything else trusts."""
	header = emit("struct h { u8 v; u16 n; }\nstruct s { h hdr; u8 opts[hdr.n]; }\n")

	assert "std::uint32_t offset, std::uint32_t length, s &out" in header
	assert "static constexpr std::uint32_t size_min = 3;" in header


def test_a_dynamic_offset_sums_what_precedes_it() -> None:
	"""The same walk the C backend does: constants for the fixed members, and
	a runtime read for each variable one.

	Statements rather than one expression, because the running total has to
	be a variable: each term's own accessor otherwise re-derives its base by
	rescanning everything before it, and the sum costs more than the terms.
	"""
	header = emit("struct h { u8 v; u16 n; }\nstruct r { u32 id; }\n"
	              "struct s { h hdr; u8 opts[hdr.n]; r recs[hdr.n]; }\n")

	assert "std::uint32_t recs_offset() const noexcept" in header
	assert "std::uint32_t at = 0;" in header
	assert "at += 3;" in header


def test_an_element_is_bounded_by_the_count_not_just_the_view() -> None:
	"""Bytes after the array are inside the view and are not elements. The C
	backend learned that the hard way; this one starts with it."""
	header = emit("struct h { u8 v; u16 n; }\nstruct r { u32 id; }\n"
	              "struct s { h hdr; r recs[hdr.n]; u32 trailer; }\n")

	assert "if (index >= recs_count()) {" in header
	assert "return ::situ::rt::err::bounds;" in header


def test_remaining_runs_to_the_limit() -> None:
	header = emit("struct s { u8 head; u8 rest[remaining]; }")

	assert "(raw_.limit - (1))" in header


# -- the stage gate ---------------------------------------------------------


SEALED = """codec aes_gcm_128 {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl aes_gcm_128 extern "my_aes_gcm_128";

struct h { u8 v; u16 length; }
struct s {
	u8   hop;
	authenticated { h hdr; u8 nonce[12]; }
	sealed(aes_gcm_128, nonce = nonce) {
		u16  inner_kind;
		u8   session_key[16] [secret];
	}
	tag  u8[16];
}
"""


def test_the_gate_has_no_public_constructor() -> None:
	"""Section 14.3 wants a sealed interior unreachable before its tag
	verifies. C gets close, and anybody determined enough can fill the struct
	in anyway; this cannot be written at all."""
	header = emit(SEALED)

	assert "class sealed_gate {" in header
	assert "friend class s;" in header
	assert "explicit constexpr sealed_gate(situ_view_t raw)" in header


def test_a_secret_field_gets_no_accessor_even_inside_the_gate() -> None:
	"""Section 14.6: no debug accessor is generated for it at all."""
	header = emit(SEALED)

	assert "session_key is [secret]" in header
	assert "session_key() const" not in header


def test_allow_unverified_read_leaves_no_gate_and_says_so() -> None:
	"""The waiver of 14.3, which this backend did not read.

	`[allow_unverified_read]` sets `stage = TransformTime` on the interior --
	the capability map says so, and `situc map` prints it -- so a backend that
	builds the gate anyway is describing different bytes from the map. Worse,
	the gate's own comments then say the interior "opens only once the tag has
	verified", which is the opposite of what the schema asked for and of what
	the map records.

	So: no gate class, no opener, and the interior on the ordinary view. What
	the waiver does *not* touch is the coverage obligation -- `inner_kind` is
	still `Covered(tag)`, so its setter still takes the message and marks the
	bit (14.2). Two separate claims, and only one of them was given up.
	"""
	header = emit(SEALED.replace(
		"sealed(aes_gcm_128, nonce = nonce) {",
		"sealed(aes_gcm_128, nonce = nonce) [allow_unverified_read] {"))

	assert "class sealed_gate {" not in header
	# The declaration, not the spelling: the note that replaces the gate
	# names the opener it is telling the reader is absent.
	assert "with_sealed(bool verified" not in header
	assert "bytes nobody has authenticated" in header
	assert "std::uint16_t sealed_inner_kind() const noexcept" in header
	assert ("void set_sealed_inner_kind(::situ::rt::message &owner,"
	        " std::uint16_t value) noexcept") in header

	# The other half of 14.6 survives the waiver: giving up the stage gate is
	# not giving up secrecy, and a `[secret]` field gets no accessor either
	# way.
	assert "session_key is [secret]" in header
	assert "session_key() const" not in header

	# And the gate is still built where nothing waived it, or this would pass
	# by having removed the feature.
	assert "class sealed_gate {" in emit(SEALED)


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_waived_interior_is_read_without_a_token(tmp_path: Path) -> None:
	"""The accessor is real, not only a name in a comment."""
	result = compiles(tmp_path, SEALED.replace(
		"sealed(aes_gcm_128, nonce = nonce) {",
		"sealed(aes_gcm_128, nonce = nonce) [allow_unverified_read] {"),
		extra=CPP_WAIVED)

	assert result.returncode == 0, result.stderr


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_interior_is_reachable_through_the_check(tmp_path: Path) -> None:
	result = compiles(tmp_path, SEALED, extra=CPP_USE)

	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_forging_a_gate_does_not_compile(tmp_path: Path) -> None:
	"""The whole claim. If this ever starts compiling, the C++ backend has
	stopped offering anything the C one does not."""
	result = compiles(tmp_path, SEALED, extra=CPP_FORGE)

	assert result.returncode != 0, "a gate was constructed without verifying"
	assert "private" in result.stderr or "protected" in result.stderr


# -- registers (section 15) -------------------------------------------------


MMIO = "target mmio;\nendian little;\nbit_order lsb_first;\n"

REGISTER = """register ctrl @ 0x00 {
	width        = 32;
	access_width = 32;
	volatile;
	no_rmw;

	bit       enable  [rw];
	bit       start   [wo, on_write = trigger];
	u3        mode    [rw];
	bit       busy    [ro];
	bit       error   [w1c];
	reserved  u25     [preserve];
}
"""


def test_a_register_composes_a_word_then_writes_it_once() -> None:
	"""Section 15's headline is that a partial-width field in a `no_rmw`
	register cannot be written alone, and the remedy is to compose the whole
	word. Here that remedy is the only shape the API has."""
	header = emit(REGISTER, preamble=MMIO)

	assert "class word {" in header
	assert "constexpr word with_enable(std::uint8_t value) const noexcept" in header
	assert "void write(word value) const noexcept" in header


def test_an_access_mode_decides_which_operations_exist() -> None:
	"""Not which are documented as unwise. A `ro` field has no composer and a
	`wo` field has no getter, so the mode is checked by the compiler."""
	header = emit(REGISTER, preamble=MMIO)

	assert "No with_busy(): the mode is ro." in header
	assert "No start(): the mode is wo" in header
	assert "No with_error(): `w1c` is not an assignment" in header


def test_a_write_that_is_not_an_assignment_gets_its_own_method() -> None:
	"""`w1c` clears by writing a one, and `on_write` makes the write itself the
	event. Neither is `x = value`, so neither is spelled that way."""
	header = emit(REGISTER, preamble=MMIO)

	assert "void clear_error() const noexcept" in header
	assert "void trigger_start() const noexcept" in header


def test_a_reserved_field_is_carried_through_a_compose() -> None:
	"""`with_x` clears only its own bits, so reserved bits keep whatever the
	read gave them -- which is what `[preserve]` asks for."""
	header = emit(REGISTER, preamble=MMIO)

	assert "is reserved: no accessor" in header
	assert "carried through a compose untouched" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_register_composition_produces_the_right_bits(tmp_path: Path) -> None:
	result = compiles(tmp_path, REGISTER, extra=COMPOSE, preamble=MMIO)

	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
@pytest.mark.parametrize("expression", [
	"w.with_busy(1)",	# ro: no composer
	"w.start()",		# wo: no getter
	"w.with_error(1)",	# w1c: the write is not an assignment
])
def test_an_operation_the_mode_forbids_does_not_compile(
	tmp_path: Path, expression: str,
) -> None:
	"""The claim registers are worth a backend for. In C these are comments."""
	main = ('#include "unit.hpp"\nint main() { situ::ctrl::word w(0);'
	        f' (void){expression}; return 0; }}\n')
	result = compiles(tmp_path, REGISTER, extra=main, preamble=MMIO)

	assert result.returncode != 0, f"{expression} compiled and should not"


def test_an_enum_rejects_a_value_that_is_not_a_member() -> None:
	"""Section 8.7, and all three backends now agree on it."""
	header = emit('enum k : u8 { one = 1, two = 2 }\nstruct s { k kind; u8 pad; }')

	assert "constexpr bool is_known(k value) noexcept" in header
	assert "if (!is_known(kind())) {" in header


def test_default_pass_admits_what_it_says_it_admits() -> None:
	header = emit('enum k : u8 { one = 1, two = 2, default = pass }\nstruct s { k kind; u8 pad; }')

	assert "constexpr bool is_known(k value) noexcept" in header
	assert "if (!is_known(kind()))" not in header


def test_a_constrained_field_at_a_dynamic_offset_is_validated() -> None:
	"""The C backend always did; this one said it could not place the field at
	all, so a `must_eq` after a variable member went unchecked. Three backends
	over one layout that disagree mean a schema means three things."""
	header = emit("struct h { u8 v; u16 n; }\n"
	              "struct s { h hdr; u8 opts[hdr.n]; u16 after [must_eq = 7]; }")

	assert "std::uint16_t after() const noexcept" in header
	assert "if (after() != 7) {" in header


# -- a member nothing can measure -------------------------------------------

UNMEASURABLE = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		default: opaque;
	}
}
struct name { label labels[] while (form == 0 && rest != 0) max 8; }
struct question { name qname; u16 qtype; }
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_an_unmeasurable_nested_member_gets_no_accessor(tmp_path: Path) -> None:
	"""A struct whose extent nothing can compute gets no accessor.

`label` ends in an `opaque` default arm, which swallows whatever is left, so
one is exactly as long as the view it was handed; `name` is a run of those, so
its own length is unknown in turn; and `question.qname` is one of *those*. Each backend emitted
the accessor anyway and reached for an extent it had declined to emit --
the header did not compile.

Nothing after such a member can be placed either, which is why `qtype` goes
with it: its offset is the extent nobody can compute.
"""
	header = emit(UNMEASURABLE)

	# The member names still appear -- in the note saying why each was
	# declined, which is the whole point of emitting one.
	assert "qname_extent()" not in header
	assert "qtype() const" not in header
	assert "question.qname: one `name` has no" in header
	assert "question.qtype: its offset cannot be resolved" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_and_the_header_compiles(tmp_path: Path) -> None:
	"""It did not: `class situ::name has no member named extent`."""
	assert compiles(tmp_path, UNMEASURABLE).returncode == 0


# -- a run whose element is a variant ---------------------------------------

DNS_LABEL = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		case 3:  u8 pointer_low;
		default: error;
	}
}
struct name { label labels[] while (form == 0 && rest != 0) max 128; }
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_compressed_name_walks(tmp_path: Path) -> None:
	"""The four shapes a DNS name comes in, against a hand-checked count and
	extent -- the same table the C suite walks, because agreeing with the
	other backends is the property under test."""
	# Compiled *and run*: `compiles` is -fsyntax-only, and a `main` returning
	# 1 that nobody executes is a test of nothing.
	result = compiles(tmp_path, DNS_LABEL, extra="""
#include "unit.hpp"

static bool walk(const std::uint8_t *b, std::uint32_t n, std::uint32_t labels,
                 std::uint32_t extent, ::situ::rt::err want)
{
	situ_view_t raw{ const_cast<std::uint8_t *>(b), n, 0 };
	::situ::name held{ raw };

	if (held.labels_count() != labels || held.labels_span() != extent)
		return false;

	for (std::uint32_t i = 0; i < labels; i++) {
		::situ::label one{ raw };
		if (held.labels(i, one) != ::situ::rt::err::ok)
			return false;
		if (one.validate() != want)
			return false;
	}
	return true;
}

int main()
{
	const std::uint8_t plain[]  = { 3,'w','w','w', 7,'e','x','a','m','p','l','e',
	                                3,'c','o','m', 0 };
	const std::uint8_t whole[]  = { 0xC0, 0x0C };
	const std::uint8_t suffix[] = { 3,'w','w','w', 0xC0, 0x0C };
	const std::uint8_t bad[]    = { 0x40, 0x00 };

	return (walk(plain,  sizeof plain,  4, 17, ::situ::rt::err::ok)
	     && walk(whole,  sizeof whole,  1,  2, ::situ::rt::err::ok)
	     && walk(suffix, sizeof suffix, 2,  6, ::situ::rt::err::ok)
	     && walk(bad,    sizeof bad,    1,  1, ::situ::rt::err::version))
	     ? 0 : 1;
}
""")

	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a length the message declares, and the frame it has to fit -------------

OVERLONG = "struct s { u8 n; u16 want; u8 body[want]; u8 tail[remaining]; }"


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_declared_length_is_clamped_and_reported(tmp_path: Path) -> None:
	"""This backend handed out a raw pointer and the length the message
	claimed, which is the same out-of-bounds read the C backend had."""
	result = compiles(tmp_path, OVERLONG, extra="""
#include "unit.hpp"

int main()
{
	std::uint8_t buf[16] = { 0 };
	buf[1] = 0x03; buf[2] = 0xE8;		/* says 1000 bytes of body */

	const ::situ::s held{ situ_view_t{ buf, sizeof buf, 0 } };
	const auto body = held.body();

	if (body.size() != 13u)
		return 1;
	if (body.data() + body.size() > buf + sizeof buf)
		return 2;
	if (held.validate() != ::situ::rt::err::bounds)
		return 3;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a member the data positions (section 9.8) ------------------------------

LOCATED = "struct s { u32 off; u16 n; u8 body[n] at off; u16 after; }"


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_located_member_is_reached_through_the_message(tmp_path: Path) -> None:
	"""The frame starts at 4, not at 0. With it at 0 the frame base and the
	message base are the same address, and reading the offset from the wrong
	one gives the right answer -- which is how the C version of this test
	passed against a generator that used the frame."""
	result = compiles(tmp_path, LOCATED, extra="""
#include <cstring>
#include "unit.hpp"

int main()
{
	std::uint8_t buf[28] = { 0 };
	buf[4 + 3] = 16; buf[4 + 5] = 4;
	buf[4 + 6] = 0xBE; buf[4 + 7] = 0xEF;
	std::memcpy(buf + 16, "DATA", 4);

	situ_msg_t msg;
	situ_msg_init(&msg, buf, sizeof buf);

	const ::situ::s held{ situ_view_t{ buf + 4, 24, msg.generation } };
	::situ::rt::bytes body;

	/* `after` sits where it would if `body` were not declared at all. */
	if (held.after() != 0xBEEF)
		return 1;
	if (held.body(&msg, body) != ::situ::rt::err::ok)
		return 2;
	if (body.data() != buf + 16 || body.size() != 4u)
		return 3;

	buf[4 + 3] = 200;
	if (held.body(&msg, body) != ::situ::rt::err::bounds)
		return 4;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- framing a stream -------------------------------------------------------

STREAM_FRAMED = "struct s { u8 version; u16 n; u8 body[n]; u16 trailer; }"


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_required_never_reads_a_length_that_has_not_arrived(tmp_path: Path) -> None:
	"""Fed one byte at a time. Until `n` has wholly arrived the only honest
	answer is the minimum -- reading it from byte 1 alone would say 0x0004 or
	0x0400 depending on which byte turned up first, and that guess would size
	the next read. The same table the C suite walks."""
	result = compiles(tmp_path, STREAM_FRAMED, extra="""
#include "unit.hpp"

int main()
{
	std::uint8_t whole[9] = { 1, 0, 4, 'D','A','T','A', 0xBE, 0xEF };
	std::uint32_t have, need;

	for (have = 0; have < sizeof whole; have++) {
		if (::situ::s::required(whole, have, need) != ::situ::rt::err::truncated)
			return 1;
		if (need > sizeof whole || need <= have)
			return 2;
		if (have < 3u && need != ::situ::s::size_min)
			return 3;
		if (have >= ::situ::s::size_min && need != sizeof whole)
			return 4;
	}

	if (::situ::s::required(whole, sizeof whole, need) != ::situ::rt::err::ok)
		return 5;
	if (need != sizeof whole)
		return 6;
	if (::situ::s::required(whole, 64u, need) != ::situ::rt::err::ok
			|| need != sizeof whole)
		return 7;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


TWO_LENGTHS = "struct s { u16 n; u8 a[n]; u16 m; u8 b[m]; }"


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_length_behind_a_variable_member_resolves(tmp_path: Path) -> None:
	"""`m` sits at `2 + n`, so there is no constant to read it at -- and this
	backend read length drivers only at constant offsets, so `b` was dropped
	from the class with a note and `required` declined along with it.

	Its own accessor knows where it is, and C++ lets a member function call
	one declared after it, so the fix is to call `m()`. The test that stood
	here asserted the limitation; this is what it was standing in for.
	"""
	result = compiles(tmp_path, TWO_LENGTHS, extra="""
#include <cstring>
#include "unit.hpp"

int main()
{
	/* n = 3 "abc", m = 2 "xy": 2 + 3 + 2 + 2 = 9. */
	std::uint8_t buf[9] = { 0, 3, 'a','b','c', 0, 2, 'x','y' };
	const ::situ::s held{ situ_view_t{ buf, sizeof buf, 0 } };

	if (held.b_offset() != 7u)
		return 1;
	if (held.b().size() != 2u || std::memcmp(held.b().data(), "xy", 2) != 0)
		return 2;

	std::uint32_t need;
	if (::situ::s::required(buf, sizeof buf, need) != ::situ::rt::err::ok)
		return 3;
	if (need != sizeof buf)
		return 4;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_length_behind_a_variable_member_is_guarded(tmp_path: Path) -> None:
	"""And now that it resolves, the framing guard can be tested here too.

	Where every length is at a static offset the `size_min` gate covers them
	all and the per-member check looks redundant; this is the shape where it
	is the only thing between `required` and a read past the buffer. C++
	reads past it silently -- Python raises and Rust panics -- so this needs a
	sanitizer to notice.
	"""
	result = compiles(tmp_path, TWO_LENGTHS, extra="""
#include <cstdlib>
#include <cstring>
#include "unit.hpp"

int main()
{
	/* n = 200, so `m` claims offset 202. Six bytes have arrived, on the heap
	 * so a read past them is a fault the sanitizer sees. */
	auto *part = static_cast<std::uint8_t *>(std::malloc(6));
	std::uint32_t need = 0;

	if (part == nullptr)
		return 1;
	part[0] = 0; part[1] = 200;
	std::memset(part + 2, 'x', 4);

	const auto got = ::situ::s::required(part, 6u, need);
	std::free(part);

	if (got != ::situ::rt::err::truncated)
		return 2;
	if (need != 204u)		/* 4 fixed, plus the 200 `n` claims */
		return 3;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 "-fsanitize=address",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	if built.returncode != 0 and "sanitize" in built.stderr:
		pytest.skip("no address sanitizer")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(binary)], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- a base the message puts past the end -----------------------------------

OVERREACHING = 'struct s { u16 n; u8 a[n]; u8 b[] until ";"; }'


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_scan_base_past_the_frame_reads_nothing(tmp_path: Path) -> None:
	"""`n` is a `u16` the message chooses, so `b`'s base can sit past the end
	of a ten-byte frame. The scan limit was `len - base`, which underflows to
	about four billion, and the scan then searched that much memory.

	C++ read out of bounds -- an AddressSanitizer SEGV. Rust panicked on the
	slice before any limit applied. Python returned a wrong number. C had been
	saturating here since the `[remaining]` fix and the other three were not.
	All four now answer as C does: an empty scan."""
	result = compiles(tmp_path, OVERREACHING, extra="""
#include <cstdlib>
#include <cstring>
#include "unit.hpp"

int main()
{
	/* Ten bytes on the heap, so a read past them is a fault ASan sees. */
	auto *buf = static_cast<std::uint8_t *>(std::malloc(10));
	if (buf == nullptr)
		return 1;
	std::memset(buf, 0, 10);
	buf[0] = 0xFF; buf[1] = 0xFF;		/* n = 65535 */

	const ::situ::s held{ situ_view_t{ buf, 10, 0 } };
	const std::uint32_t len = held.b_len();
	std::free(buf);

	return len == 0u ? 0 : 2;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 "-fsanitize=address",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	if built.returncode != 0 and "sanitize" in built.stderr:
		pytest.skip("no address sanitizer")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(binary)], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- the second accessor family (decision 0022) -----------------------------

INDEXED_RUN = """
struct l { u2 f; u6 r; u8 t[r]; }
struct n { l ls[] while (f == 0 && r != 0) max 128; }
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_run_index_agrees_with_the_walk(tmp_path: Path) -> None:
	"""C's shape rather than Python's list, because this backend shares C's
	constraint: a view is a value, nothing allocates, and the storage is the
	caller's. `max N` bounds the array. Measured at 13ms against 2ms."""
	schema   = parse_text(PREAMBLE + INDEXED_RUN)
	resolved = resolve(schema, solve(schema))
	built    = generate_cpp(schema, resolved, "unit", materialize=True)
	(tmp_path / "unit.hpp").write_text(built.header, encoding="ascii")
	(tmp_path / "main.cpp").write_text("""
#include "unit.hpp"

int main()
{
	std::uint8_t buf[128];
	std::uint32_t k = 0;
	for (int i = 0; i < 40; i++) { buf[k++] = 1; buf[k++] = 'a'; }
	buf[k++] = 0;

	const ::situ::n v{ situ_view_t{ buf, k, 0 } };
	const auto idx = v.ls_indexed();

	if (idx.count != v.ls_count() || idx.count != 41u)
		return 1;
	for (std::uint32_t i = 0; i < idx.count; i++) {
		::situ::l a{ situ_view_t{ buf, k, 0 } }, b{ situ_view_t{ buf, k, 0 } };
		if (v.ls(i, a) != ::situ::rt::err::ok)          return 2;
		if (v.ls_at(idx, i, b) != ::situ::rt::err::ok)  return 3;
		if (a.base() != b.base() || a.limit() != b.limit()) return 4;
	}
	::situ::l past{ situ_view_t{ buf, k, 0 } };
	if (v.ls_at(idx, idx.count, past) != ::situ::rt::err::bounds)
		return 5;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	built_ = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built_.returncode == 0, built_.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- reaching into a variant's arms (section 9.6) ---------------------------

ARMS = """
struct label {
	u2 form;
	u6 rest;
	variant body switch (form) {
		case 0:  u8 text[rest];
		case 3:  u8 pointer_low;
		default: error;
	}
}
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_each_arm_refuses_when_it_is_not_the_one_present(tmp_path: Path) -> None:
	"""`err::version` is what an unrecognised discriminant gets, and reading
	the arm that is not present is the same mistake from the other end."""
	result = compiles(tmp_path, ARMS, extra="""
#include <cstring>
#include "unit.hpp"

int main()
{
	std::uint8_t text[] = { 3, 'w', 'w', 'w' };
	std::uint8_t ptr[]  = { 0xC0, 0x0C };
	::situ::rt::bytes b;
	std::uint8_t low;

	const ::situ::label a{ situ_view_t{ text, sizeof text, 0 } };
	if (a.body_text(b) != ::situ::rt::err::ok)                return 1;
	if (b.size() != 3 || std::memcmp(b.data(), "www", 3) != 0) return 2;
	if (a.body_pointer_low(low) != ::situ::rt::err::version)  return 3;

	const ::situ::label c{ situ_view_t{ ptr, sizeof ptr, 0 } };
	if (c.body_pointer_low(low) != ::situ::rt::err::ok)       return 4;
	if (low != 0x0Cu)                                          return 5;
	if (c.body_text(b) != ::situ::rt::err::version)            return 6;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


ENUM_ARMS = """
enum K : u8 { a = 1, b = 2, }
struct A { u16 x; }
struct B { u32 y; }
struct S {
	K k;
	variant v switch (k) {
		case K.a: A p;
		case K.b: B q;
		default:  error;
	}
}
"""


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_an_enum_discriminant_compiles_and_selects(tmp_path: Path) -> None:
	"""An enum discriminant, which no test in this file had.

	Section 9.6's own example uses one -- `case msg_type.hello:` -- and this
	backend did not compile such a schema at all: the extent chain, the
	`default: error` check and the arm guards all compared the enum getter
	against a number, and that getter hands back an `enum class`, which has no implicit conversion. Three separate
	constructs, one missing test."""
	result = compiles(tmp_path, ENUM_ARMS, extra="""
#include "unit.hpp"

int main()
{
	std::uint8_t buf[8] = { 0 };
	buf[0] = 1; buf[1] = 0xBE; buf[2] = 0xEF;

	const ::situ::S s{ situ_view_t{ buf, sizeof buf, 0 } };
	::situ::A a{ situ_view_t{ buf, sizeof buf, 0 } };
	::situ::B b{ situ_view_t{ buf, sizeof buf, 0 } };

	if (s.v_p(a) != ::situ::rt::err::ok || a.x() != 0xBEEF) return 1;
	if (s.v_q(b) != ::situ::rt::err::version)               return 2;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- every example, compiled ------------------------------------------------


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_compiles(schema: Path, tmp_path: Path) -> None:
	"""The C suite has had this since phase 4 and this one had not, which is
	how three examples came to not compile at all: `message` had a counted
	array of structs whose element type was qualified twice, `ipv6ext` a run
	whose condition compared an `enum class` to an int, and `smtp` a coded
	region whose scan helpers this backend does not emit.

	Generating is not compiling. Every backend that emits a language with a
	compiler should be held to its compiler.

	It read `example/` only until this list did, and the cost of that was
	specific: `test/schema/edges.situ` did not compile here at all, because
	`struct framed` and the framing method every view gets meet in a scope C++
	will not let them share. The schema written to hold the awkward shapes is
	the one a check globbing `example/` skips (26.31).
	"""
	parsed   = parse_text(schema.read_text(encoding="utf-8"))
	resolved = resolve(parsed, solve(parsed))
	built    = generate_cpp(parsed, resolved, schema.stem)

	(tmp_path / f"{schema.stem}.hpp").write_text(built.header,
	                                             encoding="ascii")
	main = tmp_path / f"main_{schema.stem}.cpp"
	main.write_text(f'#include "{schema.stem}.hpp"\nint main() {{ return 0; }}\n',
	                encoding="ascii")

	# `-fsyntax-only`, because linking is what this was doing: without it g++
	# produced an `a.out` in whatever directory pytest was run from, and one
	# of them was committed to this repository. The header is header-only and
	# `main` calls nothing, so the link resolved no symbol and proved nothing.
	result = subprocess.run(
		[HOST_CXX or "g++", *WARNINGS, "-fsyntax-only",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(main)],
		capture_output=True, text=True)
	assert result.returncode == 0, f"{schema.stem}: {result.stderr}"


# -- a coded region that ends at a delimiter (13.6) -------------------------

CODED = 'codec dot_stuffing {\n\tkernel = stuffing(worst_case = 4, per = 3, unit = stream, code = smtp_dot);\n}\nimpl dot_stuffing derived;\nstruct data_block {\n\tcoded body(dot_stuffing) until "\\r\\n.\\r\\n" {\n\t\tu8 content[remaining];\n\t}\n}\n'


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_coded_region_is_framed_like_any_delimited_member(tmp_path: Path) -> None:
	"""A coded region that ends at a delimiter is framed like any other
	delimited member: the scan is over the *encoded* bytes, which is the order
	the format specifies -- a stuffing code protects its own terminator, so
	the sequence is unambiguous here and would not be after decoding (13.6).

	Three backends emitted nothing for one, because `traverse.classify`
	answered `REGION` before it asked about the delimiter. C reaches its
	delimited emitter for anything with a delimiter and does not use that
	function, so it had the accessors all along -- which is why the gap read
	as three backends being behind rather than one classifier being wrong.

	`Hello\\r\\n..dotted\\r\\n` then the terminator: 17 bytes of content,
	22 including it. All four agree."""
	result = compiles(tmp_path, CODED, extra="""
#include <cstring>
#include "unit.hpp"

int main()
{
	static const char raw[] = "Hello\\r\\n..dotted\\r\\n\\r\\n.\\r\\nX";
	std::uint8_t buf[64];
	std::memcpy(buf, raw, sizeof raw - 1);

	const ::situ::data_block v{ situ_view_t{ buf, sizeof raw - 1, 0 } };
	if (v.body_len() != 17u)    return 1;
	if (v.body_span() != 22u)   return 2;
	if (!v.body_terminated())   return 3;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a coded region's bytes, and its transform (13.5) -----------------------

CODED_PRE  = 'target buffer;\nendian big;\nbit_order msb_first;\ncodec halve { kernel = table(input_bits = 1, output_bits = 2, code = manchester_802_3); }\nimpl halve derived;\n'
CODED_BODY = 'struct S { coded body(halve) { u8 raw[4]; } }'


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_coded_region_decodes_into_the_callers_buffer(tmp_path: Path) -> None:
	"""The decoder is C's (decision 0017), declared at file scope with C
	linkage -- it cannot go inside the class that calls it, a linkage
	specification not being allowed in a block."""
	schema   = parse_text(CODED_PRE + CODED_BODY)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")

	from situc.codegen.c import derived
	(tmp_path / "unit_derived.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")
	(tmp_path / "unit.h").write_text(
		generate_c(schema, resolved, "unit").header, encoding="ascii")

	(tmp_path / "main.cpp").write_text("""
#include <cstring>
#include "unit.hpp"

extern "C" std::uint32_t situ_halve_encode(const std::uint8_t *, std::uint32_t,
                                           std::uint8_t *);

int main()
{
	std::uint8_t plain[4] = { 0xA5, 0x3C, 0xF0, 0x0F };
	std::uint8_t buf[8], out[::situ::S::body_decoded_max];
	std::uint32_t len = 0;

	situ_halve_encode(plain, 32u, buf);
	const ::situ::S v{ situ_view_t{ buf, sizeof buf, 0 } };

	if (v.body().size() != 8u)                                     return 1;
	if (::situ::S::body_decoded_max != 4u)                         return 2;
	if (v.body_decode(out, sizeof out, len) != ::situ::rt::err::ok) return 3;
	if (len != 4u || std::memcmp(out, plain, 4) != 0)               return 4;
	/* A byte short is refused rather than half-filled. */
	if (v.body_decode(out, 3u, len) != ::situ::rt::err::bounds)     return 5;
	return 0;
}
""", encoding="ascii")

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(tmp_path / "unit_derived.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- tlv regions (section 9.5) ----------------------------------------------

TLV_PREAMBLE = (PREAMBLE
	+ "varint_type pb_varint { encoding = leb128; max_bits = 64; }\n")

TLV = """struct S {
	tlv fields (
		tag_type     = pb_varint,
		tag_decode   = { field = tag >> 3, wire = tag & 0x7 },
		tag_identity = field,
		value_size   = switch (wire) {
			case 0: self_delimiting,
			case 1: 8,
			case 2: prefixed(pb_varint),
			case 5: 4,
			default: error,
		},
		known = {
			1 : { name = user_id, wire = 0, type = pb_varint },
			2 : { name = label,   wire = 2, type = u8 },
		},
		unknown = preserve
	);
}"""


def test_a_tlv_region_gets_a_walk() -> None:
	"""It answered REGION in the shared classifier and this backend emitted
	"not in the static subset yet" -- the fallthrough note, for the one
	construct section 9.7 makes the conformance gate."""
	header = emit(TLV, preamble=TLV_PREAMBLE)

	assert "fields_first(fields_item &out)" in header
	assert "fields_next(fields_item &item)" in header
	assert "fields_count()" in header
	assert "not in the static subset yet" not in header


def test_the_item_is_a_nested_struct() -> None:
	"""A cursor into bytes somebody else owns. Giving it the view's interface
	would suggest it were a frame of its own."""
	header = emit(TLV, preamble=TLV_PREAMBLE)

	assert "struct fields_item {" in header
	assert "std::uint32_t field;\t/* tag >> 3 */" in header
	assert "std::uint32_t wire;\t/* tag & 0x7 */" in header


def test_each_wire_type_is_sized_as_the_dispatch_says() -> None:
	header = emit(TLV, preamble=TLV_PREAMBLE)

	assert "switch (out.wire) {" in header
	assert "size = 8u;" in header and "size = 4u;" in header
	assert "return ::situ::rt::err::constraint;" in header


def test_each_known_tag_gets_an_accessor() -> None:
	header = emit(TLV, preamble=TLV_PREAMBLE)

	assert "user_id(fields_item &item)" in header
	assert "label(fields_item &item)" in header
	assert "fields_find(1u, item)" in header


def test_by_name_accessors_match_the_identity_part() -> None:
	"""Decision 0023."""
	header = emit(TLV, preamble=TLV_PREAMBLE)

	assert "if (item.field == tag) {" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_generated_walk_compiles(tmp_path: Path) -> None:
	result = compiles(tmp_path, TLV, preamble=TLV_PREAMBLE)
	assert result.returncode == 0, result.stderr


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_generated_walk_reads_protoc_output(tmp_path: Path) -> None:
	"""The same vectors the C suite uses, which came out of protoc."""
	schema   = parse_text(TLV_PREAMBLE + TLV)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text('''
#include <cstring>
#include "unit.hpp"

static const std::uint8_t WIRE[] = {
	0x08, 0x96, 0x01,
	0x12, 0x04, 's', 'i', 't', 'u',
	0x1d, 0x00, 0x00, 0xC0, 0x3F,
};

int main()
{
	std::uint8_t buf[sizeof(WIRE)];
	std::memcpy(buf, WIRE, sizeof(WIRE));

	situ::rt::message owner(buf, sizeof(buf));
	situ::S msg;
	if (situ::S::at(owner, 0, sizeof(WIRE), msg) != situ::rt::err::ok) return 1;
	if (msg.fields_count() != 3u) return 2;

	situ::S::fields_item item{};
	if (msg.user_id(item) != situ::rt::err::ok) return 3;
	if (item.wire != 0u || item.at != 0u) return 4;

	if (msg.label(item) != situ::rt::err::ok) return 5;
	if (item.wire != 2u || item.value_len != 4u || item.at != 3u) return 6;
	if (std::memcmp(msg.base() + item.value_at, "situ", 4) != 0) return 7;

	/* A wire type the schema refuses. */
	std::uint8_t group[] = { 0x0B };
	situ::rt::message other(group, sizeof(group));
	situ::S bad;
	if (situ::S::at(other, 0, 1, bad) != situ::rt::err::ok) return 8;
	if (bad.fields_first(item) != situ::rt::err::constraint) return 9;

	return 0;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- indexed regions (section 9.3) ------------------------------------------

INDEXED = ("struct R { u32 id; u16 kind; }"
	"struct V { u16 len; u8 body[len]; }"
	"struct S { u16 n; indexed(offset_type = u16, count = n)"
	" { R fixed[]; } }"
	"struct T { u16 n; indexed(offset_type = u16, count = n)"
	" { V varying[]; } }")


def test_an_indexed_region_gets_its_table_walked() -> None:
	"""It answered REGION in the shared classifier and this backend emitted
	"not in the static subset yet" -- the fallthrough note, for the last
	construct no backend reached into."""
	header = emit(INDEXED)

	assert "fixed_count() const noexcept" in header
	assert "fixed_offset(std::uint32_t index," in header
	assert "fixed_at(std::uint32_t index," in header
	assert "not in the static subset yet" not in header


def test_an_index_entry_is_read_in_the_region_s_byte_order() -> None:
	header = emit(INDEXED)

	assert "situ_get_be16(raw_.base + at)" in header


def test_an_index_over_variable_elements_measures_one() -> None:
	"""`_extent_method` gated the extent on runs and nested members, so an
	indexed region asked for one that was never emitted."""
	header = emit(INDEXED)

	assert "::situ::V(raw).extent()" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_index_reaches_elements_in_any_order(tmp_path: Path) -> None:
	"""The whole point of the table. A walk over an ascending table proves
	nothing, so the offsets here are deliberately out of order."""
	schema   = parse_text(PREAMBLE + INDEXED)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text('''
#include <cstring>
#include "unit.hpp"

static const std::uint8_t S_BYTES[] = {
	0x00, 0x03,
	0x00, 0x12, 0x00, 0x06, 0x00, 0x0C,
	0x00, 0x00, 0x00, 0xBB, 0x00, 0x02,
	0x00, 0x00, 0x00, 0xCC, 0x00, 0x03,
	0x00, 0x00, 0x00, 0xAA, 0x00, 0x01,
};
static const std::uint8_t T_BYTES[] = {
	0x00, 0x02,
	0x00, 0x04, 0x00, 0x0B,
	0x00, 0x05, 'h', 'e', 'l', 'l', 'o',
	0x00, 0x02, 'h', 'i',
};

int main()
{
	std::uint8_t buf[64];

	std::memcpy(buf, S_BYTES, sizeof(S_BYTES));
	situ::rt::message owner(buf, sizeof(S_BYTES));
	situ::S s;
	if (situ::S::at(owner, 0, sizeof(S_BYTES), s) != situ::rt::err::ok) return 1;
	if (s.fixed_count() != 3u) return 2;

	situ::R e;
	if (s.fixed_at(0, e) != situ::rt::err::ok || e.id() != 170u) return 3;
	if (s.fixed_at(1, e) != situ::rt::err::ok || e.id() != 187u) return 4;
	if (s.fixed_at(2, e) != situ::rt::err::ok || e.id() != 204u) return 5;
	if (s.fixed_at(3, e) != situ::rt::err::bounds) return 6;

	std::memcpy(buf, T_BYTES, sizeof(T_BYTES));
	situ::rt::message other(buf, sizeof(T_BYTES));
	situ::T t;
	if (situ::T::at(other, 0, sizeof(T_BYTES), t) != situ::rt::err::ok) return 7;

	/* Each element is narrowed to its own extent, not to the rest. */
	situ::V v;
	if (t.varying_at(0, v) != situ::rt::err::ok || v.limit() != 7u) return 8;
	if (std::memcmp(v.body().data(), "hello", 5) != 0) return 9;
	if (t.varying_at(1, v) != situ::rt::err::ok || v.limit() != 4u) return 10;
	if (std::memcmp(v.body().data(), "hi", 2) != 0) return 11;

	return 0;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- varint fields (section 8.1.1) ------------------------------------------

VARINT = "varint_type v { encoding = leb128; max_bits = 64; minimal; }"


def test_a_varint_field_decodes() -> None:
	"""It classified as NOTHING and this backend emitted nothing at all --
	not an accessor and not a note, so the member simply was not there."""
	header = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "n(std::uint64_t &out) const noexcept" in header
	assert "n_len() const noexcept" in header


def test_a_member_after_a_varint_is_placed_past_it() -> None:
	"""It used to say "its offset cannot be resolved" and emit nothing, which
	is the safe half of the gap -- C placed it at zero instead and read the
	varint's own bytes."""
	header = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "situ_advance_u32(1, n_len(), raw_.limit)" in header
	assert "s.after: its offset cannot be resolved" not in header


def test_a_varint_may_size_an_array() -> None:
	header = emit(VARINT + "struct S { v n; u8 payload[n]; }")

	assert "n_value()" in header
	assert "cannot resolve" not in header


def test_a_minimal_varint_refuses_a_padded_encoding() -> None:
	header = emit(VARINT + "struct S { v n; }")

	assert "if (used != situ_varint_len(raw)) {" in header
	assert "return ::situ::rt::err::constraint;" in header


def test_a_zigzag_varint_decodes_signed() -> None:
	header = emit("varint_type z { encoding = leb128; max_bits = 64;"
	              " transform = zigzag; }struct S { z n; }")

	assert "n(std::int64_t &out) const noexcept" in header
	assert "situ_zigzag_decode(raw)" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_a_varint_reads_the_bytes_after_it(tmp_path: Path) -> None:
	schema   = parse_text(PREAMBLE + VARINT
	                      + "struct S { u8 kind; v n; u16 after; }")
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text('''
#include "unit.hpp"

int main()
{
	/* kind = 1, n = 300 (leb128 AC 02), after = 0xBEEF */
	std::uint8_t buf[] = { 0x01, 0xAC, 0x02, 0xBE, 0xEF };
	situ::rt::message owner(buf, sizeof(buf));
	situ::S s;
	if (situ::S::at(owner, 0, sizeof(buf), s) != situ::rt::err::ok) return 1;

	std::uint64_t n = 0;
	if (s.n(n) != situ::rt::err::ok || n != 300u) return 2;
	if (s.n_len() != 2u) return 3;
	if (s.after() != 0xBEEFu) return 4;

	/* A padded encoding of 1, which `minimal` refuses. */
	std::uint8_t padded[] = { 0x01, 0x81, 0x00, 0xBE, 0xEF };
	situ::rt::message other(padded, sizeof(padded));
	situ::S p;
	if (situ::S::at(other, 0, sizeof(padded), p) != situ::rt::err::ok) return 5;
	if (p.n(n) != situ::rt::err::constraint) return 6;

	return 0;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


BE128 = "varint_type sq { encoding = be128; max_bits = 64; max_bytes = 9; }"


def test_a_be128_field_uses_the_big_endian_reader() -> None:
	"""The groups come from the other end, so the leb128 reader would hand
	back a plausible number and not the one on the wire. This asserted the
	refusal one commit ago -- invariant 11, and the shelf life was a day."""
	header = emit(BE128 + "struct S { sq n; u16 after; }")

	assert "situ_varint_be_get(raw_.base + at, raw_.limit - at, 9u, 8u, &raw)" in header
	assert "is not an encoding this" not in header


def test_a_member_after_a_be128_is_placed_past_it() -> None:
	header = emit(BE128 + "struct S { sq n; u16 after; }")

	assert "n_len()" in header
	assert "S.after: its offset cannot be resolved" not in header


def test_the_arm_types_of_a_variant_come_before_it(tmp_path: Path) -> None:
	"""Ordering was by containment over `own_entries`, which a variant's arms
	are not in -- so an arm type came out in the right place by alphabet, and
	would not have for a schema naming them the other way round."""
	header = emit("enum K : u8 { a = 1, b = 2, }"
	              "struct Zeta { u16 x; } struct Yank { u32 y; }"
	              "struct S { K k;"
	              " variant v switch (k) { case K.a: Zeta p; case K.b: Yank q; } }")

	assert header.index("class Zeta") < header.index("class S")
	assert header.index("class Yank") < header.index("class S")


# -- a coded region that ends at a delimiter (13.6) -------------------------

STUFFED = ("codec stuff { kernel = stuffing(worst_case = 4, per = 3,"
	" unit = stream, code = smtp_dot); }\nimpl stuff derived;\n"
	'struct S { coded body(stuff) until "\\r\\n.\\r\\n" '
	"{ u8 content[remaining]; } }")


def test_a_delimited_coded_region_says_the_bytes_are_encoded() -> None:
	"""It emitted the bytes and nothing else, so a reader had no way to know
	they were not the value."""
	header = emit(STUFFED, preamble=PREAMBLE)

	assert "is `stuff` output, and the bytes above" in header
	assert "The scan runs on the encoded bytes" in header


def test_a_stuffing_kernel_gets_a_decode_accessor() -> None:
	"""Table-only, on the argument that the other families were described and
	not generated -- which stopped being true without the comment noticing."""
	header = emit(STUFFED, preamble=PREAMBLE)

	assert "body_decode(std::uint8_t *out," in header
	assert "situ_stuff_decode(body().data()," in header


def test_the_decode_runs_over_the_content_and_not_the_delimiter() -> None:
	header = emit(STUFFED, preamble=PREAMBLE)

	assert "const std::uint32_t encoded = body_len();" in header
	# `_span` includes the delimiter, which is not the codec's to transform.
	assert "const std::uint32_t encoded = body_span();" not in header


def test_a_byte_kernel_is_handed_bytes() -> None:
	"""`unit` decides. Passing a byte count to a bit loop decodes an eighth of
	the region and returns confidently."""
	header = emit(STUFFED, preamble=PREAMBLE)

	assert "encoded, out);" in header
	assert "std::uint32_t len, std::uint8_t *out);" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_decode_unstuffs_a_real_body(tmp_path: Path) -> None:
	"""RFC 5321 section 4.5.2: the receiver removes one period from a line
	that starts with one, and the terminator is the only bare dot."""
	from situc.codegen.c import derived

	schema   = parse_text(PREAMBLE + STUFFED)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	# The derived file includes `unit.h`, whose `extern "C"` guard is what
	# gives its definitions C linkage when g++ compiles them.
	(tmp_path / "unit.h").write_text(
		generate_c(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "impl.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")
	(tmp_path / "main.cpp").write_text('''
#include <cstring>
#include "unit.hpp"

int main()
{
	static const char WIRE[] = "a\\r\\n..b\\r\\n\\r\\n.\\r\\n";
	static const char WANT[] = "a\\r\\n.b\\r\\n";

	std::uint8_t buf[64];
	std::uint8_t out[64];
	std::uint32_t len = 0;

	std::memcpy(buf, WIRE, sizeof(WIRE) - 1);
	situ::rt::message owner(buf, sizeof(WIRE) - 1);
	situ::S s;
	if (situ::S::at(owner, 0, sizeof(WIRE) - 1, s) != situ::rt::err::ok) return 1;
	if (s.body_decode(out, sizeof out, len) != situ::rt::err::ok) return 2;
	if (len != sizeof(WANT) - 1 || std::memcmp(out, WANT, len) != 0) return 3;
	if (s.body_decode(out, 1, len) != situ::rt::err::bounds) return 4;
	return 0;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"), str(tmp_path / "impl.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- an array of struct elements --------------------------------------------

ELEMENTS = ("struct reading { i16 value; u8 channel; }"
	"struct frame { u8 version; reading readings[8]; u32 checksum; }")


def test_an_array_of_structs_gets_an_indexed_accessor() -> None:
	"""It said "element type reading is not in the static subset yet", which
	the subset had nothing to do with: the branch wanted a byte scalar and a
	struct element has no scalar at all. The other three all emitted one."""
	header = emit(ELEMENTS)

	assert "readings(std::uint32_t index," in header
	assert "not in the static subset yet" not in header


def test_an_element_index_is_bounded_by_the_count() -> None:
	"""Bounded by the count and not only by the extent: bytes after the array
	are inside the view and are not elements."""
	header = emit(ELEMENTS)

	assert "if (index >= 8u) {" in header
	assert "reading::size_bytes" in header


def test_an_element_of_no_single_size_is_refused() -> None:
	"""Element N is not at a constant stride, so there is no index to compute."""
	header = emit("struct v { u8 n; u8 body[n]; }"
	              "struct frame { v items[4]; }")

	assert "has no single size" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_elements_are_where_c_puts_them(tmp_path: Path) -> None:
	"""The claim every backend carries: that it describes the same bytes as
	the C. Written through C's accessors and read back through this one."""
	schema   = parse_text(PREAMBLE + ELEMENTS)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "unit.h").write_text(
		generate_c(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "unit.c").write_text(
		generate_c(schema, resolved, "unit").source, encoding="ascii")
	(tmp_path / "main.cpp").write_text('''
#include <cstring>
#include "unit.hpp"
extern "C" {
#include "unit.h"
}

int main()
{
	std::uint8_t buf[64];
	std::memset(buf, 0, sizeof buf);

	situ_msg_t msg;
	situ_view_t view;
	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_frame_view(&msg, 0, &view) != SITU_OK) return 1;

	for (std::uint32_t i = 0; i < 8; i++) {
		situ_view_t e;
		if (situ_frame_readings_at(view, i, &e) != SITU_OK) return 2;
		situ_reading_channel_set(e, static_cast<std::uint8_t>(i + 1));
		situ_reading_value_set(e, static_cast<std::int16_t>(-i));
	}

	situ::rt::message owner(buf, sizeof buf);
	situ::frame f;
	if (situ::frame::at(owner, 0, f) != situ::rt::err::ok) return 3;

	for (std::uint32_t i = 0; i < 8; i++) {
		situ::reading r;
		if (f.readings(i, r) != situ::rt::err::ok) return 4;
		if (r.channel() != i + 1) return 5;
		if (r.value() != -static_cast<std::int16_t>(i)) return 6;
	}

	situ::reading r;
	if (f.readings(8, r) != situ::rt::err::bounds) return 7;
	return 0;
}
''', encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"), str(tmp_path / "unit.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- an endian marker (section 8.3) -----------------------------------------

MARKED = ("endian_marker order : u16 { little = 0x4949, big = 0x4D4D, }\n"
	"struct hdr [endian = from(order)] { endian_marker order; u16 magic;"
	" u32 offset; }")


def test_a_marker_gets_its_constants_and_predicate() -> None:
	"""It said "not in the static subset yet" and then read every field the
	marker governs big-endian regardless."""
	header = emit(MARKED)

	assert "order_little = 0x4949u" in header
	assert "order_big = 0x4D4Du" in header
	assert "bool order_is_little() const noexcept" in header
	assert "not in the static subset yet" not in header


def test_a_governed_field_branches_on_the_marker() -> None:
	"""The map said `ConditionallyConverted(order)` on these the whole time,
	and the read was unconditional -- so a little-endian frame came back
	byte-swapped with no diagnostic."""
	header = emit(MARKED)

	assert "order_is_little() ? situ_get_le16" in header
	assert "order_is_little() ? situ_get_be16" not in header


def test_the_setter_agrees_with_the_getter() -> None:
	"""Or a round trip through one view swaps the value."""
	header = emit(MARKED)

	assert "if (order_is_little()) {" in header
	assert "situ_put_le32" in header and "situ_put_be32" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_both_byte_orders_read_the_same_values(tmp_path: Path) -> None:
	"""TIFF's own header, in both orders. Little-endian is the common case and
	is the one that was wrong."""
	schema   = parse_text("target buffer;\nendian big;\n" + MARKED)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "main.cpp").write_text("""
#include <cstring>
#include "unit.hpp"

static int one(const std::uint8_t *bytes)
{
	std::uint8_t buf[8];
	std::memcpy(buf, bytes, 8);
	situ::rt::message owner(buf, sizeof buf);
	situ::hdr h;
	if (situ::hdr::at(owner, 0, h) != situ::rt::err::ok) return 1;
	return (h.magic() == 42 && h.offset() == 8) ? 0 : 2;
}

int main()
{
	static const std::uint8_t LE[] = { 'I','I', 0x2A,0x00, 0x08,0x00,0x00,0x00 };
	static const std::uint8_t BE[] = { 'M','M', 0x00,0x2A, 0x00,0x00,0x00,0x08 };

	if (one(LE)) return 1;
	if (one(BE)) return 2;

	std::uint8_t buf[8];
	std::memcpy(buf, LE, sizeof buf);
	situ::rt::message owner(buf, sizeof buf);
	situ::hdr h;
	if (situ::hdr::at(owner, 0, h) != situ::rt::err::ok) return 3;
	h.set_offset(0x12345678u);
	if (h.offset() != 0x12345678u) return 4;
	if (buf[4] != 0x78) return 5;
	return 0;
}
""", encoding="ascii")

	assert HOST_CXX is not None
	binary = tmp_path / "probe"
	build = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a fixed-width text number (section 8.6.2) ------------------------------

TEXT = "struct reply { decimal u16 code[3]; u8 sep; }"


def test_a_fixed_width_text_number_parses() -> None:
	"""It read as an array: the bracket is a width in bytes and the branch
	took it for a count."""
	header = emit(TEXT)

	assert "code(std::uint16_t &out) const noexcept" in header
	assert "code_digits() const noexcept" in header
	assert "situ_parse_uint(raw_.base + (0), 3u, 10u, 999u, &value)" in header


def test_the_range_is_the_fields_not_the_types() -> None:
	"""`decimal u16 code[3]` holds 0..999, and a check written against `u16`
	would accept a value the three bytes cannot represent."""
	header = emit(TEXT)

	assert "0..999" in header
	assert "999u, &value" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_digits_parse_as_smtp_writes_them(tmp_path: Path) -> None:
	"""Padded, and the leading zero required rather than tolerated -- which is
	what makes this Canonical where a delimited text number is not."""
	compiled = compiles(tmp_path, TEXT, extra='''
#include <cstring>
#include "unit.hpp"

static int one(const char *line, unsigned want, bool ok)
{
	std::uint8_t buf[8];
	std::memset(buf, 0, sizeof buf);
	std::memcpy(buf, line, 4);

	situ::rt::message owner(buf, sizeof buf);
	situ::reply r;
	if (situ::reply::at(owner, 0, r) != situ::rt::err::ok) return 1;

	std::uint16_t code = 0;
	const auto e = r.code(code);
	if (ok) return (e == situ::rt::err::ok && code == want) ? 0 : 2;
	return e == situ::rt::err::constraint ? 0 : 3;
}

int main()
{
	if (one("250 ", 250, true)) return 1;
	if (one("007 ", 7, true))   return 2;
	if (one("2x0 ", 0, false))  return 3;
	if (one("25  ", 0, false))  return 4;
	return 0;
}
''')
	assert compiled.returncode == 0, compiled.stderr


# -- a tag's coverage, dirty bit and finalize (section 14.2) ----------------

TAGGED = ("struct s { u8 hop; authenticated { u16 seq; u8 body[4]; }"
	" tag u8 mac[16]; }")


def test_a_tag_gets_its_bytes_span_and_dirty_bit() -> None:
	"""It emitted the dirty constant and the setters that mark it, and then
	said "not in the static subset yet" about the tag -- so a caller could be
	told a write left the tag stale and had no way to reach it."""
	header = emit(TAGGED)

	assert "::situ::rt::bytes mac() const noexcept" in header
	assert "mac_covered(std::uint32_t &offset," in header
	assert "mac_is_dirty(const ::situ::rt::message &owner)" in header
	assert "mac_finalize(::situ::rt::message &owner)" in header
	assert "not in the static subset yet" not in header


def test_a_gap_in_the_coverage_gets_no_span() -> None:
	"""A range covering bytes the tag does not is worse than no range."""
	header = emit("struct s { authenticated a { u16 x; } u32 gap;"
	              " authenticated b { u16 y; } tag u8 mac[4] covers(a, b); }")

	assert "mac_covered" not in header


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_span_and_the_bit_behave(tmp_path: Path) -> None:
	compiled = compiles(tmp_path, TAGGED, extra='''
#include <cstring>
#include "unit.hpp"

int main()
{
	std::uint8_t buf[32];
	std::memset(buf, 0, sizeof buf);

	situ::rt::message owner(buf, sizeof buf);
	situ::s v;
	if (situ::s::at(owner, 0, v) != situ::rt::err::ok) return 1;

	std::uint32_t at = 0, len = 0;
	if (v.mac_covered(at, len) != situ::rt::err::ok) return 2;
	if (at != 1 || len != 6) return 3;      /* the authenticated region */
	if (v.mac().size() != 16) return 4;

	if (situ::s::mac_is_dirty(owner)) return 5;
	v.set_seq(owner, 0x1234);
	if (!situ::s::mac_is_dirty(owner)) return 6;
	situ::s::mac_finalize(owner);
	if (situ::s::mac_is_dirty(owner)) return 7;
	return 0;
}
''')
	assert compiled.returncode == 0, compiled.stderr


# -- the offset cache (decision 0022) ---------------------------------------

KV = ('struct kv { u8 k[] until ": "; u8 v[] until "\\r\\n"; }\n'
	'struct block { u8 head[] until ";"; kv entries[] until "\\r\\n";'
	' u8 tail[remaining]; }')


def test_a_member_after_a_run_uses_the_runs_from_helper() -> None:
	"""A member after a run is placed through the run's `_from` helper.

	The runs were the last member kind without one: every accumulating pass
	holds the base already, and the plain span re-resolves it by rescanning
	everything before the run. Recorded as marginal in 26.31, and that
	depended on what precedes the run -- with a 400-byte member ahead of a
	twenty-record run, resolving what follows it is 1.6x faster for dropping
	the rescan."""
	header = emit(KV)

	assert "at = situ_advance_u32(at, entries_span_from(at), raw_.limit);" in header
	assert "std::uint32_t entries_span_from(std::uint32_t start)" in header


CHAIN = ('struct line { u8 method[] until " "; u8 target[] until " ";'
	' u8 version[] until "\\r\\n"; }')


def test_the_offset_cache_is_behind_the_flag() -> None:
	"""Memory the caller did not ask for, which is a deployment decision and
	not a schema one (0022)."""
	assert "resolve_offsets" not in emit(CHAIN)


def test_the_offset_cache_resolves_every_dynamic_member() -> None:
	header = emit_materialized(CHAIN)

	assert "struct offsets {" in header
	assert "std::uint32_t target;" in header
	assert "std::uint32_t version;" in header
	assert "void resolve_offsets(offsets &out) const noexcept" in header


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_cache_agrees_with_the_per_member_offsets(tmp_path: Path) -> None:
	"""Which is the whole point: one pass instead of a rescan per member, and
	the same answer.

	C, Rust and Python each had this executed and this backend had the
	structural assertions only -- that the struct is declared and the method
	is there. A cache that resolved a member to the wrong place would satisfy
	every one of them. Section 22's own warning, in the file that ought to
	know it: reading generated text is not running it."""
	result = compiles(tmp_path, CHAIN, materialize=True, extra="""
#include <cstring>
#include "unit.hpp"

int main()
{
	static const char raw[] = "GET /index.html HTTP/1.1\\r\\n";
	std::uint8_t buf[64];
	std::memcpy(buf, raw, sizeof raw - 1);

	const situ::line view{ situ_view_t{ buf, sizeof raw - 1, 0 } };
	situ::line::offsets at;
	view.resolve_offsets(at);

	if (at.target != view.target_offset())   return 1;
	if (at.version != view.version_offset()) return 2;
	if (at.target != 4u || at.version != 16u) return 3;

	/* And a span from a known base is the span that resolves its own. */
	if (view.target_span_from(at.target) != view.target_span()) return 4;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_the_last_advance_is_trimmed() -> None:
	"""It moves a total nobody reads again -- dead arithmetic, and an
	`unused_assignments` error in Rust, which builds under `-D warnings`."""
	header = emit_materialized(CHAIN)
	body   = header.split("resolve_offsets")[1]

	assert body.count("at +=") == 2	# method and target; not version


def test_an_opaque_region_hands_back_its_bytes() -> None:
	"""Treat-as-bytes is the whole of what the construct supports, and this
	supported none of it -- the fallthrough note claiming a language limit
	where C hands back a pointer and a length."""
	header = emit("struct s { u16 n; opaque payload [n]; }")

	assert "::situ::rt::bytes payload() const noexcept" in header
	assert "not in the static subset yet" not in header


def test_a_member_after_a_sealed_region_is_placed() -> None:
	"""Only C computed a coded region's length, so the other three could place
	nothing after one -- `example/packet`'s tag among them."""
	header = emit("codec seal { granularity = byte; length_preserving;"
	              " seekable; authenticated; invertible; deterministic; }\n"
	              "impl seal extern \"x\";\n"
	              "struct s { u16 n; sealed(seal) { u8 body[n]; }"
	              " tag u8 mac[16]; }")

	assert "mac_covered" in header
	assert "cannot resolve where the tag sits" not in header


WIDE = "struct w { u8 kind; u16 samples[4]; i32 deltas[2]; }"


def test_an_array_of_wide_scalars_gets_an_indexed_getter() -> None:
	"""C emits one; this said "element type u16 is not in the static subset
	yet" -- the array branch wanting a byte scalar, as it did for struct
	elements."""
	header = emit(WIDE)

	assert "std::uint16_t samples(std::uint32_t index) const noexcept" in header
	assert "std::int32_t deltas(std::uint32_t index) const noexcept" in header
	assert "not in the static subset yet" not in header


def test_a_wide_element_gets_no_pointer() -> None:
	"""It is ValueConverted, so a pointer into it would alias bytes that are
	not the value -- C's rule and the reason it gives."""
	header = emit(WIDE)

	assert "::situ::rt::bytes samples()" not in header
	assert "would alias bytes that are not the value" in header


# -- an offset the message chooses (26.27) ---------------------------------


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_a_member_past_the_frame_is_empty_not_read(tmp_path: Path) -> None:
	"""A message that says its payload is a thousand bytes, in seventy of them.

	`example/packet` puts its tag after a sealed region sized by `hdr.length`,
	so a length the frame cannot hold resolves the tag past the end of it.
	Found by `make fuzz` three seconds into the first run that was fuzzing
	rather than eight random inputs (26.27), and the answer is the one already
	settled: the accessor answers safely, and `validate` reports the message as
	malformed rather than short.
	"""
	result = compiles(
		tmp_path,
		(ROOT / "example" / "packet" / "packet.situ").read_text(encoding="ascii"),
		preamble="", extra=r"""
#include <cstring>
#include "unit.hpp"

int main()
{
	std::uint8_t raw[70] = { 0 };
	raw[4] = 1;			/* hdr.version, [must_eq = 1] */
	raw[5] = 1;			/* hdr.type = hello */
	raw[6] = 0x03;			/* hdr.length = 1000, inside `[max = 1024]` */
	raw[7] = 0xe8;

	situ::packet view{ situ_view_t{ raw, sizeof raw, 0 } };

	if (!view.tag().empty())                             return 1;
	if (view.validate() != situ::rt::err::bounds)        return 2;

	raw[6] = 0;
	raw[7] = 8;
	if (view.tag().size() != 16)                         return 3;
	if (view.tag().data() != raw + 54)                   return 4;
	if (view.validate() != situ::rt::err::ok)            return 5;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- framing a run (20.3) ---------------------------------------------------


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_every_prefix_of_a_real_request_answers_honestly(tmp_path: Path) -> None:
	"""The claim the entry made: an HTTP header block could not be framed.

	`example/http` rather than a schema written for this, because that is what
	the entry named and 26.32's rule is that the worked example is the claim.
	Every prefix of a real request must come back truncated, and every bound
	must be a bound: greater than what is in hand, and never more than the
	message turns out to be. A framer that overshoots stalls waiting for bytes
	that will not come."""
	result = compiles(
		tmp_path,
		(ROOT / "example" / "http" / "http.situ").read_text(encoding="ascii"),
		preamble="", extra=r"""
#include "unit.hpp"

const char req[] =
	"GET /index.html HTTP/1.1\r\n"
	"Host: example.com\r\n"
	"Accept: */*\r\n"
	"\r\n";

int main()
{
	const std::uint32_t whole = sizeof req - 1;
	const auto *data = reinterpret_cast<const std::uint8_t *>(req);
	std::uint32_t need;

	for (std::uint32_t i = 0; i < whole; i++) {
		if (situ::request_head::required(data, i, need)
		    == situ::rt::err::ok) return 1;
		if (need <= i || need > whole)  return 2;
	}
	if (situ::request_head::required(data, whole, need)
	    != situ::rt::err::ok)       return 3;
	return need == whole ? 0 : 4;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


# -- a class a member has taken the name of ---------------------------------


def test_a_struct_named_for_one_of_its_members_is_renamed_and_aliased() -> None:
	"""C++ declares a class's own name inside the class, so no member may take
	it. `struct option { u8 option; ... }` is a shape a real protocol produces
	without trying, and `class option { std::uint8_t option(); }` is not C++.

	Refusing the schema was the alternative and it is the worse one: it would
	make a struct illegal for a reason that has nothing to do with its bytes,
	in one backend of four. So the class moves and the schema's name becomes an
	alias for it -- every accessor keeps its name, and every other class goes
	on naming this one the way the schema does."""
	header = emit("struct option { u8 option; u8 length; }")

	assert "class option_ : public ::situ::rt::view {" in header
	assert "using option = option_;" in header
	assert "std::uint8_t option() const noexcept" in header
	assert "void set_option(std::uint8_t value) noexcept" in header


def test_a_struct_named_for_a_method_every_view_has_is_renamed() -> None:
	"""`framed` is not an accessor the schema asked for: every view gets one,
	so nothing in the schema names it and nothing in the schema can avoid it.

	This is the one `test/schema/edges.situ` carried. The file exists to hold
	the constructs no worked example has, and the compile check globbed
	`example/`, so the backend went weeks emitting a header no C++ compiler
	accepts (26.31)."""
	header = emit('struct framed { u8 magic[4]; u8 label[] until "\\0"; }')

	assert "class framed_ : public ::situ::rt::view {" in header
	assert "using framed = framed_;" in header
	assert "::situ::rt::err framed(std::uint32_t &need) const noexcept" in header
	# The self-references inside the class body cannot use the alias: it is
	# declared after the class, and a member of that name hides it anyway.
	assert "framed_ &out) noexcept" in header
	assert "return framed_{ situ_view_t{" in header


def test_a_register_named_for_one_of_its_own_members_is_renamed_too() -> None:
	"""A register is a class as much as a view is: `read()` and `write()` are
	the two bus transactions, so a register called `read` meets the same rule.

	It emits from its own branch, which returns before the rest of a struct is
	built -- so the first version of this renamed the class and emitted no
	alias, leaving a type nothing in the schema could name. Half a rename is
	worse than either half alone."""
	header = emit("""register read @ 0x00 {
	width        = 32;
	access_width = 32;
	bit  enable  [rw];
	u3   mode    [rw];
	}
	""", preamble=MMIO)

	assert "class read_ {" in header
	assert "using read = read_;" in header
	assert "[[nodiscard]] word read() const noexcept" in header


def test_a_struct_no_member_has_named_keeps_its_name() -> None:
	"""The rename is not free -- it is a second name for one type -- so it is
	emitted only where C++ requires it."""
	header = emit("struct option { u8 kind; u8 length; }")

	assert "class option : public ::situ::rt::view {" in header
	assert "using option" not in header


def test_a_rename_with_nowhere_to_go_is_a_diagnostic() -> None:
	"""One underscore is free in every case but one: a schema holding both
	`framed` and `framed_` would have the alias for the first and the class for
	the second reach the same name. Two names one character apart is a
	coincidence rather than a construct, so this says so and stops rather than
	inventing a second escape nobody could predict."""
	with pytest.raises(SituError) as raised:
		emit('struct framed { u8 magic[4]; u8 label[] until "\\0"; }\n'
		     "struct framed_ { u8 a; }")

	assert "needs another name for its C++ class" in str(raised.value)


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_the_renamed_class_reads_the_same_bytes(tmp_path: Path) -> None:
	"""The alias is the schema's name and the two are one type: a caller writes
	`situ::option`, a container returns one, and neither is aware anything was
	renamed."""
	result = compiles(tmp_path, "struct option { u8 option; u8 length; }\n"
	                            "struct frame { option first; u8 rest[2]; }",
	                  extra="""
#include "unit.hpp"

int main()
{
	std::uint8_t buf[4] = { 0x11, 0x22, 0x33, 0x44 };
	situ::rt::message msg(buf, sizeof buf);

	situ::option one;
	if (situ::option::at(msg, 0, one) != situ::rt::err::ok) return 1;
	if (one.option() != 0x11 || one.length() != 0x22)       return 2;

	const situ::frame whole{ situ_view_t{ buf, sizeof buf, 0 } };
	situ::option held;
	if (whole.first(held) != situ::rt::err::ok)              return 6;
	if (held.option() != 0x11)                              return 3;

	one.set_option(0x99);
	if (one.option() != 0x99 || buf[0] != 0x99)             return 4;
	return 0;
}
""")
	assert result.returncode == 0, result.stderr

	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX or "g++", *[w for w in WARNINGS if w != "-fsyntax-only"],
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	assert subprocess.run([str(binary)]).returncode == 0


def test_the_affixes_match_the_emitter() -> None:
	"""Which classes move is decided from the schema rather than by reading the
	emitted text back, which section 25 forbids. That costs completeness: the
	rule has to know every shape a member name reaches, and the emitter is free
	to add one.

	So the lists are held to the emitter's own source. An accessor named
	`{name}_capacity()` arriving in some later phase fails this until
	`cpp/names.py` learns the suffix -- which is cheaper than the alternative,
	a header that does not compile for a schema nobody in this tree has
	written."""
	source = (ROOT / "situc" / "codegen" / "cpp" / "emit.py").read_text(
		encoding="ascii")

	# Anything the emitter interpolates a name into, whatever the variable
	# holding it is called: `{name}_len`, `set_{name}`, `{holder}_gate`. The
	# free functions of the C runtime are not members and are dropped.
	suffixes = {affix for affix in re.findall(r"\{\w+\}_([a-z][a-z0-9_]*)", source)
	            if not affix.startswith("situ")}
	prefixes = {affix for affix in re.findall(r"\b([a-z][a-z0-9_]*)_\{\w+\}", source)
	            if not affix.startswith("situ")}

	assert suffixes <= SUFFIXES, f"unlisted suffixes: {sorted(suffixes - SUFFIXES)}"
	assert prefixes <= PREFIXES, f"unlisted prefixes: {sorted(prefixes - PREFIXES)}"


# -- a struct named for a C++ keyword (26.116) ------------------------------
#
# `bare_name` has mangled a *member* called `class` or `operator` since
# decision 0025, and the class itself went out verbatim: `struct class`
# emitted `class class : public ::situ::rt::view` and g++ reported six errors
# naming neither the schema nor situc. It was found by naming a test struct
# `protected` and watching the four-backend differential fail to build.
#
# The remedy 26.116 first proposed was a front-end refusal, and `cpp/names.py`
# already argues against exactly that: refusing makes `class` and `operator`
# reserved words in one backend, and DNS has fields called both. The class
# moves instead, which is what this file's whole rename mechanism is for.

KEYWORD_STRUCT = "struct class { u8 x; }\n"


def test_a_struct_named_for_a_keyword_moves() -> None:
	header = emit(KEYWORD_STRUCT)
	assert "class class_ : public" in header
	assert "class class :" not in header


def test_no_alias_is_emitted_for_a_keyword_name() -> None:
	"""The alias exists so a caller may write the schema's name. A keyword is
	the one name they cannot write, so `using class = class_;` would be as
	illegal as the declaration it stood in for."""
	header = emit(KEYWORD_STRUCT)
	assert "using class =" not in header
	assert "is a C++ keyword" in header


def test_a_keyword_named_struct_compiles(tmp_path: Path) -> None:
	"""The assertion that would have caught this in the first place: the
	other three backends accept the schema, and only compiling the fourth
	says whether it is legal."""
	result = compiles(tmp_path, KEYWORD_STRUCT)
	assert result.returncode == 0, result.stderr


def test_the_other_spelling_is_still_refused() -> None:
	"""`class` and `class_` in one schema reach one C++ name. Decision 0013's
	gate has refused two constructs that land on one identifier since the day
	it was written, and it covers this without knowing about keywords."""
	with pytest.raises(SituError) as caught:
		emit("struct class { u8 x; }\nstruct class_ { u8 y; }\n")
	assert "`class_` is taken" in caught.value.diagnostic.render()


def test_value_bounds_are_exported_as_constexpr() -> None:
	"""`[min]`/`[max]` as constants a caller can share (26.125).

	The bound was stated once in the schema, enforced in `validate`, and
	reachable from nowhere else -- so hand-written code validating the same
	value before it crosses this wire restated the number and drifted.
	`value_min`/`value_max` rather than `min`/`max` because it is the value's
	domain, not the field's byte size, and because a field named `size` must
	not collide with the struct's own size constants -- which is why that
	field is in the fixture."""
	source = emit("const CAP = 9216;\n"
	              "struct s { u16 mtu [min = 576, max = CAP];"
	              " i8 bias [min = -20]; u16 size [max = 100]; }")
	assert "static constexpr std::uint16_t mtu_value_min = 576;" in source
	assert "static constexpr std::uint16_t mtu_value_max = 9216;" in source
	assert "static constexpr std::int8_t bias_value_min = -20;" in source
	assert "static constexpr std::uint16_t size_value_max = 100;" in source
	assert "bias_value_max" not in source


def test_value_bounds_stay_out_of_the_wrong_domains() -> None:
	"""Fixed point is excluded: the getter's value is scaled, so the raw
	bound would be a constant in the wrong domain -- worse than none."""
	source = emit("struct s { q8_8 trim [max = 100]; u8 pad; }")
	assert "value_max" not in source
