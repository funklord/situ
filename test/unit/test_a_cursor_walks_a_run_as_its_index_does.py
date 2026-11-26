"""A run's `_next` hands out what its indexed accessor does, in one pass (0060).

A run whose elements have no single size is Sequential: `_at(index)` walks
from the run's start on every call, so a loop over every element is
quadratic. raidcfgd and then fuzznet wrote the cursor by hand -- fuzznet
restating json's `while (sep == ',')` outside the schema -- and the holder
settled it on 2026-11-26: every backend generates `_next`.

Each backend DERIVES `_next` from the body of its own walking accessor
rather than writing a second loop: the accessor opens `at = <base>; n = 0`
and hands out the element at `n == index`, so resumed from the cursor the
same lines re-read the element handed out last -- which is where a `while`
run asks whether that element ended it -- and hand out the next. Python's
`while` and record runs index a list of starts walked in one pass, and the
cursor keeps that list. Where a body lacks the lines the derivation keys
on, nothing is emitted, so the pinned counts below are what catch a
derivation that quietly stopped matching.

The property is the one a caller relies on: the k-th call of `_next` on a
fresh cursor answers exactly what `_at(k - 1)` answers -- the same element,
or the same refusal -- past the run's end, and past an element that cannot
be read as well, since `_at` moves on from one too.
"""

from __future__ import annotations

import importlib.util
import random
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(RUNTIME / "python"))

from situc.cli import read_source			# noqa: E402
from situc.codegen.c import generate as c_generate	# noqa: E402
from situc.codegen.cpp import generate as cpp_generate	# noqa: E402
from situc.codegen.python import generate as py_generate	# noqa: E402
from situc.codegen.rust import generate as rust_generate	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse, parse_text		# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402
from situc import ast					# noqa: E402



def _runtime_of(loaded: ModuleType) -> ModuleType:
	"""The runtime the generated module actually bound.

	Not `import situ_runtime` here: another test in the same process may
	have put a copy from its own directory in `sys.modules` first, and a
	`View` from one copy is not a `View` of the other -- the differential
	then found no struct to ask and said so.
	"""
	return sys.modules[loaded.Message.__module__]

