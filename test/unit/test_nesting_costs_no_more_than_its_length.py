"""What a message costs to read is a polynomial in its length (26.909).

`[depth]` bounds the stack and it never bounded the work. Every level of a
recursive struct measured its child more than once -- an extent summing terms
that each re-derived their own offset, a `while` condition whose getter
re-measured the element it had just measured -- so a few dozen bytes of `[`
took a validator longer than anybody would wait.

Counted rather than timed, because a count does not move at load 10. Each
backend runs the generated code under a counter of function entries that stops
the program past a budget, so the old code fails here in a fraction of a
second instead of hanging, and what is asserted is the count at n and at 2n.

The question each driver asks is `differ`'s, which is every question there is:
every member of every struct, `validate`, and `required` on each prefix.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import fourway
from every_schema import ROOT
from situc.codegen import differ
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.diagnostics import Source
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse, parse_text
from situc.resolve import resolve
from test_walker_c import (DRIVER, OFFSETS, SHOW, VERDICT, _build_driver,
                           c_verdict, python_verdict, shape_named_text)
from walker.walk import Refused

RUNTIME  = ROOT / "runtime"
HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: Past this many function entries a driver stops itself and says so.
BUDGET = 400_000_000

#: The same under Python's profiler, where a call costs a hundred times what
#: it does in C and the old code has to fail in minutes rather than hours.
SLOW_BUDGET = BUDGET // 8

HEAD = "target buffer;\nendian big;\n"

#: Deep enough that the levels asked about are all allowed: the bound is what
#: the test holds still, so the only thing growing is the message.
DEEP = 2000


def example(name: str) -> str:
	"""A corpus schema with room for the levels asked about."""
	text = (ROOT / "example" / name / f"{name}.situ").read_text(encoding="ascii")
	return text.replace("[depth = 64]", f"[depth = {DEEP}]")


SHAPES: dict[str, tuple[str, object]] = {
	# The defect as reported, and the same nesting well formed.
	"json_cut": (example("json"), lambda n: b"[" * n + b'"\xff]'),
	"json_whole": (example("json"), lambda n: b"[" * n + b"1" + b"]" * n),
	"json_object": (example("json"),
	                lambda n: b'{"a":' * n + b"1" + b"}" * n),
	"sexpr": (example("sexpr"), lambda n: b"(" * n + b"a" + b")" * n),

	# A run counted by a field in front of it, and a field behind it.
	"counted": (HEAD + f"""
struct node [depth = {DEEP}] {{ u8 n; node kids[n]; u8 after; }}
""", lambda n: b"\x01" * n + b"\x00" + b"\x07" * n),

	# A run ended by a delimiter, with a member behind it.
	"delimited": (HEAD + f"""
struct dnode [depth = {DEEP}] {{ u8 d; dnode kids[] until ";"; u8 after; }}
""", lambda n: b"d" * n + b";" + b";a" * (n - 1) + b";a"),

	# The condition field behind two variable members, one of them the
	# recursion, and in the second shape with another variable member after
	# it, so no arithmetic from the end of the element finds it.
	"member": (HEAD + f"""
struct tree [depth = {DEEP}] {{
	peek u8 kind;
	variant body switch (kind) {{
		case '[': list as_list;
		default:  atom as_atom;
	}}
}}
struct atom {{ u8 c; }}
struct list [depth = {DEEP}] {{ u8 open; kv entries[] while (sep == ','); }}
struct kv [depth = {DEEP}] {{
	u8 key[] until ":";
	tree held;
	u8 sep;
}}
""", lambda n: b"[k:" * n + b"1" + b"]" * n),
	"between": (HEAD + f"""
