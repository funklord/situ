"""A recursive member placed by `at`: a chain the message links together.

openmlx4's FS2 image lists its sections this way -- each section's header
says where the next one starts, measured from the start of the image:

	struct chain [depth = 8] {
		hdr   head;
		u8    data[head.size];
		chain following at head.next;
	}

`situc build` accepted it and generated code that compiled in no backend: C
called a `_following_extent_at` and a `_following_offset` it never emitted,
C++ and Rust a `following_count` and an `extent`, and Python's probe read a
count that did not exist. All four had walked the located member as though
it followed `data` inside the frame.

It does not. A located member is reached from the message, so it adds
nothing to its struct's extent -- the layout's minimum already left it out,
and `traverse.extent_parts` now does too -- and the depth probe follows it
through the message, as `validate` already did. Depth is then counted along
the chain like any nested member's, which is what makes a chain that loops
back on itself a CONSTRAINT refusal rather than a hang.

A chain with no way to end cannot validate at all: its last `next` points
somewhere, and outside the message that is BOUNDS. The cases below hold
both refusals and the nesting count, which separates chains of different
lengths where the verdicts alone cannot.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from situc.cli import main

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian big;\n"
          "struct hdr { u32 size; u32 next; }\n"
          "struct chain [depth = 8] {\n"
          "\thdr   head;\n"
          "\tu8    data[head.size];\n"
          "\tchain following at head.next;\n"
          "}\n")


def _sections(*pairs: tuple[int, int, int]) -> bytes:
	"""Sections at the given offsets: (at, size, next), zero-filled between."""
	end = max(at + 8 + size for at, size, _ in pairs)
	out = bytearray(end)
	for at, size, nxt in pairs:
		out[at:at + 8] = struct.pack(">II", size, nxt)
	return bytes(out)


#: name -> (message, verdict, nesting). Nesting saturates at 10, one above
#: the depth this build follows; past `[depth = 8]` the verdict is
#: CONSTRAINT, and a `next` outside the message is BOUNDS.
CASES: dict[str, tuple[bytes, str, int]] = {
	"loops_to_itself": (_sections((0, 2, 0)), "constraint", 10),
	"three_then_out":  (_sections((0, 2, 10), (10, 1, 19), (19, 0, 200)),
	                    "bounds", 2),
	"six_then_out":    (_sections(*((8 * i, 0, 8 * (i + 1)) for i in range(5)),
	                              (40, 0, 1000)), "bounds", 5),
	"twelve":          (_sections(*((8 * i, 0, 8 * (i + 1))
	                                for i in range(12))), "constraint", 10),
	"loops_after_three": (_sections((0, 0, 8), (8, 0, 16), (16, 0, 0)),
	                      "constraint", 10),
}


#: The chain ended on a sentinel (26.1044): FS2's `next == 0xFF000000`,
#: spelled as an arm placed by `at` beside a `nothing` arm, which is how
#: situ writes an optional member elsewhere.
SENTINEL = ("target buffer;\nendian big;\n"
            "struct nothing { }\n"
            "struct hdr { u32 size; u32 next; }\n"
            "struct chain [depth = 8] {\n"
            "\thdr   head;\n"
            "\tu8    data[head.size];\n"
            "\tvariant rest switch (head.next) {\n"
            "\t\tcase 0xFF000000: nothing none;\n"
            "\t\tdefault:         chain following at head.next;\n"
            "\t}\n"
            "}\n")

END = 0xFF000000

#: The same questions with an end to say. Nine sections nest eight deep,
#: which `[depth = 8]` allows; ten do not.
ENDED: dict[str, tuple[bytes, str, int]] = {
	"one":       (_sections((0, 2, END)), "ok", 0),
	"three":     (_sections((0, 2, 10), (10, 1, 19), (19, 0, END)), "ok", 2),
	"three_then_out": (_sections((0, 2, 10), (10, 1, 19), (19, 0, 200)),
	                   "bounds", 2),
	"loops_to_itself": (_sections((0, 2, 0)), "constraint", 10),
	"nine":      (_sections(*((8 * i, 0, 8 * (i + 1)) for i in range(8)),
	                        (64, 0, END)), "ok", 8),
	"ten":       (_sections(*((8 * i, 0, 8 * (i + 1)) for i in range(9)),
	                        (72, 0, END)), "constraint", 9),
	# Not adjacent, with a section-shaped header in the gap claiming 1000
	# bytes: a reader that measured the arm where the variant sits reads
	# that header as the next section. Every other case here puts the next
	# section right after the frame, where the two readings agree.
	"apart":     (_sections((0, 2, 30), (10, 1000, 0), (30, 0, END))[:38],
	              "ok", 1),
}

#: And FS2's other rule, "the list must go forward" (fs2.c refuses
#: `next <= at`): `offset(chain)` in a `when` is where this link sits in the
#: message (26.1045), so a link pointing at or before itself is refused.
FORWARD = SENTINEL + (
	"when chain.head.next != 0xFF000000 && chain.head.next <= offset(chain)\n"
	"\trefuse backwards \"a section points at or before itself\";\n")

GONE: dict[str, tuple[bytes, str, int]] = {
	"forward":   (_sections((0, 0, 10), (10, 0, 20), (20, 0, END)), "ok", 2),
	"backward":  (_sections((0, 0, 20), (20, 0, 10), (10, 0, END)),
	              "constraint", 2),
	"loops_to_itself": (_sections((0, 2, 0)), "constraint", 10),
	"apart":     ENDED["apart"],
}

#: A chain whose every link is the same length: no `data`, so the struct's
#: frame is fixed and it has no extent function. C's located accessor asked
#: for one and `situc build` crashed with a `ValueError`.
FIXED = SENTINEL.replace("\tu8    data[head.size];\n", "")

SAME: dict[str, tuple[bytes, str, int]] = {
	"one":       (_sections((0, 0, END)), "ok", 0),
	"three":     (_sections((0, 0, 16), (16, 0, 8), (8, 0, END)), "ok", 2),
	"three_then_out": (_sections((0, 0, 16), (16, 0, 8), (8, 0, 200)),
	                   "bounds", 2),
	"loops_to_itself": (_sections((0, 0, 0)), "constraint", 10),
	"nine":      ENDED["nine"],
	"ten":       ENDED["ten"],
}

#: FS2 as it is: a format that sets no maximum section count, so the schema
#: states only how far this build follows it (26.1047). The verdicts are the
#: sentinel chain's but for depth: past `[limit]` the image is refused as
#: more than this reader spends, never as malformed.
LIMITED = SENTINEL.replace("[depth = 8]", "[limit = 8]")

UNBOUNDED: dict[str, tuple[bytes, str, int]] = {
	name: (data, "depth" if verdict == "constraint" else verdict, nesting)
	for name, (data, verdict, nesting) in ENDED.items()}

#: FS2's link sits inside the section's `authenticated body`: plaintext,
#: so an `at` reads it as a discriminant would (26.1046), and the CRC is the
#: caller's to check, since `validate` computes no checksum (0051). A
#: corrupt section's link is followed like any other.
AUTHENTICATED = ("target buffer;\nendian big;\n"
         "codec c8 {\n\tkernel = polynomial(width = 8, poly = 0x07);\n}\n"
         "impl c8 derived;\n"
         "struct hdr { u8 next; u8 len; }\n"
         "struct section {\n\tauthenticated body { hdr h; }\n"
         "\tchecksum u8 crc[1] covers(body) is c8;\n}\n"
         "struct chain [limit = 8] {\n"
         "\tsection one;\n"
         "\tchain   following at one.h.next;\n"
         "}\n")


def _crc8(data: bytes) -> int:
	crc = 0
	for byte in data:
		crc ^= byte
		for _ in range(8):
			crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
	return crc


def _links(*pairs: tuple[int, int, bool]) -> bytes:
	"""Three-byte sections at the given offsets: (at, next, crc good)."""
	end = max(at + 3 for at, _, _ in pairs)
	out = bytearray(end)
	for at, nxt, good in pairs:
		out[at:at + 2] = bytes([nxt, 1])
		out[at + 2] = _crc8(bytes([nxt, 1])) ^ (0 if good else 0x5A)
	return bytes(out)


LINKED: dict[str, tuple[bytes, str, int]] = {
	"three_then_out": (_links((0, 3, True), (3, 6, True), (6, 200, True)),
	                   "bounds", 2),
	"first_corrupt":  (_links((0, 3, False), (3, 6, True), (6, 200, True)),
	                   "bounds", 2),
	"second_corrupt": (_links((0, 3, True), (3, 6, False), (6, 200, True)),
	                   "bounds", 2),
	"loops":          (_links((0, 0, True)), "depth", 10),
}

SHAPES = [(SCHEMA, CASES), (SENTINEL, ENDED), (FORWARD, GONE), (FIXED, SAME),
          (LIMITED, UNBOUNDED), (AUTHENTICATED, LINKED)]
SHAPE_IDS = ("located", "sentinel", "forward", "fixed", "limited", "authenticated")

#: Shapes whose struct is one fixed length, so a view of one takes none.
FIXED_SIZE = (FIXED, AUTHENTICATED)


def _build(tmp_path: Path, target: str, text: str = SCHEMA) -> Path:
	schema = tmp_path / "chain.situ"
	schema.write_text(text, encoding="ascii")
	out = tmp_path / target
	assert main(["build", str(schema), "--target", target,
	             "--out", str(out)]) == 0
	# A derived checksum's C (`chain_derived.c`), which C links as source
	# and C++ as an object built against the C header beside it.
	if "codec" in text and target == "c":
		assert main(["gen-derived", str(schema), "--target", "c",
		             "--out", str(out)]) == 0
	if "codec" in text and target == "cpp":
		side = tmp_path / "c-side"
		assert main(["build", str(schema), "--target", "c",
		             "--out", str(side)]) == 0
		assert main(["gen-derived", str(schema), "--target", "c",
		             "--out", str(side)]) == 0
		made = subprocess.run(
			["gcc", "-std=c99", "-Os", f"-I{RUNTIME / 'c'}", f"-I{side}", "-c",
			 str(side / "chain_derived.c"), "-o", str(out / "chain_derived.o")],
			capture_output=True, text=True)
		assert made.returncode == 0, made.stderr
	return out


def _derived(out: Path) -> list[str]:
	return [str(path) for path in sorted([*out.glob("*_derived.c"),
	                                      *out.glob("*_derived.o")])]


def _table(ran: "subprocess.CompletedProcess[str]") -> dict[str, tuple[str, int]]:
	assert ran.returncode == 0, ran.stdout + ran.stderr
	rows = {}
	for line in ran.stdout.splitlines():
		name, verdict, nesting = line.split()
		rows[name] = (verdict, int(nesting))
	return rows


def _want(cases: dict[str, tuple[bytes, str, int]]
		) -> dict[str, tuple[str, int]]:
	return {name: (verdict, nesting)
	        for name, (_, verdict, nesting) in cases.items()}


def _c_bytes(data: bytes) -> str:
	return "{" + ", ".join(f"0x{b:02x}" for b in data) + "}"


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
@pytest.mark.parametrize("text,cases", SHAPES, ids=list(SHAPE_IDS))
def test_c(tmp_path: Path, text: str,
		cases: dict[str, tuple[bytes, str, int]]) -> None:
	out = _build(tmp_path, "c", text)
	# A fixed struct's view takes no length: the struct says it.
	length = "" if text in FIXED_SIZE else "cases[i].n, "
	rows = "\n".join(
		f'\t{{ "{name}", (const uint8_t[]){_c_bytes(data)}, {len(data)}u }},'
		for name, (data, _, _) in cases.items())
	(out / "main.c").write_text(f'''#include <stdio.h>
#include <string.h>
#include "chain.h"
static const struct {{ const char *name; const uint8_t *data; uint32_t n; }} cases[] = {{
{rows}
}};
static const char *word(situ_err_t e)
{{
	return e == SITU_OK ? "ok" : e == SITU_ERR_BOUNDS ? "bounds"
	     : e == SITU_ERR_CONSTRAINT ? "constraint" : e == SITU_ERR_DEPTH ? "depth"
	     : e == SITU_ERR_CHECKSUM ? "checksum" : "other";
}}
int main(void)
{{
	unsigned i;
	for (i = 0; i < sizeof cases / sizeof cases[0]; i++) {{
		uint8_t     buf[128];
		situ_msg_t  msg;
		situ_view_t view;

		memcpy(buf, cases[i].data, cases[i].n);
		situ_msg_init(&msg, buf, cases[i].n);
		if (situ_chain_view(&msg, 0u, {length}&view) != SITU_OK) {{
			return 1;
		}}
		printf("%s %s %u\\n", cases[i].name, word(situ_chain_validate(view)),
		       situ_chain_nesting(view));
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "chain.c"), str(RUNTIME / "c" / "situ.c"),
		 *_derived(out), "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want(cases)


@pytest.mark.skipif(shutil.which("g++") is None, reason="no C++ compiler")
@pytest.mark.parametrize("text,cases", SHAPES, ids=list(SHAPE_IDS))
def test_cpp(tmp_path: Path, text: str,
		cases: dict[str, tuple[bytes, str, int]]) -> None:
	out = _build(tmp_path, "cpp", text)
	rows = "\n".join(
		f'\t{{ "{name}", {{{", ".join(f"0x{b:02x}" for b in data)}}},'
		f' {len(data)}u }},'
		for name, (data, _, _) in cases.items())
	(out / "main.cpp").write_text(f'''#include <cstdio>
#include <cstring>
#include "chain.hpp"
struct one {{ const char *name; std::uint8_t data[128]; std::uint32_t n; }};
static const one cases[] = {{
{rows}
}};
static const char *word(::situ::rt::err e)
{{
	return e == ::situ::rt::err::ok ? "ok" : e == ::situ::rt::err::bounds ? "bounds"
	     : e == ::situ::rt::err::constraint ? "constraint"
	     : e == ::situ::rt::err::depth ? "depth"
	     : e == ::situ::rt::err::checksum ? "checksum" : "other";
}}
int main()
{{
	for (const one &c : cases) {{
		std::uint8_t buf[128];
		std::memcpy(buf, c.data, c.n);
		situ_msg_t msg;
		situ_msg_init(&msg, buf, c.n);
		situ_view_t view;
		situ_view_at(&msg, 0u, c.n, &view);
		const ::situ::chain top(view);
		std::printf("%s %s %u\\n", c.name, word(top.validate()), top.nesting());
	}}
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["g++", "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{out}",
		 str(out / "main.cpp"), str(RUNTIME / "c" / "situ.c"),
		 *_derived(out), "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want(cases)


@pytest.mark.skipif(shutil.which("rustc") is None, reason="no Rust compiler")
@pytest.mark.parametrize("text,cases", SHAPES, ids=list(SHAPE_IDS))
def test_rust(tmp_path: Path, text: str,
		cases: dict[str, tuple[bytes, str, int]]) -> None:
	out = _build(tmp_path, "rust", text)
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", out / "situ_rt.rs")
	rows = "\n".join(
		f'\t\t("{name}", vec![{", ".join(str(b) for b in data)}]),'
		for name, (data, _, _) in cases.items())
	(out / "main.rs").write_text(f'''mod situ_rt;
mod chain;
fn main() {{
	let cases: Vec<(&str, Vec<u8>)> = vec![
{rows}
	];
	for (name, d) in cases {{
		let top = chain::Chain::new(&d).unwrap();
		let word = match top.validate() {{
			Ok(()) => "ok",
			Err(situ_rt::Error::Bounds) => "bounds",
			Err(situ_rt::Error::Constraint) => "constraint",
			Err(situ_rt::Error::Depth) => "depth",
			Err(situ_rt::Error::Checksum) => "checksum",
			Err(_) => "other",
		}};
		println!("{{}} {{}} {{}}", name, word, top.nesting());
	}}
}}
''', encoding="ascii")
	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-o", str(out / "r"),
		 str(out / "main.rs")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert _table(subprocess.run([str(out / "r")], capture_output=True,
	                             text=True)) == _want(cases)


@pytest.mark.parametrize("text,cases", SHAPES, ids=list(SHAPE_IDS))
def test_python(tmp_path: Path, text: str,
		cases: dict[str, tuple[bytes, str, int]]) -> None:
	out = _build(tmp_path, "python", text)
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	listed = ", ".join(f"({name!r}, {data!r})"
	                   for name, (data, _, _) in cases.items())
	length = "" if text in FIXED_SIZE else ", len(data)"
	(out / "main.py").write_text(f'''import situ_runtime as rt
import chain
for name, data in [{listed}]:
	top = chain.chain.at(rt.Message(bytearray(data)), 0{length})
	try:
		top.validate()
		word = "ok"
	except rt.BoundsError:
		word = "bounds"
	except rt.ConstraintError:
		word = "constraint"
	except rt.DepthError:
		word = "depth"
	except rt.ChecksumError:
		word = "checksum"
	print(name, word, top.nesting)
''', encoding="ascii")
	assert _table(subprocess.run([sys.executable, str(out / "main.py")],
	                             capture_output=True, text=True,
	                             cwd=out)) == _want(cases)


@pytest.mark.parametrize("text", [SCHEMA, SENTINEL, FORWARD, FIXED, LIMITED,
                                  AUTHENTICATED],
                         ids=list(SHAPE_IDS))
def test_python_type_checks(tmp_path: Path, text: str) -> None:
	"""The probe's `try`/`else` is shaped for mypy: binding the element to
	`None` on a miss would retype a variable `mypy` reads as the struct."""
	if subprocess.run([sys.executable, "-m", "mypy", "--version"],
	                  capture_output=True).returncode != 0:
		pytest.skip("no mypy")
	out = _build(tmp_path, "python", text)
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	checked = subprocess.run(
		[sys.executable, "-m", "mypy", "--strict", "--no-pretty",
		 "--no-error-summary", str(out / "chain.py")],
		capture_output=True, text=True)
	assert checked.returncode == 0, checked.stdout + checked.stderr


#: What the two walkers answer, which is the backends' verdict but for a
#: message past the depth: there they say they cannot say rather than give
#: one, as for every recursion (26.286). `nine` is OK to the backends and
#: past the walkers' ceiling only because `hdr`, nested in each section,
#: counts as a level there -- an open walker gap, not this construct's.
WALKED = {
	"located": {"loops_to_itself": "cannot-say", "three_then_out": "1",
	            "six_then_out": "1", "twelve": "cannot-say",
	            "loops_after_three": "cannot-say"},
	"sentinel": {"one": "0", "three": "0", "three_then_out": "1",
	             "loops_to_itself": "cannot-say", "nine": "cannot-say",
	             "ten": "cannot-say", "apart": "0"},
	"forward": {"forward": "0", "backward": "2",
	            "loops_to_itself": "cannot-say", "apart": "0"},
	"fixed": {"one": "0", "three": "0", "three_then_out": "1",
	          "loops_to_itself": "cannot-say", "nine": "cannot-say",
	          "ten": "cannot-say"},
}
WALKED["limited"] = WALKED["sentinel"]
#: A link the walkers follow as the backends do; a chain looping back is
#: past the walkers' depth, which they cannot say about (26.1043).
WALKED["authenticated"] = {"three_then_out": "1", "first_corrupt": "1",
                           "second_corrupt": "1", "loops": "cannot-say"}


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
@pytest.mark.parametrize("text,cases", SHAPES, ids=list(SHAPE_IDS))
def test_both_walkers(tmp_path: Path, text: str,
		cases: dict[str, tuple[bytes, str, int]]) -> None:
	"""The walkers place a located struct from the message too (26.1043):
	bounded by the parent's frame, every link that pointed outside its own
	section was BOUNDS, and the C walker measured the offset from the
	nested struct rather than from the message."""
	from test_walker_c import (VERDICT, _build_driver, c_verdict,
	                           image_for, python_verdict, shape_named)

	schema = tmp_path / "chain.situ"
	schema.write_text(text, encoding="ascii")
	image = image_for(schema)
	shape = shape_named(schema, "chain")
	(tmp_path / "img").write_bytes(image)
	drive = _build_driver(tmp_path, image, VERDICT)
	want  = WALKED[SHAPE_IDS[SHAPES.index((text, cases))]]
	said  = {name: (python_verdict(image, data, shape),
	                c_verdict(tmp_path, image, data, shape, drive))
	         for name, (data, _, _) in cases.items()}
	assert said == {name: (word, word) for name, word in want.items()}


@pytest.mark.parametrize("text", [SCHEMA, SENTINEL, FORWARD],
                         ids=list(SHAPE_IDS[:3]))
def test_the_dissector_steps_over_the_frame_alone(tmp_path: Path,
		text: str) -> None:
	"""The sixth reader. The next section is not this one's bytes, so the
	dissector steps over head and data and no further: a located arm was
	read as nine bytes of the frame where the four measure it as none."""
	from test_dissector import LUA, read_back
	from situc.cli import analyse
	from situc.dissector import generate
	from situc.parser import parse

	if LUA is None:
		pytest.skip("no Lua interpreter")
	schema = tmp_path / "chain.situ"
	schema.write_text(text, encoding="ascii")
	source, resolved, _ = analyse(schema)
	lua = tmp_path / "chain.lua"
	lua.write_text(generate(parse(source), resolved, "chain"),
	               encoding="ascii")
	message = _sections((0, 2, 10), (10, 1, 19), (19, 0, END))
	used, rows = read_back(lua, "chain", message)
	assert used == 10, rows
	assert [name for name, *_ in rows] == ["hdr.size", "hdr.next",
	                                       "chain.data"], rows


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
@pytest.mark.parametrize("text", [SCHEMA, SENTINEL, FORWARD],
                         ids=list(SHAPE_IDS[:3]))
def test_c_frames_a_section_by_its_own_bytes(tmp_path: Path,
		text: str) -> None:
	"""`required` asks for this section's bytes and no more: the next one
	is wherever `next` says, and a stream framer that waited for it would
	wait for a section that may sit anywhere -- or, here, for the 1000
	bytes a header in the gap claims. Both shapes, since `required` walked
	the located member and the located arm alike."""
	out = _build(tmp_path, "c", text)
	message = _sections((0, 2, 30), (10, 1000, 0), (30, 0, END))[:38]
	(out / "main.c").write_text(f'''#include <stdio.h>
#include "chain.h"
static const uint8_t message[] = {_c_bytes(message)};
int main(void)
{{
	uint32_t need = 0u;
	const situ_err_t err = situ_chain_required(message, sizeof message, &need);
	printf("%d %u\\n", (int)err, need);
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "chain.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.split() == ["0", "10"], ran.stdout


#: A located arm beside a variable one, so the variant's size is computed
#: per message and the arm placed by `at` has to count as nothing in it.
#: FS2's shape never reaches that sum: `nothing` and the located arm are both
#: zero bytes in the frame, so the layout makes the variant a fixed zero.
MIXED = ("target buffer;\nendian big;\n"
         "struct t { u8 a; u8 b; }\n"
         "struct s {\n"
         "\tu8 k;\n"
         "\tu8 n;\n"
         "\tvariant v switch (k) {\n"
         "\t\tcase 0:  u8 run[n];\n"
         "\t\tdefault: t far at n;\n"
         "\t}\n"
         "\tu8 tail;\n"
         "}\n")


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c_places_what_follows_a_located_arm(tmp_path: Path) -> None:
	"""`tail` follows the variant, which holds no bytes when its located arm
	is selected: at offset 2, not 2 plus the two bytes `far` occupies at 4."""
	out = _build(tmp_path, "c", MIXED)
	(out / "main.c").write_text('''#include <stdio.h>
#include "chain.h"
int main(void)
{
	uint8_t     buf[] = {1, 4, 0x7e, 0, 0xaa, 0xbb};
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    need = 0u;

	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_s_view(&msg, 0u, sizeof buf, &view) != SITU_OK) {
		return 1;
	}
	(void)situ_s_required(buf, sizeof buf, &need);
	printf("%u %u %d\\n", situ_s_tail_get(view), need, (int)situ_s_validate(view));
	return 0;
}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "chain.c"), str(RUNTIME / "c" / "situ.c"),
		 "-o", str(out / "r")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	assert ran.stdout.split() == ["126", "3", "0"], ran.stdout


def test_python_places_what_follows_a_located_arm(tmp_path: Path) -> None:
	"""The C case above, in Python."""
	out = _build(tmp_path, "python", MIXED)
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            out / "situ_runtime.py")
	(out / "main.py").write_text('''import situ_runtime as rt
import chain
buf = bytearray([1, 4, 0x7e, 0, 0xaa, 0xbb])
one = chain.s.at(rt.Message(buf), 0, len(buf))
one.validate()
print(one.tail, chain.s.required(bytes(buf)))
''', encoding="ascii")
	ran = subprocess.run([sys.executable, str(out / "main.py")],
	                     capture_output=True, text=True, cwd=out)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split()[0] == "126", ran.stdout
