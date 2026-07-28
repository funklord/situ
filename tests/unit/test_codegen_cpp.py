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

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

WARNINGS = ["-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
	"-Wsign-conversion", "-fno-exceptions", "-fno-rtti"]


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_cpp(schema, resolved, "unit").header


def compiles(tmp_path: Path, body: str, extra: str = "") -> subprocess.CompletedProcess[str]:
	"""Generate the header, compile it, and hand back the result."""
	schema   = parse_text(PREAMBLE + body)
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


def test_a_construct_outside_the_static_subset_says_so() -> None:
	"""Rather than emitting something that looks complete. A reader has to be
	able to tell a gap from a feature."""
	header = emit("""struct h { u8 v; u16 n; }
	struct s { h hdr; u8 opts[hdr.n]; }
	""")

	assert "not fixed size" in header
	assert "use the C header" in header


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