HOST_CC  = shutil.which("gcc") or shutil.which("cc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: How many runs get a cursor, per schema, measured 2026-11-26 when it
#: landed: every walked run of structs in the corpus (26.1172), and every
#: run of varints (26.1173): quic's one, wasm's seventh, edges' two. Each backend must
#: emit exactly this many, so a derivation that stops matching its
#: accessor's lines -- and so emits nothing -- fails here by name.
CURSORS = {
	"example/bitcoin/bitcoin.situ": 2, "example/ble/ble.situ": 1,
	"example/cbor/cbor.situ": 6, "example/dnsname/dnsname.situ": 5,
	"example/flac/flac.situ": 1, "example/hpack/hpack.situ": 1,
	"example/http/http.situ": 2, "example/id3/id3.situ": 1,
	"example/ipv4/ipv4.situ": 1, "example/ipv6ext/ipv6ext.situ": 1,
	"example/json/json.situ": 2, "example/matroska/matroska.situ": 3,
	"example/mqtt/mqtt.situ": 2, "example/netlink/netlink.situ": 1,
	"example/pcap/pcap.situ": 1, "example/png/png.situ": 1,
	"example/quic/quic.situ": 1,
	"example/radiotap/radiotap.situ": 1, "example/riff/riff.situ": 1,
	"example/sexpr/sexpr.situ": 1, "example/tcp/tcp.situ": 1,
	"example/tls/tls.situ": 1, "example/wasm/wasm.situ": 7,
	"example/zip/zip.situ": 1,
	"test/schema/edges.situ": 22,
}

#: The differential's schemas: json's `while` runs, cbor's record runs
#: (`until "\xff"`) and counted ones (`items[count]`), and edges, which
#: carries every shape including recursive and argument-taking runs, and
#: a counted run of varints and one sized by the frame; quic and wasm for
#: varint runs in a real format.
#: How many of each WALKED schema's cursors walk varints and take nothing
#: but the view, so a probe that stops finding them fails rather than
#: passing on the struct runs beside them.
VARINT_RUNS = {"example/json/json.situ": 0, "example/cbor/cbor.situ": 0,
               "test/schema/edges.situ": 2, "example/quic/quic.situ": 1,
               "example/wasm/wasm.situ": 1}

WALKED = ["example/json/json.situ", "example/cbor/cbor.situ",
          "test/schema/edges.situ", "example/quic/quic.situ",
          "example/wasm/wasm.situ"]

#: Bytes a run's own grammar is made of, mixed half and half with random
#: ones, so walks of more than one element happen at all: uniform bytes
#: almost never put json's `,` where a member's separator sits.
PICK = [0, 1, 2, 3, 4, 5, 8, 0x0A, 0x0D, 0x20, 0x22, 0x2C, 0x3A, 0x3B,
        0x5B, 0x5D, 0x7B, 0x7D, 0x80, 0xFF, 0x00, 0x2C, 0x2C, 0x7D]
ROUNDS = 3000
LONGEST = 96

#: The xorshift every compiled probe runs, so the four draw one sequence.
RNG = """static unsigned long long rng = 88172645463325252ull;
static unsigned r(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)rng; }
static const unsigned char pick[] = {%s};
""" % ", ".join(str(one) for one in PICK)


def _resolved(path: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse(read_source(ROOT / path))
	return schema, resolve(schema, solve(schema))


def _counts(path: str) -> dict[str, int]:
	schema, resolved = _resolved(path)
	return _counted(schema, resolved, Path(path).stem)


def _counted(schema: ast.Schema, resolved: ResolvedSchema,
		stem: str) -> dict[str, int]:
	c    = c_generate(schema, resolved, stem).header
	cpp  = cpp_generate(schema, resolved, stem).header
	rust = rust_generate(schema, resolved, stem).module
	py   = py_generate(schema, resolved, stem).module
	return {
		"c": len(re.findall(r"_next\(situ_view_t view, situ_cursor_t \*cursor",
		                    c)),
		# A recursive class is declared in one place and defined out of
		# line in another: count the definitions.
		"cpp": len(re.findall(r"_next\(::situ::rt::cursor &cursor,"
		                      r"(?![^\n]*;\n)", cpp)),
		"rust": rust.count("_next(&self, cursor: &mut situ_rt::Cursor)"),
		"python": py.count("_next(self, cursor: _situ_cursor)"),
	}


@pytest.mark.parametrize("path", sorted(CURSORS))
def test_every_backend_gives_each_walked_run_a_cursor(path: str) -> None:
	assert _counts(path) == dict.fromkeys(("c", "cpp", "rust", "python"),
	                                      CURSORS[path])


def test_a_fixed_stride_run_gets_no_cursor() -> None:
	"""The control for the counts above: a counted run of one-size elements
	is indexed by stride in constant time, and its accessor has none of the
	walk's lines -- while the same run of variable elements has them."""
	fixed = parse_text("target buffer;\nendian big;\n"
	                   "struct pt { u8 x; u8 y; }\n"
	                   "struct s { u8 n; pt pts[n]; }\n")
	walked = parse_text("target buffer;\nendian big;\n"
	                    "struct pt { u8 len; u8 body[len]; }\n"
	                    "struct s { u8 n; pt pts[n]; }\n")
	each = ("c", "cpp", "rust", "python")
	assert _counted(fixed, resolve(fixed, solve(fixed)), "fixed") \
		== dict.fromkeys(each, 0)
	assert _counted(walked, resolve(walked, solve(walked)), "walked") \
		== dict.fromkeys(each, 1)


# -- a walk worked by hand -----------------------------------------------------
#
# json's `members` over `"a":1,"bc":[]}`: the first member is `"a":1,` --
# six bytes, ending in the `,` that says another follows -- and the second
# `"bc":[]}`, eight bytes, whose `}` ends the run.

MEMBERS = b'"a":1,"bc":[]}'


def _json_python(tmp_path: Path) -> ModuleType:
	schema, resolved = _resolved("example/json/json.situ")
	module = tmp_path / "json_cursor.py"
	module.write_text(py_generate(schema, resolved, "json").module)
	spec = importlib.util.spec_from_file_location("json_cursor", module)
	assert spec is not None and spec.loader is not None
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	return loaded


def test_python_walks_json_members_by_hand(tmp_path: Path) -> None:
	json = _json_python(tmp_path)
	rt   = _runtime_of(json)
	view = json.members(rt.Message(bytearray(MEMBERS)), 0, len(MEMBERS))
	cursor = rt.Cursor()

	first = view.entries_next(cursor)
	assert (first._at, first._len, cursor.at, cursor.n) == (0, 6, 0, 1)
	second = view.entries_next(cursor)
	assert (second._at, second._len, cursor.at, cursor.n) == (6, 8, 6, 2)
	with pytest.raises(IndexError):
		view.entries_next(cursor)
	assert cursor.n == 2, "the end must not move the cursor"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_walks_json_members_by_hand(tmp_path: Path) -> None:
	schema, resolved = _resolved("example/json/json.situ")
	built = c_generate(schema, resolved, "json")
	(tmp_path / "json.h").write_text(built.header)
	(tmp_path / "json.c").write_text(built.source)
	probe = tmp_path / "probe.c"
	probe.write_text("""#include <stdio.h>
#include "json.h"
int main(void)
{
	uint8_t buf[] = "%s";
	situ_msg_t msg; situ_view_t view, one;
	situ_cursor_t cursor = {0};
	situ_err_t e;
	situ_msg_init(&msg, buf, sizeof buf - 1u);
	if (situ_view_at(&msg, 0, sizeof buf - 1u, &view) != SITU_OK) return 2;
	while ((e = situ_members_entries_next(view, &cursor, &one)) == SITU_OK)
		printf("%%u+%%u ", (unsigned)(one.base - buf), one.limit);
	printf("%%d %%u %%u\\n", (int)e, cursor.at, cursor.n);
	return 0;
}
""" % MEMBERS.decode().replace('"', '\\"'))
	assert HOST_CC is not None
	_run([HOST_CC, "-std=c11", "-Wall", "-Wextra", "-Wconversion",
	      "-Wsign-conversion", "-Werror", f"-I{RUNTIME / 'c'}",
	      f"-I{tmp_path}", str(probe), str(tmp_path / "json.c"),
	      str(RUNTIME / "c" / "situ.c"), "-o", str(tmp_path / "probe")])
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=60)
	assert ran.stdout == f"0+6 6+8 {_bounds()} 6 2\n"


def _bounds() -> int:
	"""SITU_ERR_BOUNDS, read from the header rather than assumed."""
	text  = (RUNTIME / "c" / "situ.h").read_text()
	found = re.search(r"SITU_ERR_BOUNDS\s*=\s*(-?\d+)", text)
	assert found, "SITU_ERR_BOUNDS is not an enumerator in situ.h"
	return int(found.group(1))


def _run(command: list[str]) -> None:
	done = subprocess.run(command, capture_output=True, text=True,
	                      timeout=900)
	assert done.returncode == 0, done.stderr[-3000:]


# -- the differential: `_next` against `_at`, per backend ----------------------

def _tally(stdout: str, path: str, tested: int) -> None:
	walks, multi, bad = (int(word) for word in stdout.split())
	assert bad == 0
	assert tested > 0 and walks > 0, f"{path}: no cursor was asked anything"
	assert multi > 0, f"{path}: no walk reached a second element"


C_CALL = """	{ situ_cursor_t c = {0u, 0u}; situ_view_t a, b; uint32_t got = 0u;
	  walks++;
	  for (uint32_t i = 0u; i < len + 2u; i++) {
		situ_err_t ea = %(f)s_at(view, i, &a);
		situ_err_t eb = %(f)s_next(view, &c, &b);
		if (ea != eb || (ea == SITU_OK && (a.base != b.base || a.limit != b.limit))) {
			fprintf(stderr, "%(f)s len=%%u i=%%u\\n", (unsigned)len, (unsigned)i);
			bad++;
			break;
		}
		if (ea == SITU_OK) { got++; }
	  }
	  if (got >= 2u) { multi++; } }
"""

#: The same for a run of varints, whose element is a value.
C_VALUE_CALL = """	{ situ_cursor_t c = {0u, 0u}; %(t)s a = 0, b = 0; uint32_t got = 0u;
	  walks++;
	  for (uint32_t i = 0u; i < len + 2u; i++) {
		situ_err_t ea = %(f)s_at(view, i, &a);
		situ_err_t eb = %(f)s_next(view, &c, &b);
		if (ea != eb || (ea == SITU_OK && a != b)) {
			fprintf(stderr, "%(f)s len=%%u i=%%u\\n", (unsigned)len, (unsigned)i);
			bad++;
			break;
		}
		if (ea == SITU_OK) { got++; }
	  }
	  if (got >= 2u) { multi++; } }
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("path", WALKED)
def test_c_next_answers_as_at_does(path: str, tmp_path: Path) -> None:
	schema, resolved = _resolved(path)
	stem  = Path(path).stem
	built = c_generate(schema, resolved, stem)
	(tmp_path / f"{stem}.h").write_text(built.header)
	(tmp_path / f"{stem}.c").write_text(built.source)
	# Only the cursors taking nothing but the view: one handed a parent's
	# argument has nothing a random buffer can supply.
	found = re.findall(r"static inline situ_err_t (\w+)_next\(situ_view_t view, "
	                   r"situ_cursor_t \*cursor, situ_view_t \*out\)", built.header)
	valued = re.findall(r"static inline situ_err_t (\w+)_next\(situ_view_t view, "
	                    r"situ_cursor_t \*cursor, (u?int64_t) \*out\)",
	                    built.header)
	c_calls = ("".join(C_CALL % {"f": name} for name in found)
	           + "".join(C_VALUE_CALL % {"f": name, "t": kind}
	                     for name, kind in valued))
	probe = tmp_path / "probe.c"
	probe.write_text(f"""#include <stdio.h>
#include "{stem}.h"
{RNG}
int main(void)
{{
	static uint8_t buf[{LONGEST}];
	unsigned long walks = 0, multi = 0, bad = 0;
	for (int round = 0; round < {ROUNDS}; round++) {{
		uint32_t len = r() % {LONGEST + 1}u;
		situ_msg_t msg; situ_view_t view;
		for (uint32_t i = 0u; i < len; i++)
			buf[i] = (r() & 1u) ? pick[r() % sizeof pick] : (uint8_t)r();
		situ_msg_init(&msg, buf, len);
		if (situ_view_at(&msg, 0, len, &view) != SITU_OK) continue;
{c_calls}
	}}
	printf("%lu %lu %lu\\n", walks, multi, bad);
	return 0;
}}
""")
	assert HOST_CC is not None
	_run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{tmp_path}", str(probe),
	      str(tmp_path / f"{stem}.c"), str(RUNTIME / "c" / "situ.c"),
	      "-o", str(tmp_path / "probe")])
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=600)
	assert ran.returncode == 0, ran.stderr[-2000:]
	_tally(ran.stdout, path, len(found) + len(valued))
	assert len(valued) == VARINT_RUNS[path], valued
	assert "past" not in ran.stderr and ran.stderr == "", ran.stderr[-2000:]


CPP_CALL = """	{ const ::situ::%(cls)s obj(view); ::situ::rt::cursor c{}; %(out)s a, b; std::uint32_t got = 0; walks++;
	  for (std::uint32_t i = 0; i < len + 2u; i++) {
		auto ea = obj.%(at)s(i, a);
		auto eb = obj.%(nx)s(c, b);
		if (ea != eb || (ea == ::situ::rt::err::ok && (a.raw().base != b.raw().base || a.raw().limit != b.raw().limit))) {
			std::fprintf(stderr, "%(cls)s::%(nx)s len=%%u i=%%u\\n", (unsigned)len, (unsigned)i);
			bad++;
			break;
		}
		if (ea == ::situ::rt::err::ok) { got++; }
	  }
	  if (got >= 2) { multi++; } }
"""

CPP_VALUE_CALL = """	{ const ::situ::%(cls)s obj(view); ::situ::rt::cursor c{}; %(out)s a = 0, b = 0; std::uint32_t got = 0; walks++;
	  for (std::uint32_t i = 0; i < len + 2u; i++) {
		auto ea = obj.%(at)s(i, a);
		auto eb = obj.%(nx)s(c, b);
		if (ea != eb || (ea == ::situ::rt::err::ok && a != b)) {
			std::fprintf(stderr, "%(cls)s::%(nx)s len=%%u i=%%u\\n", (unsigned)len, (unsigned)i);
			bad++;
			break;
		}
		if (ea == ::situ::rt::err::ok) { got++; }
	  }
	  if (got >= 2) { multi++; } }
"""


def _cpp_calls(header: str) -> list[str]:
	"""One probe per cursor whose class and element take only a view."""
	lines = header.splitlines()
	calls: list[str] = []
	owner = indexed = None
	for at, line in enumerate(lines):
		found = re.match(r"class (\w+) (?:final )?: public ::situ::rt::view", line)
		if found:
			owner = found.group(1)
		found = re.match(r"\t\[\[nodiscard\]\] ::situ::rt::err (\w+)"
		                 r"\(std::uint32_t index,(.*)", line)
		if found:
			rest    = found.group(2) if "&out" in found.group(2) else lines[at + 1]
			taken   = re.search(r"(\S+) &out", rest)
			indexed = (found.group(1), taken.group(1)) if taken else None
		found = re.match(r"\t\[\[nodiscard\]\] ::situ::rt::err (\w+)_next"
		                 r"\(::situ::rt::cursor &cursor,", line)
		if not found or indexed is None or owner is None:
			continue
		owned = f"explicit constexpr {owner}(situ_view_t raw) noexcept" in header
		if indexed[1] in ("std::uint64_t", "std::int64_t"):
			if owned:
				calls.append(CPP_VALUE_CALL % {"cls": owner, "out": indexed[1],
				                               "at": indexed[0],
				                               "nx": found.group(1) + "_next"})
			continue
		out   = indexed[1] if "::" in indexed[1] else f"::situ::{indexed[1]}"
		plain = (f"explicit constexpr {owner}(situ_view_t raw) noexcept" in header
		         and f"explicit constexpr {out.rsplit('::', 1)[-1]}(situ_view_t raw)"
		             " noexcept" in header)
		if plain:
			calls.append(CPP_CALL % {"cls": owner, "out": out, "at": indexed[0],
			                         "nx": found.group(1) + "_next"})
	return calls


@pytest.mark.skipif(HOST_CC is None or HOST_CXX is None,
                    reason="no host C and C++ compilers")
@pytest.mark.parametrize("path", WALKED)
def test_cpp_next_answers_as_at_does(path: str, tmp_path: Path) -> None:
	schema, resolved = _resolved(path)
	stem   = Path(path).stem
	header = cpp_generate(schema, resolved, stem).header
	cbuilt = c_generate(schema, resolved, stem)
	(tmp_path / f"{stem}.hpp").write_text(header)
	# The C++ header calls the C one's kernels: both are built.
	(tmp_path / f"{stem}.h").write_text(cbuilt.header)
	(tmp_path / f"{stem}.c").write_text(cbuilt.source)
	calls = _cpp_calls(header)
	cpp_calls = "".join(calls)
	probe = tmp_path / "probe.cpp"
	probe.write_text(f"""#include <cstdio>
#include "{stem}.hpp"
{RNG}
int main()
{{
	static std::uint8_t buf[{LONGEST}];
	unsigned long walks = 0, multi = 0, bad = 0;
	for (int round = 0; round < {ROUNDS}; round++) {{
		std::uint32_t len = r() % {LONGEST + 1}u;
		situ_msg_t msg; situ_view_t view;
		for (std::uint32_t i = 0; i < len; i++)
			buf[i] = (r() & 1u) ? pick[r() % sizeof pick] : (std::uint8_t)r();
		situ_msg_init(&msg, buf, len);
		if (situ_view_at(&msg, 0, len, &view) != SITU_OK) continue;
{cpp_calls}
	}}
	std::printf("%lu %lu %lu\\n", walks, multi, bad);
	return 0;
}}
""")
	assert HOST_CC is not None and HOST_CXX is not None
	objects = []
	for source in (tmp_path / f"{stem}.c", RUNTIME / "c" / "situ.c"):
		made = tmp_path / (source.name + ".o")
		_run([HOST_CC, "-std=c11", "-O1", "-c", f"-I{RUNTIME / 'c'}",
		      f"-I{tmp_path}", str(source), "-o", str(made)])
		objects.append(str(made))
	_run([HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Werror",
	      f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", f"-I{tmp_path}",
	      str(probe), *objects, "-o", str(tmp_path / "probe")])
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=600)
	assert ran.returncode == 0 and ran.stderr == "", ran.stderr[-2000:]
	_tally(ran.stdout, path, len(calls))
	assert sum("a = 0, b = 0" in call for call in calls) == VARINT_RUNS[path]


RUST_PROBE = """
pub fn cursor_probe() -> (u64, u64, u64) {
	let pick: [u8; %(picks)d] = [%(pick)s];
	let mut rng: u64 = 88172645463325252;
	let mut r = move || { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; rng as u32 };
	let (mut walks, mut multi, mut bad) = (0u64, 0u64, 0u64);
	let mut buf = [0u8; %(longest)d];
	for _ in 0..%(rounds)d {
		let len = (r() %% %(span)d) as usize;
		for i in 0..len { buf[i] = if r() & 1 == 1 { pick[(r() as usize) %% %(picks)d] } else { r() as u8 }; }
		let bytes = &buf[..len];
%(calls)s
	}
	(walks, multi, bad)
}
"""

RUST_CALL = """		if let Ok(obj) = %(cls)s::new(bytes) {
			walks += 1;
			let mut c = crate::situ_rt::Cursor::default();
			let mut got = 0u64;
			for i in 0..len + 2 {
				let a = obj.%(at)s(i);
				let b = obj.%(nx)s(&mut c);
				match (&a, &b) {
					(Ok(x), Ok(y)) => {
						got += 1;
						if %(differ)s {
							eprintln!("%(cls)s.%(nx)s len={} i={}", len, i); bad += 1; break;
						}
					}
					(Err(_), Err(_)) => {}
					_ => { eprintln!("%(cls)s.%(nx)s len={} i={}", len, i); bad += 1; break; }
				}
			}
			if got >= 2 { multi += 1; }
		}
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("path", WALKED)
def test_rust_next_answers_as_at_does(path: str, tmp_path: Path) -> None:
	schema, resolved = _resolved(path)
	stem   = Path(path).stem
	module = rust_generate(schema, resolved, stem).module
	calls: list[str] = []
	owner, plain = None, False
	valued_runs = 0
	for line in module.splitlines():
		found = re.match(r"impl<'a> (\w+)<'a> \{", line)
		if found:
			owner, plain = found.group(1), False
		if line == "\tpub fn new(bytes: &'a [u8]) -> Result<Self> {":
			plain = True
		found = re.match(r"\tpub fn (\w+)_next\(&self, cursor: &mut situ_rt::Cursor\)"
		                 r" -> Result<(\w+)", line)
		valued = found is not None and found.group(2) in ("u64", "i64")
		differ = ("x != y" if valued else
		          "x.bytes.as_ptr() != y.bytes.as_ptr() || x.bytes.len() != y.bytes.len()")
		# A run of structs is indexed by `x(i)`, one of varints by `x_at(i)`.
		at = next((name for name in (found.group(1), found.group(1) + "_at")
		           if f"\tpub fn {name}(&self, index: usize)" in module),
		          None) if found else None
		if found and plain and at is not None:
			valued_runs += valued
			calls.append(RUST_CALL % {"cls": owner, "at": at,
			                          "nx": found.group(1) + "_next",
			                          "differ": differ})
	# Appended to the module, so the probe can read each element's bytes.
	(tmp_path / f"{stem}.rs").write_text(module + RUST_PROBE % {
		"picks": len(PICK), "pick": ", ".join(str(one) for one in PICK),
		"longest": LONGEST, "rounds": ROUNDS, "span": LONGEST + 1,
		"calls": "".join(calls)})
	(tmp_path / "situ_rt.rs").write_text((RUNTIME / "rust" / "situ_rt.rs").read_text())
	(tmp_path / "main.rs").write_text(
		f"#![allow(dead_code, unused)]\nmod situ_rt;\nmod {stem};\n"
		f"fn main() {{ let (w, m, b) = {stem}::cursor_probe();"
		' println!("{} {} {}", w, m, b); }\n')
	assert RUSTC is not None
	_run([RUSTC, "--edition", "2021", "-O", "-o", str(tmp_path / "probe"),
	      str(tmp_path / "main.rs")])
	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, timeout=600)
	assert ran.returncode == 0 and ran.stderr == "", ran.stderr[-2000:]
	_tally(ran.stdout, path, len(calls))
	assert valued_runs == VARINT_RUNS[path], valued_runs