struct tree [depth = {DEEP}] {{
	peek u8 kind;
	variant body switch (kind) {{
		case '[': list as_list;
		default:  atom as_atom;
	}}
}}
struct atom {{ u8 c; }}
struct list [depth = {DEEP}] {{ u8 open; kv entries[] while (sep == ','); }}
struct kv [depth = {DEEP}] {{
	u8 key[] until ":";
	tree held;
	u8 sep;
	u8 note[] until ";";
}}
""", lambda n: b"[k:" * n + b"1" + b"];" * n),

	# Recursion through an arm, with members behind the variant.
	"arm": (HEAD + f"""
struct nothing {{ }}
struct t [depth = {DEEP}] {{
	u8 k;
	variant b switch (k) {{
		case 1:  t one;
		default: nothing none;
	}}
	u8 note[] until ";";
	u8 sep;
}}
""", lambda n: b"\x01" * n + b"\x00" + b";s" * (n + 1)),

	# Arguments, changing at every level.
	"arguments": (HEAD + f"""
struct nothing {{ }}
struct nonce {{ parameter u8 n [stream]; u8 data[n]; }}
struct p [depth = {DEEP}] {{
	parameter u8 kind [stream];
	u8 next;
	nonce(n = kind) body;
	variant rest switch (next) {{
		case 0:  nothing none;
		default: p(kind = next) more;
	}}
	u8 tail;
}}
struct top {{ u8 first; p(kind = first) one; }}
""", lambda n: b"\x01" + b"\x01a" * n + b"\x00a" + b"t" * (n + 1)),

	# A `while` run whose element takes an argument.
	"handed": (HEAD + f"""
struct w [depth = {DEEP}] {{
	parameter u8 b [stream];
	u8 data[b];
	u8 more;
	wl(b = more) sub;
	u8 go;
}}
struct wl [depth = {DEEP}] {{
	parameter u8 b [stream];
	u8 n;
	w(b = n) items[] while (go != 0) max 4;
}}
struct wtop {{ u8 b; wl(b = b) all; }}
""", lambda n: b"\x00" * 0 + b"\x00\x00" * n + b"\x00" * (n + 1)),

	# Fields read behind the recursion: a length, a discriminant, a count.
	"length_behind": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 n;
	u8 tail[n];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01t" * (n + 1)),
	"switch_behind": (HEAD + f"""
struct nothing {{ }}
struct one {{ u8 a; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 tag;
	variant d switch (tag) {{
		case 1:  one a;
		default: nothing absent;
	}}
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01a" * (n + 1)),
	"count_behind": (HEAD + f"""
struct nothing {{ }}
struct one {{ u8 l; u8 d[l]; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 n;
	one items[n];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01\x00" * (n + 1)),

	# A lead in front of the recursion.
	"lead": (HEAD + f"""
whitespace ' ';
struct nothing {{ }}
struct e [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  w inner;
		default: nothing none;
	}}
	u8 sep skip;
}}
struct w [depth = {DEEP}] {{ u8 o; e held skip; u8 c skip; }}
""", lambda n: b"\x01o" * n + b"\x00" + b"sc" * n + b"s"),

	# What an arm holds, behind the recursion.
	"arm_behind": (HEAD + f"""
struct nothing {{ }}
struct name {{ u8 l; u8 d[l]; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 tag;
	variant d switch (tag) {{
		case 1:  name named;
		case 2:  u8 scanned[] until ";";
		default: nothing absent;
	}}
	u8 after;
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01\x00a\x02;a" * (n + 1)),

