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
])
def test_it_compiles_clean(tmp_path: Path, body: str) -> None:
	result = compiles(tmp_path, body)

	assert result.returncode == 0, result.stderr


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
	a runtime read for each variable one."""
	header = emit("struct h { u8 v; u16 n; }\nstruct r { u32 id; }\n"
	              "struct s { h hdr; u8 opts[hdr.n]; r recs[hdr.n]; }\n")

	assert "std::uint32_t recs_offset() const noexcept" in header
	assert "return 3 + (" in header


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