@pytest.mark.parametrize("path", WALKED)
def test_python_next_answers_as_at_does(path: str, tmp_path: Path) -> None:
	schema, resolved = _resolved(path)
	stem   = Path(path).stem
	module = tmp_path / f"{stem}_cursor.py"
	module.write_text(py_generate(schema, resolved, stem).module)
	spec = importlib.util.spec_from_file_location(module.stem, module)
	assert spec is not None and spec.loader is not None
	loaded = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(loaded)
	rt = _runtime_of(loaded)

	pairs = []
	for kind in vars(loaded).values():
		if not (isinstance(kind, type) and issubclass(kind, rt.View)):
			continue
		# A struct handed a parent's argument has nothing a random buffer
		# can supply.
		if any(name.startswith("arg_")
		       for name in getattr(kind, "__annotations__", {})):
			continue
		for name in list(vars(kind)):
			# `cursor`, not just the suffix: a tlv region's walk is
			# `x_first()` and `x_next(item)`, a different API.
			if name.endswith("_next") and "cursor" in \
					getattr(kind, name).__code__.co_varnames[:2]:
				# A run of structs is indexed by `x(i)`, one of varints
				# by `x_at(i)`.
				base = name.removesuffix("_next")
				pairs.append((kind, base if base in vars(kind)
				              else base + "_at", base))

	draw  = random.Random(7)
	walks = multi = 0
	for _ in range(ROUNDS):
		length = draw.randrange(LONGEST + 1)
		data   = bytearray(draw.choice(PICK) if draw.random() < 0.5
		                   else draw.randrange(256) for _ in range(length))
		message = rt.Message(data)
		for kind, name, base in pairs:
			try:
				view = kind(message, 0, length)
			except rt.SituError:
				continue
			walks += 1
			cursor = rt.Cursor()
			got    = 0
			for index in range(length + 2):
				expected = _answer(lambda: getattr(view, name)(index),
				                   rt.SituError)
				answered = _answer(lambda: getattr(view, base + "_next")(cursor),
				                   rt.SituError)
				assert expected == answered, (kind.__name__, name, bytes(data), index)
				got += expected[0] is None
			multi += got >= 2
	assert pairs and walks > 0, f"{path}: no cursor was asked anything"
	assert sum(name != base for _, name, base in pairs) == VARINT_RUNS[path]
	assert multi > 0, f"{path}: no walk reached a second element"


def _answer(call: Callable[[], Any], refusal: type[BaseException]
		) -> tuple[type | None, tuple[int, int] | int | None]:
	"""What one call hands back. Only the two refusals are answers; any
	other exception is a crash and propagates, rather than agreeing with
	the same crash on the other side."""
	try:
		element = call()
	except (IndexError, refusal) as refused:
		return type(refused), None
	if isinstance(element, int):
		return None, element
	return None, (element._at, element._len)