	# A variable member of a type outside the cycle, behind it, whose argument
	# is read behind it as well.
	"nested_behind": (HEAD + f"""
struct nothing {{ }}
struct name {{ parameter u8 w [stream]; u8 d[w]; u8 l; u8 e[l]; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 w;
	name(w = w) tail;
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01a\x00" * (n + 1)),

	# The rest of the frame, behind the recursion.
	"remaining_behind": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 rest[remaining];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"r" * n),
	"run_remaining_behind": (HEAD + f"""
struct nothing {{ }}
struct one {{ u8 l; u8 d[l]; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	one items[remaining];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x00" * n),

	# A varint and a text number, whose reads are not a plain load.
	"varint_behind": (HEAD + f"""
varint_type v {{ encoding = leb128; max_bits = 14; max_bytes = 2; }}
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	v n;
	u8 tail[n];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01t" * (n + 1)),
	"digits_behind": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	decimal u8 n until ";" max 3;
	u8 tail[n];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"1;t" * (n + 1)),
	"digits_fixed": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	decimal u8 n[1];
	u8 tail[n];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"1t" * (n + 1)),

	# A field of a nested struct, read behind the recursion.
	"dotted_behind": (HEAD + f"""
struct nothing {{ }}
struct hdr {{ u8 l; u8 m; }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	hdr h;
	u8 tail[h.l];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01mt" * (n + 1)),

	# A condition over a varint behind the recursion. C++ has never built one.
	"varint_while": (HEAD + f"""
varint_type v {{ encoding = leb128; max_bits = 14; max_bytes = 2; }}
struct nothing {{ }}
struct e [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  r inner;
		default: nothing none;
	}}
	v g;
}}
struct r [depth = {DEEP}] {{ u8 o; e items[] while (g == 1); }}
""", lambda n: b"o" + b"\x01o" * n + b"\x00\x00" + b"\x01\x00\x00" * n),

	# A condition over an enum, which the accessor answers in its own type.
	# C and Python only: behind a variable member the other two have never
	# built one, the framing reading the field where only a view can.
	"enum_while": (HEAD + f"""
enum go : u8 {{ stop = 0, more = 1 }}
struct nothing {{ }}
struct e [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  r inner;
		default: nothing none;
	}}
	go g;
}}
struct r [depth = {DEEP}] {{ u8 o; e items[] while (g == 1); }}
""", lambda n: b"\x01o" * n + b"\x00" + b"\x00" * (n + 1)),

	# A total taken over a run behind the recursion.
	"sum_behind": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 n;
	u8 lens[n];
	u8 tail[sum(lens)];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x01\x01t" * (n + 1)),

	# Padding behind the recursion.
	"pad_behind": (HEAD + f"""
struct nothing {{ }}
struct x [depth = {DEEP}] {{
	u8 k;
	variant c switch (k) {{
		case 1:  x inner;
		default: nothing none;
	}}
	u8 l;
	pad_to(4);
	u8 tail[l];
}}
""", lambda n: b"\x01" * n + b"\x00" + b"\x00\x00\x00\x00" * (n + 1)),

	# Recursion through the bytes that hold it.
	"holds": (HEAD + f"""
struct b [depth = {DEEP}] {{ u8 n; u8 body[n] [holds = b]; }}
""", lambda n: bytes(range(min(n, 200), 0, -1)) + b"\x00"),

	# Two recursive members side by side: a tree, so the work is the bytes.
	"two_arms": (HEAD + f"""
struct nothing {{ }}
struct b [depth = {DEEP}] {{
	u8 l;
	variant left switch (l) {{
		case 1:  b kid;
		default: nothing none;
	}}
	u8 r;
	variant right switch (r) {{
		case 1:  b other;
		default: nothing absent;
	}}
}}
""", lambda n: b"\x01" * n + b"\x00\x00" + b"\x00" * n),
}


#: Which struct each shape is entered at, where it is not `x`.
TOPS = {"json_cut": "value", "json_whole": "value", "json_object": "value",
        "sexpr": "sexpr", "counted": "node", "delimited": "dnode",
        "member": "tree", "between": "tree", "arm": "t", "arguments": "top",
        "handed": "wtop", "lead": "e", "enum_while": "r", "varint_while": "r",
        "two_arms": "b", "holds": "b"}


def top_of(shape: str) -> str:
	return TOPS.get(shape, "x")


#: The counter, linked into a C or C++ driver built with
#: `-finstrument-functions`. Printed from a destructor, so a driver that ran to
#: its end reports the same way as one that stopped itself.
HOOK = """#include <stdio.h>
#include <unistd.h>
#define QUIET __attribute__((no_instrument_function))
#ifdef __cplusplus
extern "C" {
#endif
static unsigned long long ticks;
QUIET void __cyg_profile_func_enter(void *function, void *caller)
{
	(void)function; (void)caller;
	if (++ticks > BUDGETull) {
		fprintf(stderr, "ticks over\\n");
		_exit(77);
	}
}
QUIET void __cyg_profile_func_exit(void *function, void *caller)
{
	(void)function; (void)caller;
}
QUIET __attribute__((destructor)) static void report(void)
{
	fprintf(stderr, "ticks %llu\\n", ticks);
}
#ifdef __cplusplus
}
#endif
""".replace("BUDGET", str(BUDGET))

#: The same counter for Rust, which has no stable instrumentation of its own,
#: so a call to it goes at the head of every function the module defines.
RUST_TICK = """
static TICKS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
pub fn tick() {
	let held = TICKS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
	if held > BUDGET {
		eprintln!("ticks over");
		std::process::exit(77);
	}
}
pub fn ticks() -> u64 { TICKS.load(std::sync::atomic::Ordering::Relaxed) }
""".replace("BUDGET", str(BUDGET))

#: And for Python, which has one.
PYTHON_TICK = """import runpy, sys, os
ticks = 0
def count(frame, event, arg):
	global ticks
	if event == "call":
		ticks += 1
		if ticks > BUDGET:
			sys.stderr.write("ticks over\\n")
			sys.stderr.flush()
			os._exit(77)
sys.setrecursionlimit(1000000)
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(script))
sys.setprofile(count)
runpy.run_path(script)
sys.setprofile(None)
sys.stderr.write(f"ticks {ticks}\\n")
""".replace("BUDGET", str(SLOW_BUDGET))


def ticked_rust(module: str) -> str:
	"""`module` with the counter at the head of every function it can reach."""
	opens = re.compile(r"^([ \t]*)(pub(\(crate\))? )?fn [^;\n]*\{[ \t]*$", re.M)
	return opens.sub(lambda found: f"{found.group(0)}\n"
	                 f"{found.group(1)}\tcrate::tick();", module)


def lean_driver(schema: object, resolved: object, target: str,
		top: str) -> str:
	"""`differ`'s driver asking one struct `validate` and `required`, once.

	Every other struct, every member and every prefix is a factor on top of
	the validator, which at sixty levels is minutes under a counter. What those
	cost is measured at a shallower depth by the test that asks them.
	"""
	held = differ.structs_of(resolved)  # type: ignore[arg-type]
	with mock.patch.object(differ, "asks", lambda *_: []), \
			mock.patch.object(differ, "FRAMING_PREFIXES", 0), \
			mock.patch.object(differ, "structs_of", lambda *_: [
				one for one in held if one.name == top]):
		return differ.generate(schema, resolved, target)  # type: ignore[arg-type]


def build(tmp: Path, text: str, target: str, top: str | None = None
		) -> list[str]:
	"""One driver under a counter: `differ`'s every question, or with `top`
	the two that matter for that struct."""
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	driver   = (differ.generate(schema, resolved, target) if top is None
	            else lean_driver(schema, resolved, target, top))

	if target == "c":
		for name, body in generate_c(schema, resolved, "unit").files().items():
			(tmp / name).write_text(body, encoding="ascii")
		(tmp / "driver.c").write_text(driver, encoding="ascii")
		(tmp / "hook.c").write_text(HOOK, encoding="ascii")
		subprocess.run([HOST_CC or "cc", "-std=gnu11", "-O0", "-c", "hook.c"],
		               cwd=tmp, check=True)
		subprocess.run(
			[HOST_CC or "cc", "-std=c11", "-O0", "-finstrument-functions", "-no-pie",
			 f"-I{RUNTIME / 'c'}", "-I.", "driver.c", "unit.c",
			 str(RUNTIME / "c" / "situ.c"), "hook.o", "-o", "probe"],
			cwd=tmp, check=True, capture_output=True, text=True)
		return [str(tmp / "probe")]

	if target == "cpp":
		(tmp / "unit.hpp").write_text(
			generate_cpp(schema, resolved, "unit").header, encoding="ascii")
		(tmp / "driver.cpp").write_text(driver, encoding="ascii")
		(tmp / "hook.c").write_text(HOOK, encoding="ascii")
		subprocess.run([HOST_CC or "cc", "-std=gnu11", "-O0", "-c", "hook.c"],
		               cwd=tmp, check=True)
		subprocess.run(
			[HOST_CC or "cc", "-std=c11", "-O0", "-c",
			 str(RUNTIME / "c" / "situ.c"), "-o", "situ.o"],
			cwd=tmp, check=True)
		subprocess.run(
			[HOST_CXX or "g++", "-std=c++17", "-O0", "-finstrument-functions", "-no-pie",
			 f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.",
			 "driver.cpp", "situ.o", "hook.o", "-o", "probe"],
			cwd=tmp, check=True, capture_output=True, text=True)
		return [str(tmp / "probe")]

	if target == "rust":
		(tmp / "situ_rt.rs").write_text(
			(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
			.replace("#![no_std]\n", ""), encoding="ascii")
		(tmp / "unit.rs").write_text(
			ticked_rust(generate_rs(schema, resolved, "unit").module),
			encoding="ascii")
		(tmp / "main.rs").write_text(
			driver.replace("fn main() {", RUST_TICK + "fn main() {", 1)
			.rstrip().removesuffix("}")
			+ '\teprintln!("ticks {}", ticks());\n}\n', encoding="ascii")
		assert RUSTC is not None
		subprocess.run([RUSTC, "--edition", "2021", "-A", "warnings",
		                "main.rs", "-o", "probe"],
		               cwd=tmp, check=True, capture_output=True, text=True)
		return [str(tmp / "probe")]

	(tmp / "situ_runtime.py").write_text(
		(RUNTIME / "python" / "situ_runtime.py").read_text(encoding="ascii"),
		encoding="ascii")
	(tmp / "unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")
	(tmp / "driver.py").write_text(driver, encoding="ascii")
	(tmp / "count.py").write_text(PYTHON_TICK, encoding="ascii")
	return [sys.executable, str(tmp / "count.py"), str(tmp / "driver.py")]


def cost(command: list[str], message: bytes, tmp: Path) -> tuple[int | None, str]:
	"""Function entries one driver takes over `message`, and what it said.

	None where it went past the budget, which is the answer the old code
	gives. A crash is not an answer of any kind.
	"""
	# The driver's standard output goes to a file so that a long answer
	# cannot block on a pipe nobody is reading until the end.
	with (tmp / "answer").open("wb") as held:
		ran = subprocess.run([*command, message.hex()], stdout=held,
		                     stderr=subprocess.PIPE, text=True, cwd=tmp,
		                     timeout=1200)
	said = (tmp / "answer").read_text(encoding="latin-1")
	if ran.returncode == 77:
		return None, said
	assert ran.returncode == 0, (ran.returncode, ran.stderr[-2000:])
	found = re.findall(r"^ticks (\d+)$", ran.stderr, re.M)
	assert found, ran.stderr[-2000:]
	return int(found[-1]), said


#: The walker's own ceiling, raised past anything asked: what is being measured
#: is the work a level costs, and the default stops at eight levels of it.
CEILING = 100000

#: What the Python walker is asked, under the same counter. It takes the
#: top-level struct and answers what `validate` and every member's offset say.
PYTHON_WALKER = """import sys, os
sys.path[:0] = [ROOT]
sys.setrecursionlimit(1000000)
from walker import report, walk
from walker.image import load
walk.WALK_DEPTH_MAX = CEILING
blob  = open(sys.argv[1], "rb").read()
shape = int(sys.argv[2])
data  = bytes.fromhex(sys.argv[3])
image = load(blob)
ticks = 0
def count(frame, event, arg):
	global ticks
	if event == "call":
		ticks += 1
		if ticks > BUDGET:
			sys.stderr.write("ticks over\\n")
			sys.stderr.flush()
			os._exit(77)
sys.setprofile(count)
try:
	view = walk.acquire(image, data, shape)
	print(report._validate(image, view, shape))
	for index in image.members(image.structs[shape]):
		try:
			print(walk.offset_bits(view, index))
		except walk.Refused:
			print("refused")
except walk.Refused:
	print("refused")
sys.setprofile(None)
sys.stderr.write(f"ticks {ticks}\\n")
""".replace("CEILING", str(CEILING)).replace("BUDGET", str(SLOW_BUDGET)) \
	.replace("ROOT", repr(str(ROOT)))


def build_walker(tmp: Path, text: str, top: str, target: str) -> list[str]:
	"""One walker's driver over the image of `text`, asking about `top`."""
	blob  = pack_text(text)
	shape = shape_named_text(text, top)
	(tmp / "img").write_bytes(blob)

	if target == "python":
		(tmp / "count.py").write_text(PYTHON_WALKER, encoding="ascii")
		return [sys.executable, str(tmp / "count.py"), str(tmp / "img"),
		        str(shape)]

	(tmp / "drive.c").write_text(
		DRIVER.replace("SHOW", SHOW).replace("ASK", f"{{ {OFFSETS} }}\n{VERDICT}"),
		encoding="ascii")
	(tmp / "hook.c").write_text(HOOK, encoding="ascii")
	subprocess.run([HOST_CC or "cc", "-std=gnu11", "-O0", "-c", "hook.c"],
	               cwd=tmp, check=True)
	subprocess.run(
		[HOST_CC or "cc", "-std=gnu11", "-O0", "-finstrument-functions",
		 f"-DWALK_DEPTH_MAX={CEILING}u", f"-I{ROOT / 'walker' / 'c'}",
		 "drive.c", str(ROOT / "walker" / "c" / "situ_walk.c"), "hook.o",
		 "-o", "probe"],
		cwd=tmp, check=True, capture_output=True, text=True)
	return [str(tmp / "probe"), str(tmp / "img"), "MESSAGE", str(shape)]


def walker_cost(command: list[str], message: bytes,
		tmp: Path) -> tuple[int | None, str]:
	"""`cost`, with the message where this driver takes it."""
	if "MESSAGE" in command:
		held = list(command)
		held[held.index("MESSAGE")] = message.hex()
		with (tmp / "answer").open("wb") as out:
			ran = subprocess.run(held, stdout=out, stderr=subprocess.PIPE,
			                     text=True, cwd=tmp, timeout=1200)
		said = (tmp / "answer").read_text(encoding="latin-1")
		if ran.returncode == 77:
			return None, said
		assert ran.returncode == 0, (ran.returncode, ran.stderr[-2000:])
		found = re.findall(r"^ticks (\d+)$", ran.stderr, re.M)
		assert found, ran.stderr[-2000:]
		return int(found[-1]), said
	return cost(command, message, tmp)


TARGETS = ("c", "cpp", "rust", "python")

#: Shapes a backend has never built, whatever this change does: `pad_to` in a
#: recursive struct has no extent in C++, Rust or Python, a condition over an
#: enum behind a variable member reads a field in `required` where only a view
#: can, C++ has no reading of a varint in a condition, and Python has no
#: `_value` for fixed digits at a dynamic offset.
#: What a level costs in the worst of these is the validator asking each level
#: for its nesting, which walks every level beneath it and measures each: n^3
#: where the nesting grows with the message, so 2n costs 8 times n. Anything
#: exponential is past `BUDGET` long before this is asked.
GROWTH = 10

#: Sixty levels, and what the same message costs at half of them.
LEVELS = 60

#: Where that is the wrong pair. The reported input refuses early at some
#: lengths and late at others, so its cost is not monotonic below about forty
#: levels: 29 cost a fortieth of what 28 did, in C, and a ratio across that
#: says nothing about growth.
SPANS = {"json_cut": (LEVELS, LEVELS * 2)}


def growth(tmp: Path, command: list[str], message: object, ask: object,
		short: int, long: int) -> tuple[int, int]:
	"""The cost at `short` and at `long` levels, both inside the budget."""
	first,  _ = ask(command, message(short), tmp)  # type: ignore[operator]
	assert first is not None, f"{short} levels went past {BUDGET} calls"
	second, _ = ask(command, message(long), tmp)  # type: ignore[operator]
	assert second is not None, f"{long} levels went past {BUDGET} calls"
	return first, second


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("shape", list(SHAPES))
def test_sixty_levels_cost_a_polynomial(shape: str, target: str,
		tmp_path: Path) -> None:
	"""`validate` and `required`, on a message nested within its `[depth]`."""
	text, message = SHAPES[shape]
	command = build(tmp_path, text, target, top_of(shape))
	short, long = SPANS.get(shape, (LEVELS // 2, LEVELS))
	first, second = growth(tmp_path, command, message, cost, short, long)
	assert second <= first * GROWTH, (shape, target, first, second)


#: Shapes that reach their recursion by every construct that reads a field
#: behind it, asked `differ`'s whole list, every accessor and every prefix.
EVERY_QUESTION = ("json_whole", "member", "between", "arguments", "handed",
                  "length_behind", "count_behind", "switch_behind",
                  "arm_behind", "varint_behind")


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("shape", EVERY_QUESTION)
def test_every_question_costs_a_polynomial(shape: str, target: str,
		tmp_path: Path) -> None:
	"""An accessor measures what is in front of it, and that is one
	measurement: it has to stay one when each is asked in turn."""
	text, message = SHAPES[shape]
	command = build(tmp_path, text, target)
	first, second = growth(tmp_path, command, message, cost, 12, 24)
	# Each prefix of the message is another question, so one more factor of n
	# than the validator alone.
	assert second <= first * GROWTH * 2, (shape, target, first, second)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("name", ["json", "sexpr"])
def test_past_the_depth_it_costs_a_polynomial_too(name: str, target: str,
		tmp_path: Path) -> None:
	"""The schemas as they stand, `[depth = 64]`, at twice and four times it.

	A message past the bound is the one somebody chose, and it is refused:
	what matters is how long that takes. The reported input is here at the
	same depth, cut short in the middle of a string.
	"""
	text = (ROOT / "example" / name / f"{name}.situ").read_text(encoding="ascii")
	opens = {"json": b"[", "sexpr": b"("}[name]
	closes = {"json": b"]", "sexpr": b")"}[name]
	top = {"json": "value", "sexpr": "sexpr"}[name]
	command = build(tmp_path, text, target, top)

	def whole(n: int) -> bytes:
		return opens * n + (b"1" if name == "json" else b"a") + closes * n

	def cut(n: int) -> bytes:
		return opens * n + b'"\xff' + closes

	for message in (whole, cut):
		first, second = growth(tmp_path, command, message, cost, 64, 256)
		assert second <= first * GROWTH * 2, (name, target, first, second)
	said = cost(command, whole(256), tmp_path)[1]
	assert "validate 2" in said, said


@pytest.mark.parametrize("target", ("c", "python"))
@pytest.mark.parametrize("shape", list(TOPS) + [
	name for name in SHAPES if name not in TOPS and name != "digits_fixed"])
def test_the_walkers_cost_a_polynomial(shape: str, target: str,
		tmp_path: Path) -> None:
	"""Both walkers, with the ceiling out of the way.

	At their default of eight levels neither can multiply far, and a host tool
	reading deep JSON is told to raise it: the walk has to stay affordable when
	it does.
	"""
	text, message = SHAPES[shape]
	command = build_walker(tmp_path, text, top_of(shape), target)
	short, long = SPANS.get(shape, (LEVELS // 2, LEVELS))
	# Half the span for Python: about n^3 calls, and each one profiled, so
	# sixty levels is over `SLOW_BUDGET` while the ratio is still the ratio.
	if target == "python" and shape not in SPANS:
		short, long = short // 2, long // 2
	first, second = growth(tmp_path, command, message, walker_cost, short,
	                       long)
	assert second <= first * GROWTH, (shape, target, first, second)


#: The shapes measured by a cursor, which nothing in the corpus but JSON is.
AGREEING = ("member", "between", "arm", "handed", "length_behind",
            "switch_behind", "count_behind", "arm_behind", "nested_behind",
            "varint_behind", "digits_behind", "dotted_behind", "sum_behind",
            "lead", "two_arms", "sexpr", "counted", "delimited",
            "json_whole", "varint_while", "enum_while", "pad_behind",
            "digits_fixed")


@pytest.mark.parametrize("shape", AGREEING)
def test_six_readers_agree_on_each_shape(shape: str, tmp_path: Path) -> None:
	"""Within the bound and past it, whole, cut short and with a byte wrong.

	A field read from where the measurement found it is a second way of
	finding it, so agreement with the walkers, which find it otherwise, is
	what says the two ways are one.
	"""
	text, message = SHAPES[shape]
	bound = 4
	source = tmp_path / "shape.situ"
	source.write_text(text.replace(f"[depth = {DEEP}]", f"[depth = {bound}]"),
	                  encoding="ascii")
	commands = fourway.build(tmp_path, source)
	blob = pack_text(source.read_text(encoding="ascii"))
	struct = shape_named_text(source.read_text(encoding="ascii"), top_of(shape))
	drive = _build_driver(tmp_path, blob, VERDICT)

	rng = random.Random(shape)
	asked = 0
	for levels in range(1, bound + 4):
		whole = message(levels)  # type: ignore[operator]
		for buffer in (whole, whole[:-1], whole[:len(whole) // 2],
		               flipped(rng, whole), flipped(rng, whole)):
			said = {target: settled(fourway.answers(command, buffer, tmp_path))
			        for target, command in commands.items()}
			assert len(set(said.values())) == 1, (buffer.hex(), said)
			try:
				walked = python_verdict(blob, buffer, struct)
			except Refused:
				continue
			# `cannot-say` is not a contradiction. `variant_bits` spends a
			# level per hop to bound a malformed image, which it says, so the
			# C walker stops short of `[depth]` where an arm recurses. Two
			# answers that are both answers have to be the same answer.
			answered = c_verdict(tmp_path, blob, buffer, struct, drive)
			assert answered == walked or "cannot-say" in (answered, walked), \
				buffer.hex()
			asked += 1
	assert asked, "no message reached both walkers"


def settled(answer: str) -> str:
	"""A driver's answer without its `required` lines.

	Over a prefix cut inside a member behind the recursion, C's lower bound
	and the other three's are two different lower bounds. Both are correct as
	lower bounds, and neither changed here: this test is about where a field
	is found.
	"""
	return "\n".join(line for line in answer.splitlines()
	                 if not line.startswith("required"))


def flipped(rng: random.Random, buffer: bytes) -> bytes:
	"""`buffer` with one byte replaced, where it has one."""
	if not buffer:
		return buffer
	held = bytearray(buffer)
	held[rng.randrange(len(held))] = rng.choice(b"\x00\x01\x02,;[](1")
	return bytes(held)


def pack_text(text: str) -> bytes:
	source = Source("costs.situ", text)
	schema = parse(source)
	return pack(schema, resolve(schema, solve(schema)))[0]
