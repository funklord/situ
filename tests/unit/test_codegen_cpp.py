"""The C++ backend (section 26.16).

The claim worth testing is not that the C++ compiles -- that is necessary and
easy -- but that it describes the same bytes as the C. Two backends over one
layout that disagree would be worse than one backend, because a schema would
then mean two things.

So the substantial test compiles both headers into one program and compares
them field by field on the same buffer.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime"
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
LIBSITU  = ROOT / "build" / "host" / "runtime" / "libsitu.a"

CPP_USE = '#include "unit.hpp"\nint main()\n{\n\tstd::uint8_t buf[64] = {0};\n\tsitu::rt::message msg(buf, sizeof buf);\n\tsitu::s p;\n\tif (situ::s::at(msg, 0, p) != situ::rt::err::ok) { return 1; }\n\n\tstd::uint16_t seen = 0;\n\tif (p.with_sealed(true, [&](situ::s::sealed_gate g) {\n\t\tseen = g.inner_kind();\n\t}) != situ::rt::err::ok) { return 1; }\n\n\treturn seen == 0 ? 0 : 1;\n}\n'
CPP_FORGE = '#include "unit.hpp"\nint main()\n{\n\tstd::uint8_t buf[64] = {0};\n\tsitu_view_t raw{buf, sizeof buf, 0};\n\tsitu::s::sealed_gate forged(raw);\n\treturn static_cast<int>(forged.inner_kind());\n}\n'

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
	authenticated { h hdr; u8 nonce[12] [nonce]; }
	sealed(aead, nonce = nonce) {
		u16  kind;
		u8   body[hdr.length];
	}
	tag  u8[16];
}
"""

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

WARNINGS = ["-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
	"-Wsign-conversion", "-fno-exceptions", "-fno-rtti"]


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_cpp(schema, resolved, "unit").header


def compiles(tmp_path: Path, body: str, extra: str = "",
		preamble: str = "") -> subprocess.CompletedProcess[str]:
	"""Generate the header, compile it, and hand back the result."""
	schema   = parse_text((preamble or PREAMBLE) + body)
	resolved = resolve(schema, solve(schema))

	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header, encoding="ascii")

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
	assert "::situ::rt::bytes(base() + 0, 4)" in header


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
	if (std::memcmp(cpp.source().octets().data(),
	                situ_addr_octets_ptr(csrc), 4) != 0) { bad++; }
	if (cpp.source().octets().size() != 4) { bad++; }

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

	assert "(limit() - (1))" in header


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
	authenticated { h hdr; u8 nonce[12] [nonce]; }
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
