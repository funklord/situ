"""The Python backend (section 26.17).

Two things are worth testing. That the generated module describes the same
bytes as the C -- three backends over one layout that disagreed would mean a
schema means three things -- and that the places Python cannot enforce the
lattice say so, because a reader who has seen the C++ backend would otherwise
assume the guarantees came along.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.python import generate as generate_py
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

import python_floor
from every_schema import ROOT, SCHEMAS, ids

RUNTIME = ROOT / "runtime"
HOST_CC = shutil.which("gcc") or shutil.which("cc")
LIBSITU = ROOT / "build" / "host" / "runtime" / "libsitu.a"

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_py(schema, resolved, "unit").module


def emit_materialized(body: str, preamble: str = PREAMBLE) -> str:
	"""The second accessor family as well (decision 0022)."""
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_py(schema, resolved, "unit", materialize=True).module


def load(tmp_path: Path, body: str, preamble: str = PREAMBLE,
		module_text: str | None = None, materialize: bool = False) -> ModuleType:
	"""Generate the module, import it, and hand it back."""
	if module_text is None:
		module_text = (emit_materialized(body, preamble) if materialize
		               else emit(body, preamble))
	(tmp_path / "unit.py").write_text(module_text, encoding="ascii")

	runtime()			# so the generated import finds the cached one
	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location("unit", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		sys.path.remove(str(tmp_path))


def runtime() -> ModuleType:
	"""The Python runtime, loaded once and cached.

	By path rather than by name because it is not on the path mypy sees: it
	ships beside the generated code, not inside `situc`. Cached in
	`sys.modules` so the generated modules import the same object -- two copies
	would give two `ConstraintError` classes, and `except` would miss.
	"""
	cached = sys.modules.get("situ_runtime")
	if cached is not None:
		return cached

	spec = importlib.util.spec_from_file_location(
		"situ_runtime", RUNTIME / "python" / "situ_runtime.py")
	assert spec is not None and spec.loader is not None

	module = importlib.util.module_from_spec(spec)
	sys.modules["situ_runtime"] = module
	spec.loader.exec_module(module)
	return module


def test_an_arithmetic_count_counts_elements(tmp_path: Path) -> None:
	"""`T name[N]` counts elements, and every backend rendered `sized_by` as
	`count * width`. The `size_expr` branch beside it did not: `u32 d[n + 1]`
	advanced `n + 1` bytes where the solver said `(n + 1) * 4`, so the compiler
	disagreed with its own accessors by a factor of four and `tail` was read
	out of the array.

	Executed rather than matched, and against the solver's own arithmetic:
	`n = 2` means three `u32`s, so `tail` is at 1 + 12."""
	body   = "struct s { u8 n; u32 d[n + 1]; u16 tail; }\n"
	module = load(tmp_path, body)

	buf = bytearray(32)
	buf[0] = 2
	buf[13], buf[14] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.tail == 0xABCD


def test_a_packed_pair_before_a_dynamic_member_is_one_byte(
		tmp_path: Path) -> None:
	"""Executed rather than matched. `hi` and `n` are a nibble each, so `d`
	starts at byte 1 and two `u32`s put `tail` at 9. The offset sum divided
	each member by eight and added the quotients, so it said 8."""
	module = load(tmp_path, "struct s { u4 hi; u4 n; u32 d[n]; u16 tail; }\n",
	              preamble="endian big;\nbit_order msb_first;\n")

	buf = bytearray(32)
	buf[0] = 0x02				# hi = 0, n = 2
	buf[9], buf[10] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.tail == 0xABCD


def test_a_fixed_width_text_number_can_drive_a_length(tmp_path: Path) -> None:
	"""`decimal u32 n[4]` is a length field written as digits, and an
	expression over it names `n_value` -- the read that cannot fail, which
	only the delimited and nested forms of a text number emitted. This shape
	produced a module referring to a property nothing defined, and generated C
	that did not compile.

	Every text driver in `example/` is delimited or nested, which is why."""
	module = load(tmp_path, "struct s { decimal u32 n[4]; u16 d[n]; u16 tail; }\n")

	buf = bytearray(b"0004" + bytes(28))
	buf[12], buf[13] = 0xAB, 0xCD		# 4 + four u16s

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.n == 4
	assert held.tail == 0xABCD


def test_a_varint_can_drive_an_arithmetic_length(tmp_path: Path) -> None:
	"""Executed. `n = 2` means three `u16`s after the varint's one byte, so
	`tail` is at 7. The expression named `n` and nothing defined it."""
	module = load(
		tmp_path, "struct s { v n; u16 d[n + 1]; u16 tail; }\n",
		preamble=("endian big;\nvarint_type v { encoding = be128;"
		          " max_bits = 64; max_bytes = 9; }\n"))

	buf = bytearray(32)
	buf[0] = 2
	buf[7], buf[8] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.tail == 0xABCD


def test_a_run_the_message_counts_is_read_by_index(tmp_path: Path) -> None:
	"""Executed. `u16 a[n]` is `u16 samples[4]` with its count in the message,
	and this handed back a `memoryview` of the raw bytes -- three lines under
	the comment saying why the constant-count form does not. A caller who cast
	it read host byte order for a schema that names big.

	The last assertion is the count: the message says four elements and the
	frame holds three, so a caller looping to it stays inside the buffer."""
	module = load(tmp_path, "struct s { u8 n; u16 a[n]; }\n")

	buf = bytearray(7)
	buf[0] = 4				# four elements claimed, three carried
	buf[1], buf[2] = 0x12, 0x34
	buf[3], buf[4] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.a(0) == 0x1234
	assert held.a(1) == 0xABCD
	assert held.a_count == 3
	with pytest.raises(IndexError):
		held.a(3)


def test_an_arithmetic_count_of_wide_elements_is_read_by_index(
		tmp_path: Path) -> None:
	"""The same array with its count written as arithmetic, which is the
	spelling that reached C's *scalar* branch: one getter, no index, and every
	element after the first unreachable in the one backend that decoded any of
	them."""
	module = load(tmp_path, "struct s { u8 n; i32 b[n + 1]; }\n")

	buf = bytearray(9)
	buf[0] = 1				# n + 1 == two elements
	buf[1], buf[2], buf[3], buf[4] = 0xFF, 0xFF, 0xFF, 0xFE
	buf[5], buf[6], buf[7], buf[8] = 0x00, 0x00, 0x01, 0x00

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.b_count == 2
	assert held.b(0) == -2			# signed, and indexed
	assert held.b(1) == 256


def test_a_run_may_walk_a_fixed_size_element(tmp_path: Path) -> None:
	"""Executed. Two elements of two bytes each, the second failing the
	condition, so the run is four bytes and `tail` is at 4."""
	module = load(tmp_path, "struct e { u8 k; u8 pad; }\n"
	                        "struct s { e c[] while (k == 1) max 4; u16 tail; }\n")

	buf = bytearray(32)
	buf[0] = 1				# first element continues the run
	buf[2] = 0				# second ends it, and is part of it
	buf[4], buf[5] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.c_count == 2
	assert held.c_span == 4
	assert held.tail == 0xABCD


def test_a_counted_run_of_variable_records_has_a_span(tmp_path: Path) -> None:
	"""Executed. Two records of two and three bytes, so the run is five and
	`tail` is at 6.

	The count says how many there are and each one says how long it is, which
	is a walk and not a stride. Nothing emitted a span for it: three backends
	declined every member after such a run, and C fell through to the
	counted-array branch and measured the run as `count` *bytes*."""
	module = load(tmp_path, "struct e { u8 k; u8 body[k]; }\n"
	                        "struct s { u8 c; e recs[c]; u16 tail; }\n")

	buf = bytearray(32)
	buf[0] = 2
	buf[1], buf[2] = 1, 0x11
	buf[3], buf[4], buf[5] = 2, 0x22, 0x33
	buf[6], buf[7] = 0xAB, 0xCD

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.recs_span == 5
	assert held.tail == 0xABCD


# -- the surface ------------------------------------------------------------


def test_fields_are_properties() -> None:
	"""A caller who has to write `packet.version()` writes the parser by hand
	instead, and a backend nobody uses enforces nothing at all."""
	module = emit("struct s { u8 version; u16 length; }")

	assert "\t@property\n\tdef version(self) -> int:" in module
	assert "\t@version.setter" in module


def test_what_a_property_hides_is_said_in_its_docstring() -> None:
	"""The syntax cannot show that a read costs a byte swap. The map can, and
	each field quotes it."""
	module = emit("struct s { u16 length; }")

	assert "repr=ValueConverted" in module


def test_a_covered_write_is_not_spelled_as_an_assignment() -> None:
	"""It leaves a tag stale, so it is not `x = value`. The C backend makes the
	same refusal and the two have to agree: a schema that means one thing in C
	must not mean another here."""
	module = emit("struct s { u8 hop; authenticated { u16 seq; } tag u8[16]; }")

	assert "No seq setter: writing it leaves tag stale." in module
	assert "def set_seq(self, msg: Message, value: int) -> None:" in module
	assert "msg.mark_dirty(self.DIRTY_TAG)" in module


def test_a_field_after_a_variable_member_still_writes_in_place() -> None:
	"""Its offset is dynamic; its mutation is not. Those are different axes and
	conflating them would refuse a setter the map allows."""
	module = emit("struct h { u8 v; u16 n; }"
	              "\nstruct s { h hdr; u8 opts[hdr.n]; u16 after; }")

	assert "offset=Dynamic" in module
	assert "\t@after.setter" in module


def test_an_enum_field_may_hold_a_value_that_is_not_a_member() -> None:
	"""Section 8.7's `default = error` rejects unknown values *on parse*, not on
	read -- and a getter is not where a caller should discover a malformed
	field. So the getter hands back the raw value and validate is what refuses."""
	module = emit("enum k : u8 { one = 1 }\nstruct s { k kind; }")

	assert "def kind(self) -> k | int:" in module
	assert "as_enum(k, " in module


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


def test_a_register_composes_words_without_claiming_to_drive_a_bus() -> None:
	"""Python cannot promise `volatile`, so this is not a driver -- and the
	docstring says so where a reader will look. What it does exactly is the
	arithmetic, which is the shape section 15 asks for anyway."""
	module = emit(REGISTER, preamble=MMIO)

	assert "cannot promise `volatile`" in module
	assert "does not drive a bus" in module or "not drive a bus" in module
	assert "def with_enable(self, value: int)" in module


def test_a_python_register_enforces_its_access_modes(tmp_path: Path) -> None:
	"""The same modes the C++ backend enforces, checked the way Python can:
	the attribute is simply not there."""
	module = load(tmp_path, REGISTER, preamble=MMIO)

	word = module.ctrl.word(0).with_enable(1).with_mode(5)

	assert word.raw == 0x15
	assert (word.enable, word.mode, word.busy) == (1, 5, 0)
	assert not hasattr(word, "with_busy")	# ro
	assert not hasattr(word, "start")	# wo
	assert not hasattr(word, "with_error")	# w1c is not an assignment


def test_a_compose_leaves_reserved_bits_alone(tmp_path: Path) -> None:
	"""`with_x` clears only its own bits, which is what `[preserve]` asks."""
	module = load(tmp_path, REGISTER, preamble=MMIO)

	word = module.ctrl.word(0xFFFFFFFF).with_mode(0)

	assert word.raw == 0xFFFFFFFF & ~(0x7 << 2)


# -- what it does at run time ------------------------------------------------


def test_it_reads_and_writes_through_the_caller_s_buffer(tmp_path: Path) -> None:
	"""Zero copy: a write through a view is visible to whoever owns the bytes."""
	module = load(tmp_path, "struct s [allow_straddle] { u4 a; u4 b; u16 c; bit d; u15 e; }")
	rt     = runtime()

	buf = bytearray(5)
	s   = module.s.at(rt.Message(buf))

	s.a = 0xA
	s.b = 0x5
	s.c = 0x1234
	assert buf[0] == 0xA5
	assert bytes(buf[1:3]) == b"\x12\x34"
	assert (s.a, s.b, s.c) == (0xA, 0x5, 0x1234)


def test_a_stale_view_is_refused(tmp_path: Path) -> None:
	"""Section 12.3, and the one place Python is stronger than release-build C:
	there the generation check compiles out."""
	module = load(tmp_path, "struct s { u16 a; }")
	rt     = runtime()

	msg = rt.Message(bytearray(4))
	s   = module.s.at(msg)
	assert s.a == 0

	msg.touch()
	with pytest.raises(rt.StaleViewError):
		_ = s.a


def test_a_short_buffer_is_refused(tmp_path: Path) -> None:
	module = load(tmp_path, "struct s { u32 a; }")
	rt     = runtime()

	with pytest.raises(rt.BoundsError):
		module.s.at(rt.Message(bytearray(3)))


def test_validate_raises_rather_than_returning(tmp_path: Path) -> None:
	"""A return code a Python caller silently drops is worse than an exception
	they have to catch. Idiom is not a capability."""
	module = load(tmp_path, "struct s { u8 v [must_eq = 1]; }")
	rt     = runtime()

	buf = bytearray(1)
	s   = module.s.at(rt.Message(buf))

	with pytest.raises(rt.ConstraintError):
		s.validate()

	buf[0] = 1
	s.validate()


def test_an_element_is_bounded_by_the_count(tmp_path: Path) -> None:
	module = load(tmp_path, "struct h { u8 v; u16 n; }\nstruct r { u32 id; }\n"
	                        "struct s { h hdr; r recs[hdr.n]; }")
	rt     = runtime()

	buf = bytearray(32)
	s   = module.s.at(rt.Message(buf), 0, 32)
	s.hdr.n = 2

	assert s.recs_count == 2
	assert s.recs(1)._at - s.recs(0)._at == 4
	with pytest.raises(IndexError):
		s.recs(2)


# -- the gate ---------------------------------------------------------------


SEALED = """codec aead {
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
		u8   session_key[16] [secret];
	}
	tag  u8[16];
}
"""


def test_the_gate_refuses_an_unverified_open(tmp_path: Path) -> None:
	module = load(tmp_path, SEALED)
	rt     = runtime()

	s = module.s.at(rt.Message(bytearray(128)))

	with pytest.raises(rt.TagError):
		s.open_sealed(False)

	assert s.open_sealed(True).kind == 0


def test_the_gate_says_it_is_weaker_here_than_in_cpp() -> None:
	"""A reader who has seen the C++ backend would otherwise assume the same
	strength carried over. It did not: Python has no access control, and
	`object.__new__` will make a gate whatever the class says."""
	module = emit(SEALED)

	assert "not the C++ guarantee" in module
	assert "object.__new__" in module


def test_a_secret_field_gets_no_accessor(tmp_path: Path) -> None:
	"""Section 14.6, and it holds in every backend."""
	module = emit(SEALED)

	assert "session_key is [secret]" in module
	assert "def session_key" not in module


SEALED_RUN = """codec aead {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl aead extern "my_aead";

struct s {
	u8   hop;
	u16  length [min = 4];
	u8   nonce[12];
	sealed(aead, nonce = nonce) {
		u8  payload[length - 4];
	}
	tag  u8[16];
}
"""


def test_a_byte_run_inside_the_gate_is_the_bytes(tmp_path: Path) -> None:
	"""`dtls.record.sealed.fragment` in miniature, and it is executed.

	A run whose length is arithmetic over a field has neither an
	`array_count` nor a `sized_by`, so admitting only `indexed_elements` let
	a run of *values* into the gate and kept a run of *bytes* out. DTLS's
	whole encrypted payload got no accessor and no note in this backend
	while C, C++ and Rust all answered it -- a member that simply vanishes,
	which is the one shape a reader cannot ask about (26.188).

	Read rather than grepped, because the length is computed through the
	gate's own view and an accessor that reaches the wrong bytes looks
	exactly like one that reaches the right ones.
	"""
	module = load(tmp_path, SEALED_RUN)
	rt     = runtime()

	# hop(1) length(2) nonce(12) payload(5) tag(16)
	buf = bytearray(36)
	buf[1:3]   = (9).to_bytes(2, "big")	# so `length - 4` is 5
	buf[15:20] = b"abcde"

	gate = module.s.at(rt.Message(buf), 0, len(buf)).open_sealed(True)

	assert bytes(gate.payload) == b"abcde"


def test_the_gate_declines_a_secret_run_exactly_once(tmp_path: Path) -> None:
	"""The note above and the note below are the same member.

	A `[secret]` run is a run, so it reaches the interior list now that byte
	runs do -- and it is also in the list of members that deliberately have
	no accessor. Both write a note, and two notes for one member is a report
	that reads like two findings.
	"""
	module = emit(SEALED)

	assert module.count("session_key is [secret]") == 1


def test_allow_unverified_read_leaves_no_gate_behind(tmp_path: Path) -> None:
	"""14.3's escape hatch, which for a long while only C honoured.

	`[allow_unverified_read]` sets `stage = TransformTime` on the interior,
	and the capability map is the arbiter. This backend emitted the gate
	anyway -- so a schema that had given the guarantee up in C still had it
	here, and the gate's own docstring told the reader the interior was
	"reachable only through a verified open", which is the opposite of what
	the schema says.

	The waiver moves the interior; it does not widen it. `session_key` is
	still `[secret]` and still gets nothing.
	"""
	body = SEALED.replace("sealed(aead, nonce = nonce) {",
	                      "sealed(aead, nonce = nonce) [allow_unverified_read] {")
	module = emit(body)

	assert "_sealed_gate" not in module
	assert "def open_sealed" not in module
	assert "bytes nobody has" in module
	assert "session_key is [secret]" in module
	assert "def session_key" not in module

	loaded = load(tmp_path, body, module_text=module)
	rt     = runtime()
	msg    = rt.Message(bytearray(128))
	view   = loaded.s.at(msg)

	# Read without opening anything: that is the whole of the waiver.
	assert view.sealed_kind == 0

	# And the write is the covered spelling, because a tag still covers it.
	assert not view.tag_is_dirty()
	view.set_sealed_kind(msg, 0x1234)
	assert view.sealed_kind == 0x1234
	assert view.tag_is_dirty()


# -- the claim that matters --------------------------------------------------


@pytest.mark.skipif(HOST_CC is None or not LIBSITU.exists(),
	reason="no host C compiler or runtime not built")
def test_the_python_and_c_backends_describe_the_same_bytes(tmp_path: Path) -> None:
	"""Three backends over one layout that disagreed would mean a schema means
	three things."""
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

	for name, text in generate_c(schema, resolved, "unit").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")

	(tmp_path / "probe.c").write_text('''
#include <stdio.h>
#include "unit.h"
int main(void)
{
	uint8_t buf[13];
	situ_msg_t msg;
	situ_view_t v;

	for (unsigned i = 0; i < sizeof buf; i++) {
		buf[i] = (uint8_t)(i * 11 + 3);
	}
	situ_msg_init(&msg, buf, sizeof buf);
	situ_hdr_view(&msg, 0, &v);

	printf("%u %u %u %u %u %u %u\\n",
	       situ_hdr_version_get(v), situ_hdr_ihl_get(v),
	       situ_hdr_total_get(v), situ_hdr_flag_get(v),
	       situ_hdr_offset_get(v), situ_hdr_proto_get(v),
	       situ_hdr_ttl_get(v));
	return 0;
}
''', encoding="ascii")

	assert HOST_CC is not None
	build = subprocess.run(
		[HOST_CC, "-std=c11", "-O1", f"-I{RUNTIME / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "unit.c"), str(LIBSITU),
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True, check=False)
	assert build.returncode == 0, build.stderr

	from_c = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                        text=True, check=False).stdout.split()

	module = load(tmp_path, body)
	rt     = runtime()
	buf    = bytearray((i * 11 + 3) & 0xFF for i in range(13))
	h      = module.hdr.at(rt.Message(buf))

	from_python = [str(int(value)) for value in
	               (h.version, h.ihl, h.total, h.flag, h.offset, h.proto, h.ttl)]

	assert from_python == from_c, f"python {from_python} != c {from_c}"


def test_an_enum_rejects_a_value_that_is_not_a_member(tmp_path: Path) -> None:
	"""Section 8.7. The gap that only surfaced because a third backend forced
	the comparison: C had never validated this either."""
	module = load(tmp_path, 'enum k : u8 { one = 1, two = 2 }\nstruct s { k kind; u8 pad; }')
	rt     = runtime()

	for value, admitted in ((1, True), (2, True), (9, False)):
		buf = bytearray([value, 0])
		s   = module.s.at(rt.Message(buf))

		if admitted:
			s.validate()
		else:
			with pytest.raises(rt.ConstraintError):
				s.validate()


def test_default_pass_admits_what_it_says_it_admits(tmp_path: Path) -> None:
	"""A schema that opts out of the rule is not second-guessed."""
	module = load(tmp_path, 'enum k : u8 { one = 1, two = 2, default = pass }\nstruct s { k kind; u8 pad; }')
	rt     = runtime()

	module.s.at(rt.Message(bytearray([9, 0]))).validate()


def test_a_constrained_field_at_a_dynamic_offset_is_validated(tmp_path: Path) -> None:
	"""Its offset is computed at run time; its constraint is checked all the
	same, which is what the C backend has always done."""
	module = load(tmp_path, "struct h { u8 v; u16 n; }\n"
	              "struct s { h hdr; u8 opts[hdr.n]; u16 after [must_eq = 7]; }")
	rt     = runtime()

	buf = bytearray(16)
	buf[2] = 2			# hdr.n = 2, so `after` lands at 5
	buf[5], buf[6] = 0, 7

	s = module.s.at(rt.Message(buf), 0, 16)
	assert s.after == 7
	s.validate()

	buf[6] = 9
	with pytest.raises(rt.ConstraintError):
		s.validate()


# -- delimited members (section 8.6) ----------------------------------------

BLOCK = (
	'struct kv { u8 key[] until ": "; u8 value[] until "\\r\\n"; }\n'
	'struct blk { kv entries[] until "\\r\\n"; u8 body[remaining]; }\n'
)

HTTP = (
	'struct msg {\n'
	'\tu8       method[] until " " max 8;\n'
	'\tdecimal  u32      length until "\\r\\n" max 12;\n'
	'\tu8       body[length];\n'
	'}\n'
)


def test_a_header_block_parses(tmp_path: Path) -> None:
	"""The claim the whole construct exists for, run rather than inspected."""
	module = load(tmp_path, BLOCK)
	raw    = bytearray(b"Host: example.com\r\nAccept: */*\r\n\r\nhello")
	view   = module.blk.at(runtime().Message(raw), 0, len(raw))

	assert view.entries_count == 2
	assert bytes(view.entries(0).key)   == b"Host"
	assert bytes(view.entries(0).value) == b"example.com"
	assert bytes(view.entries(1).key)   == b"Accept"
	assert bytes(view.body)             == b"hello"


def test_the_terminator_is_not_looked_for_inside_an_element(tmp_path: Path) -> None:
	"""A CRLF at the end of the first header line belongs to that line. Scanning
	for the terminator anywhere found it there and reported one field."""
	module = load(tmp_path, BLOCK)
	raw    = bytearray(b"A: 1\r\nB: 2\r\nC: 3\r\n\r\n")
	view   = module.blk.at(runtime().Message(raw), 0, len(raw))

	assert view.entries_count == 3


REQUEST = (b"GET /index.html HTTP/1.1\r\n"
	b"Host: example.com\r\n"
	b"Accept: */*\r\n\r\n")

RESPONSE = (b"HTTP/1.1 404 Not Found\r\n"
	b"Content-Length: 0\r\n\r\n")


def test_the_http_example_reads_an_http_message(tmp_path: Path) -> None:
	"""The worked example against the bytes it is about (26.32).

	Nothing did this. Every http test in the tree used a schema written for
	that test or a request line the dissector was handed, and `example/http`
	could not read a response: `max 3` on a three-digit status code caps the
	whole member, delimiter included (8.6.1), so the scan ran out at the third
	digit. `validate` called every real response a frame cut short, and
	`reason` kept the space that should have ended the code -- `" Not Found"`.

	A two-digit code parsed, which is why the property tests and the random
	buffers were all content.
	"""
	source = (ROOT / "example" / "http" / "http.situ").read_text(encoding="utf-8")
	module = load(tmp_path, "", module_text=emit(source, ""))
	rt     = runtime()

	raw  = bytearray(REQUEST)
	head = module.request_head.at(rt.Message(raw), 0, len(raw))
	head.validate()
	assert bytes(head.start.method)  == b"GET"
	assert bytes(head.start.target)  == b"/index.html"
	assert bytes(head.start.version) == b"HTTP/1.1"
	assert head.fields_count == 2
	assert bytes(head.fields(0).name)  == b"Host"
	assert bytes(head.fields(0).value) == b"example.com"

	raw  = bytearray(RESPONSE)
	held = module.response_head.at(rt.Message(raw), 0, len(raw))
	held.validate()
	held.start.validate()
	assert bytes(held.start.version) == b"HTTP/1.1"
	assert held.start.code           == 404
	assert bytes(held.start.reason)  == b"Not Found"
	assert held.fields_count == 1


def test_an_empty_run_has_no_elements(tmp_path: Path) -> None:
	"""The terminator standing where the first element would. The first thing
	a walk gets wrong, in one direction or the other."""
	module = load(tmp_path, BLOCK)
	raw    = bytearray(b"\r\nbody")
	view   = module.blk.at(runtime().Message(raw), 0, len(raw))

	assert view.entries_count == 0
	assert bytes(view.body) == b"body"


def test_an_index_past_the_end_raises(tmp_path: Path) -> None:
	module = load(tmp_path, BLOCK)
	raw    = bytearray(b"A: 1\r\n\r\n")
	view   = module.blk.at(runtime().Message(raw), 0, len(raw))

	with pytest.raises(IndexError):
		view.entries(1)


def test_a_text_length_drives_a_binary_body(tmp_path: Path) -> None:
	"""The shape that motivated the whole section: a header in text stating how
	long the bytes after it are."""
	module = load(tmp_path, HTTP)
	raw    = bytearray(b"GET 5\r\nhello")
	view   = module.msg.at(runtime().Message(raw), 0, len(raw))

	view.validate()
	assert view.length == 5
	assert bytes(view.body) == b"hello"


def test_digits_that_are_not_digits_raise(tmp_path: Path) -> None:
	"""Every other property here returns a value because every other conversion
	is total. Returning 0 for `5x` would hand back a number nobody wrote."""
	module = load(tmp_path, HTTP)
	raw    = bytearray(b"GET 5x\r\nhello")
	view   = module.msg.at(runtime().Message(raw), 0, len(raw))

	with pytest.raises(runtime().ConstraintError):
		view.length


def test_validate_reports_a_short_frame_before_the_digits(tmp_path: Path) -> None:
	"""For a frame cut short before the digits both are wrong, and "this frame
	stops early" is the more useful answer. C reports it in that order too."""
	module = load(tmp_path, HTTP)
	raw    = bytearray(b"GET ")
	view   = module.msg.at(runtime().Message(raw), 0, len(raw))

	with pytest.raises(runtime().ConstraintError, match="the frame stops first"):
		view.validate()


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


def test_an_unmeasurable_nested_member_gets_no_accessor(tmp_path: Path) -> None:
	"""A struct whose extent nothing can compute gets no accessor.

`label` ends in an `opaque` default arm, which swallows whatever is left, so
one is exactly as long as the view it was handed; `name` is a run of those, so
its own length is unknown in turn; and `question.qname` is one of *those*. Each backend emitted
the accessor anyway and reached for an extent it had declined to emit --
an `AttributeError` on first access here.

Nothing after such a member can be placed either, which is why `qtype` goes
with it: its offset is the extent nobody can compute.
"""
	module = load(tmp_path, UNMEASURABLE)

	assert not hasattr(module.question, "qname")
	assert not hasattr(module.question, "qname_extent")
	assert not hasattr(module.question, "qtype")


def test_and_validating_one_does_not_reach_through_it(tmp_path: Path) -> None:
	"""The path least likely to be exercised, and the one where an
	`AttributeError` would surface as a parse failure on real input."""
	module = load(tmp_path, UNMEASURABLE)
	raw    = bytes([3]) + b"www" + bytes([0])

	module.question.at(module.Message(bytearray(raw)), 0, len(raw)).validate()


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

NAMES = [
	# bytes, labels, extent
	(bytes([3]) + b"www" + bytes([7]) + b"example" + bytes([3]) + b"com"
	 + bytes([0]), 4, 17),
	(bytes([0xC0, 0x0C]), 1, 2),			# a pointer is a whole name
	(bytes([3]) + b"www" + bytes([0xC0, 0x0C]), 2, 6),	# www, then a pointer
]


def test_a_compressed_name_walks(tmp_path: Path) -> None:
	"""The four shapes a DNS name comes in, against a hand-checked count and
	extent -- the same table the C suite walks, because agreeing with the
	other backends is the property under test."""
	module = load(tmp_path, DNS_LABEL)

	for raw, labels, extent in NAMES:
		held = module.name.at(module.Message(bytearray(raw)), 0, len(raw))

		assert held.labels_count == labels, raw
		assert held.labels_span == extent, raw
		for index in range(labels):
			held.labels(index).validate()


def test_an_unrecognised_discriminant_is_rejected(tmp_path: Path) -> None:
	"""`form == 1` selects no arm, and `default: error` says there is no such
	message. Nothing rejected it before: the check had never been emitted by
	any backend, because nothing walked a variant to notice."""
	module = load(tmp_path, DNS_LABEL)
	raw    = bytes([0x40, 0x00])
	held   = module.name.at(module.Message(bytearray(raw)), 0, len(raw))

	# `VersionError`, which is what the runtime has called an unknown
	# discriminant since it was written and what the other three raise. This
	# expected a constraint failure, which is the opposite remedy: malformed
	# rather than newer than this code (19.4).
	with pytest.raises(module.VersionError):
		held.labels(0).validate()


# -- a length the message declares, and the frame it has to fit -------------

OVERLONG = "struct s { u8 n; u16 want; u8 body[want]; u8 tail[remaining]; }"


def test_a_declared_length_is_clamped_and_reported(tmp_path: Path) -> None:
	"""Python's slice already clamped, so this backend was memory-safe and
	silent: a message claiming 1000 bytes in a 16-byte frame handed back 13
	and said nothing. The other three were not safe, and none of the four
	reported it. Both halves now, in all four."""
	module = load(tmp_path, OVERLONG)
	raw    = bytearray(16)
	raw[1], raw[2] = 0x03, 0xE8		# says 1000 bytes of body

	held = module.s.at(module.Message(raw), 0, len(raw))

	assert len(held.body) == 13		# 16 bytes, 3 before the body
	# `BoundsError`: the message claims bytes that are not there, which is not
	# a value the schema forbids. The other three report it that way.
	with pytest.raises(module.BoundsError):
		held.validate()


# -- a member the data positions (section 9.8) ------------------------------

LOCATED = "struct s { u32 off; u16 n; u8 body[n] at off; u16 after; }"


def test_a_located_member_needs_no_extra_parameter_here(tmp_path: Path) -> None:
	"""A `View` already holds the `Message` it came from, so this backend can
	answer "where is offset zero" on its own. C, C++ and Rust carry a frame
	and nothing else and all three take the message. The asymmetry is in the
	runtimes, not in the construct."""
	module = load(tmp_path, LOCATED)
	raw    = bytearray(28)
	raw[4 + 3] = 16			# off = 16, from the message base
	raw[4 + 5] = 4			# n = 4
	raw[4 + 6], raw[4 + 7] = 0xBE, 0xEF
	raw[16:20] = b"DATA"

	held = module.s.at(module.Message(raw), 4)

	assert held.after == 0xBEEF	# placed as if `body` were not declared
	assert bytes(held.body) == b"DATA"


def test_and_the_offset_is_checked_on_every_read(tmp_path: Path) -> None:
	"""The frame starts at 4, not at 0. With it at 0 the frame base and the
	message base are the same address, and reading the offset from the wrong
	one gives the right answer -- which is how the C version of this test
	passed against a generator that used the frame."""
	module = load(tmp_path, LOCATED)
	raw    = bytearray(28)
	raw[4 + 5] = 4
	held   = module.s.at(module.Message(raw), 4)

	raw[4 + 3] = 200		# points outside the message
	with pytest.raises(module.BoundsError):
		held.body


# -- framing a stream -------------------------------------------------------

STREAM_FRAMED = "struct s { u8 version; u16 n; u8 body[n]; u16 trailer; }"


def test_required_never_reads_a_length_that_has_not_arrived(tmp_path: Path) -> None:
	"""Fed one byte at a time. Until `n` has wholly arrived the only honest
	answer is the minimum -- reading it from byte 1 alone would say 0x0004 or
	0x0400 depending on which byte turned up first, and that guess would size
	the next read. The same table the C suite walks."""
	module = load(tmp_path, STREAM_FRAMED)
	whole  = bytes([1, 0, 4]) + b"DATA" + bytes([0xBE, 0xEF])

	for have in range(len(whole)):
		with pytest.raises(module.TruncatedError) as caught:
			module.s.required(whole[:have])

		needed = caught.value.needed
		assert have < needed <= len(whole)
		if have < 3:
			# `n` is not wholly here, so the minimum is all that can be said.
			assert needed == module.s.SIZE_MIN
		elif have >= module.s.SIZE_MIN:
			# Past the gate, so `n` has been read and the answer is exact.
			assert needed == len(whole)

	assert module.s.required(whole) == len(whole)
	# More than a whole message is still one, and the answer is where it ends.
	assert module.s.required(whole + b"next") == len(whole)


TWO_LENGTHS = "struct s { u16 n; u8 a[n]; u16 m; u8 b[m]; }"


def test_a_length_behind_a_variable_member_resolves(tmp_path: Path) -> None:
	"""`m` sits at `2 + n`, so there is no constant to read it at -- and this
	backend read length drivers only at constant offsets, so `b` was dropped
	with a note and `required` declined along with it. Its own accessor knows
	where it is. C++, Python and Rust all had this; C did not."""
	module = load(tmp_path, TWO_LENGTHS)
	raw    = bytearray([0, 3]) + b"abc" + bytearray([0, 2]) + b"xy"
	held   = module.s.at(module.Message(raw), 0, len(raw))

	assert held.b_offset == 7
	assert bytes(held.b) == b"xy"
	assert module.s.required(bytes(raw)) == len(raw)


# -- a base the message puts past the end -----------------------------------

OVERREACHING = 'struct s { u16 n; u8 a[n]; u8 b[] until ";"; }'


def test_a_scan_base_past_the_frame_reads_nothing(tmp_path: Path) -> None:
	"""`n` is a `u16` the message chooses, so `b`'s base can sit past the end
	of a ten-byte frame. The scan limit was `len - base`, which underflows to
	about four billion, and the scan then searched that much memory.

	C++ read out of bounds -- an AddressSanitizer SEGV. Rust panicked on the
	slice before any limit applied. Python returned a wrong number. C had been
	saturating here since the `[remaining]` fix and the other three were not.
	All four now answer as C does: an empty scan."""
	module = load(tmp_path, OVERREACHING)
	raw    = bytearray(10)
	raw[0] = raw[1] = 0xFF		# n = 65535 in a ten-byte frame

	held = module.s.at(module.Message(raw), 0, 10)

	# The offset stops at the frame rather than running past it: every term of
	# it is a length the message chose (26.27).
	assert held.b_offset == 10
	assert held.b_len == 0


def test_an_offset_sum_keeps_a_running_total() -> None:
	"""Statements rather than one expression, because the running total has
	to be a variable. `0 + a_span + b_span` re-derives each term's base by
	rescanning everything before it, so the sum costs far more than the terms
	in it -- an eight-member record measured 389ms, then 113ms."""
	module = emit('struct s { u8 a[] until ";"; u8 b[] until ";"; u8 c[] until ";"; }')

	assert "at = 0" in module
	assert "at = advance(at, self.a_span_from(at), self._len)" in module
	assert "return 0 + self.a_span" not in module


# -- the second accessor family (decision 0022) -----------------------------

INDEXED_RUN = """
struct l { u2 f; u6 r; u8 t[r]; }
struct n { l ls[] while (f == 0 && r != 0) max 128; }
"""


def materialized(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_py(schema, resolved, "unit", materialize=True).module


def test_the_second_family_is_off_by_default() -> None:
	assert "def ls_all" not in emit(INDEXED_RUN)


def test_a_run_is_walked_once_rather_than_per_index(tmp_path: Path) -> None:
	"""`ls(i)` rebuilds the walk on every call, so visiting all of them is
	quadratic: measured at 745ms against 21ms for forty labels.

	No `max` is needed here, unlike C -- the cap there is how many offsets to
	hold, because generated C never allocates, and this list is the
	language's. One decision, a different construct in each.
	"""
	module = load(tmp_path, INDEXED_RUN,
		module_text=materialized(INDEXED_RUN))
	raw    = bytearray()
	for _ in range(40):
		raw += bytes([1, ord("a")])
	raw += bytes([0])

	held = module.n.at(module.Message(raw), 0, len(raw))
	one  = [(held.ls(i)._at, held.ls(i)._len) for i in range(held.ls_count)]
	all_ = [(e._at, e._len) for e in held.ls_all()]

	assert one == all_
	assert len(all_) == 41


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


def test_each_arm_refuses_when_it_is_not_the_one_present(tmp_path: Path) -> None:
	"""Raises rather than returning a code, which is this backend's
	convention: `VersionError` is what an unrecognised discriminant gets, and
	reading the arm that is not there is the same mistake from the other
	end."""
	module = load(tmp_path, ARMS)

	text = module.label.at(module.Message(bytearray(b"\x03www")), 0, 4)
	assert bytes(text.body_text) == b"www"
	with pytest.raises(module.VersionError):
		text.body_pointer_low

	ptr = module.label.at(module.Message(bytearray(b"\xC0\x0C")), 0, 2)
	assert ptr.body_pointer_low == 0x0C
	with pytest.raises(module.VersionError):
		ptr.body_text


def test_an_arm_that_does_not_fit_is_clamped_and_reported(tmp_path: Path) -> None:
	"""Both halves of 26.27's bargain, on a variant this time.

	`\\x37` is a length label declaring 55 bytes, and four follow it. The
	accessor hands back the four -- clamping is what keeps a caller who never
	validates inside the buffer, and C and C++ handed out the 55 until 26.35 --
	and `validate` is where the message is called malformed rather than short.

	Neither half alone is enough, which is why they are one test: clamping
	without the report turns a lie into a truncation, and the caller cannot
	tell a message that ends early from one that lied about its length.
	"""
	module = load(tmp_path, ARMS)

	short = module.label.at(module.Message(bytearray(b"\x37abcd")), 0, 5)
	assert bytes(short.body_text) == b"abcd"
	with pytest.raises(module.BoundsError):
		short.validate()

	whole = module.label.at(module.Message(bytearray(b"\x04abcd")), 0, 5)
	assert bytes(whole.body_text) == b"abcd"
	whole.validate()

	# The other arm is not this arm's business: a pointer label declares no
	# length, so nothing here has an opinion about how many bytes follow it.
	module.label.at(module.Message(bytearray(b"\xC0\x0C")), 0, 2).validate()


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


def test_an_enum_discriminant_selects(tmp_path: Path) -> None:
	"""This backend was the one that worked, and only because it emits
	`enum.IntEnum` -- so `K.a != 1` is False and the comparison means what it
	reads as. C++ and Rust hand back types that do not compare to a number,
	and neither compiled such a schema at all.
	"""
	module = load(tmp_path, ENUM_ARMS)
	held   = module.S.at(module.Message(bytearray([1, 0xBE, 0xEF, 0, 0])), 0, 5)

	assert held.v_p.x == 0xBEEF
	with pytest.raises(module.VersionError):
		held.v_q


# -- every example, imported ------------------------------------------------


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_generated_module_parses_at_the_declared_floor(schema: Path) -> None:
	"""Section 22 claims "Python (3.11+)", and that is a claim about *output*.

	The floor checks in `test_conventions` read the modules this repository
	ships. Generated code is neither shipped nor written by hand, so nothing
	asked it at all -- 33 modules and some nineteen thousand lines, verified
	only by being imported on whatever interpreter is present, which here is
	3.13. The backend could emit 3.12 grammar and every test would pass.

	`below_floor` is the same instrument the shipped modules get, and it is
	two instruments because each is blind where the other sees: `ast.parse`
	with `feature_version` catches grammar added since the floor and misses
	everything tokenizer-level, while the PEP 701 scan catches exactly the
	tokenizer-level f-string changes.
	"""
	if python_floor.floor_version() >= (3, 12):
		pytest.skip("the floor is 3.12 or later")

	parsed   = parse_text(schema.read_text(encoding="utf-8"))
	resolved = resolve(parsed, solve(parsed))
	module   = generate_py(parsed, resolved, schema.stem).module

	failed = [f"{line}: {why}" for line, why in python_floor.below_floor(module)]
	assert failed == [], (
		f"generated {schema.stem}.py needs newer than the declared floor:\n  "
		+ "\n  ".join(failed))


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_imports(schema: Path, tmp_path: Path) -> None:
	"""Python has no compiler, so importing is the check: it runs every class
	body, every annotation and every default. The C suite has compiled every
	example since phase 4 and the other three had no equivalent -- which is
	how three C++ examples and two Rust ones came to be broken.

	Every schema rather than every example, for the reason the C++ suite
	records: `test/schema/edges.situ` holds the constructs no worked example
	has, and a check globbing `example/` never reads it (26.31)."""
	runtime()
	sys.path.insert(0, str(tmp_path))
	try:
		parsed   = parse_text(schema.read_text(encoding="utf-8"))
		resolved = resolve(parsed, solve(parsed))
		module   = generate_py(parsed, resolved, schema.stem).module

		path = tmp_path / f"{schema.stem}.py"
		path.write_text(module, encoding="ascii")

		spec = importlib.util.spec_from_file_location(schema.stem, path)
		assert spec is not None and spec.loader is not None
		spec.loader.exec_module(importlib.util.module_from_spec(spec))
	finally:
		sys.path.remove(str(tmp_path))


# -- a coded region that ends at a delimiter (13.6) -------------------------

CODED = 'codec dot_stuffing {\n\tkernel = stuffing(worst_case = 4, per = 3, unit = stream, code = smtp_dot);\n}\nimpl dot_stuffing derived;\nstruct data_block {\n\tcoded body(dot_stuffing) until "\\r\\n.\\r\\n" {\n\t\tu8 content[remaining];\n\t}\n}\n'


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
	module = load(tmp_path, CODED)
	raw    = bytearray(b"Hello\r\n..dotted\r\n\r\n.\r\nX")
	held   = module.data_block.at(module.Message(raw), 0, len(raw))

	assert held.body_len == 17
	assert held.body_span == 22
	assert held.body_terminated


# -- a coded region's bytes, and its transform (13.5) -----------------------

CODED_PRE  = 'target buffer;\nendian big;\nbit_order msb_first;\ncodec halve { kernel = table(input_bits = 1, output_bits = 2, code = manchester_802_3); }\nimpl halve derived;\n'
CODED_BODY = 'struct S { coded body(halve) { u8 raw[4]; } }'


def test_a_coded_region_hands_back_its_encoded_bytes(tmp_path: Path) -> None:
	"""A region with no delimiter emitted nothing at all, so the bytes on the
	wire were unreachable -- odd for a treat-as-bytes region, and true only of
	that case: the scan path emits an accessor for the delimited one."""
	module = load(tmp_path, CODED_BODY, preamble=CODED_PRE)
	raw    = bytearray(range(8))
	held   = module.S.at(module.Message(raw), 0)

	assert bytes(held.body) == bytes(range(8))


SIZED_BODY = ("struct S { u8 n; coded body(halve) { u8 raw[n]; }"
	" u8 trailer; }")


def test_a_region_the_data_sizes_reports_the_bytes_it_occupies(
		tmp_path: Path) -> None:
	"""Every coded region in this tree was fixed inside or ended at a
	delimiter, and those were the two paths the emitters had. A region whose
	*interior* the data sizes is the third, and all four backends answered it
	with the region's minimum -- zero -- beside a pointer at the right place
	and no refusal, so a non-empty region's wire bytes were unreachable in
	every language at once (26.35).

	The module contradicted itself, which is what makes this checkable from
	one buffer: `body` claimed no bytes while `trailer`, placed after it,
	resolved through the same expansion and landed at the right offset. The
	length was the only thing not asking `traverse.region_extent`.

	`halve` doubles, so three interior bytes are six on the wire and the
	trailer is at 1 + 6.
	"""
	module = load(tmp_path, SIZED_BODY, preamble=CODED_PRE)
	raw    = bytearray([3, *range(0x10, 0x16), 0xFF])
	held   = module.S.at(module.Message(raw), 0, len(raw))

	assert len(bytes(held.body)) == 6
	assert bytes(held.body) == bytes(range(0x10, 0x16))
	assert held.trailer == 0xFF

	# And clamped where the message declares more than it sent, which is the
	# bargain every other data-driven length here strikes.
	short = module.S.at(module.Message(bytearray([9, 1, 2, 3])), 0, 4)
	assert len(bytes(short.body)) == 3


def test_and_says_why_the_decode_is_not_here(tmp_path: Path) -> None:
	"""C++ links the C codec for free and Rust declares it `extern "C"`; this
	one would have to load a shared object from a path situ has no convention
	for, and inventing one in a code generator is a policy decision. So the
	module says what to call and how large the buffer must be."""
	module = emit(CODED_BODY, CODED_PRE)

	assert "No `body_decode`" in module
	assert "situ_halve_decode" in module
	assert "4 bytes is what it needs" in module


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

# protoc --encode=User <<< 'user_id: 150; username: "situ"; score: 1.5'
WIRE = (bytes([0x08, 0x96, 0x01, 0x12, 0x04]) + b"situ"
	+ bytes([0x1D, 0x00, 0x00, 0xC0, 0x3F]))


def test_a_tlv_region_gets_a_walk() -> None:
	"""It answered REGION in the shared classifier and this backend said "not
	emitted by this backend yet" -- the fallthrough note, for the one construct
	section 9.7 makes the conformance gate."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "def fields_first(self) -> S_fields_item:" in module
	assert ("def fields_next(self, item: S_fields_item)"
	        " -> S_fields_item:") in module
	assert "not emitted by this backend yet" not in module


def test_iteration_is_a_generator() -> None:
	"""Where this backend departs from the other three on purpose: `for item
	in msg.fields()` is the shape a Python caller reaches for."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "\t\t\t\tyield item" in module


def test_the_item_is_not_a_dataclass() -> None:
	"""A dataclass resolves its annotations through
	`sys.modules[cls.__module__]` under `from __future__ import annotations`,
	so a module loaded by `exec_module` on a spec -- which is how the example
	suite loads these -- raises on the class body. Generated code should not
	care how it was imported."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "@dataclass" not in module
	assert "from dataclasses import" not in module
	assert '__slots__ = ("at", "next", "tag", "field", "wire",' in module


def test_the_walk_reads_protoc_output(tmp_path: Path) -> None:
	module = load(tmp_path, TLV, preamble=TLV_PREAMBLE)
	held   = module.S.at(runtime().Message(bytearray(WIRE)), 0, len(WIRE))

	assert held.fields_count == 3
	# Where each item starts, not where its value does.
	assert [item.at for item in held.fields()] == [0, 3, 9]

	item = held.user_id()
	assert item.wire == 0
	assert runtime().varint_get(bytes(held.fields_value(item)), 0, 10)[0] == 150

	item = held.label()
	assert item.wire == 2
	assert bytes(held.fields_value(item)) == b"situ"


def test_a_refused_wire_type_raises(tmp_path: Path) -> None:
	"""`default: error` is a rejection rather than a gap: groups have no
	extent this schema can compute."""
	module = load(tmp_path, TLV, preamble=TLV_PREAMBLE)
	held   = module.S.at(runtime().Message(bytearray([0x0B])), 0, 1)

	with pytest.raises(runtime().ConstraintError) as caught:
		held.fields_first()

	assert "wire type 3 is not one this schema sizes" in str(caught.value)


def test_a_missing_tag_raises(tmp_path: Path) -> None:
	module = load(tmp_path, TLV, preamble=TLV_PREAMBLE)
	held   = module.S.at(runtime().Message(bytearray(WIRE)), 0, len(WIRE))

	with pytest.raises(runtime().BoundsError):
		held.fields_find(9)


def test_the_item_repr_names_its_parts(tmp_path: Path) -> None:
	module = load(tmp_path, TLV, preamble=TLV_PREAMBLE)
	held   = module.S.at(runtime().Message(bytearray(WIRE)), 0, len(WIRE))

	assert repr(held.fields_first()) == (
		"S_fields_item(at=0, next=3, tag=8, field=1, wire=0, "
		"value_at=1, value_len=2)")


# -- indexed regions (section 9.3) ------------------------------------------

INDEXED = ("struct R { u32 id; u16 kind; }"
	"struct V { u16 len; u8 body[len]; }"
	"struct S { u16 n; indexed(offset_type = u16, count = n)"
	" { R fixed[]; } }"
	"struct T { u16 n; indexed(offset_type = u16, count = n)"
	" { V varying[]; } }")

S_BYTES = bytes([0, 3,
	0, 0x12, 0, 0x06, 0, 0x0C,
	0, 0, 0, 0xBB, 0, 2,
	0, 0, 0, 0xCC, 0, 3,
	0, 0, 0, 0xAA, 0, 1])
T_BYTES = bytes([0, 2, 0, 4, 0, 0x0B]) + bytes([0, 5]) + b"hello" \
	+ bytes([0, 2]) + b"hi"


def test_an_indexed_region_gets_its_table_walked() -> None:
	"""It answered REGION in the shared classifier and this backend said "not
	emitted by this backend yet" -- the fallthrough note, for the last
	construct no backend reached into."""
	module = emit(INDEXED)

	assert "def fixed_count(self) -> int:" in module
	assert "def fixed_offset(self, index: int) -> int:" in module
	assert "def fixed_at(self, index: int) -> R:" in module
	assert "not emitted by this backend yet" not in module


def test_an_index_entry_is_read_in_the_region_s_byte_order() -> None:
	module = emit(INDEXED)

	assert 'int.from_bytes(self._span[at:at + 2], "big")' in module


def test_the_index_reaches_elements_in_any_order(tmp_path: Path) -> None:
	"""Offsets deliberately out of order: a walk over an ascending table would
	prove nothing about a construct that exists to reach them in any."""
	module = load(tmp_path, INDEXED)
	held   = module.S.at(runtime().Message(bytearray(S_BYTES)), 0, len(S_BYTES))

	assert held.fixed_count == 3
	assert held.fixed_offset(0) == 0x12
	assert [held.fixed_at(i).id for i in range(3)] == [170, 187, 204]


def test_an_index_over_variable_elements_measures_one(tmp_path: Path) -> None:
	"""The construct exists for elements that are not the same size, so each
	is narrowed to its own extent rather than to the rest of the region."""
	module = load(tmp_path, INDEXED)
	held   = module.T.at(runtime().Message(bytearray(T_BYTES)), 0, len(T_BYTES))

	assert [bytes(held.varying_at(i).body) for i in range(2)] == [b"hello", b"hi"]


def test_an_entry_past_the_end_is_refused(tmp_path: Path) -> None:
	module = load(tmp_path, INDEXED)
	held   = module.S.at(runtime().Message(bytearray(S_BYTES)), 0, len(S_BYTES))

	with pytest.raises(IndexError):
		held.fixed_at(3)


# -- varint fields (section 8.1.1) ------------------------------------------

VARINT = "varint_type v { encoding = leb128; max_bits = 64; minimal; }"


def test_a_varint_field_decodes() -> None:
	"""It classified as NOTHING and this backend emitted nothing at all --
	not an accessor and not a note."""
	module = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "def n(self) -> int:" in module
	assert "def n_len(self) -> int:" in module


def test_a_member_after_a_varint_is_placed_past_it() -> None:
	module = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "self.n_len" in module
	assert "its offset cannot be resolved" not in module


def test_a_zigzag_varint_decodes_signed() -> None:
	module = emit("varint_type z { encoding = leb128; max_bits = 64;"
	              " transform = zigzag; }struct S { z n; }")

	assert "zigzag_decode(raw)" in module


def test_a_varint_reads_the_bytes_after_it(tmp_path: Path) -> None:
	module = load(tmp_path, VARINT + "struct S { u8 kind; v n; u16 after; }")
	held   = module.S.at(
		runtime().Message(bytearray([0x01, 0xAC, 0x02, 0xBE, 0xEF])), 0, 5)

	assert held.n == 300
	assert held.n_len == 2
	assert held.after == 0xBEEF


def test_a_minimal_varint_refuses_a_padded_encoding(tmp_path: Path) -> None:
	module = load(tmp_path, VARINT + "struct S { u8 kind; v n; u16 after; }")
	held   = module.S.at(
		runtime().Message(bytearray([0x01, 0x81, 0x00, 0xBE, 0xEF])), 0, 5)

	with pytest.raises(runtime().ConstraintError) as caught:
		_ = held.n

	assert "`minimal` admits one encoding" in str(caught.value)


def test_a_varint_may_size_an_array(tmp_path: Path) -> None:
	"""A real SQLite cell: `varint payload_size; varint rowid; u8 payload[]`,
	which was refused outright with "no fields are in scope at this point"."""
	module = load(tmp_path, VARINT
	              + "struct cell { v payload_size; v rowid; u8 payload[payload_size]; }")
	held   = module.cell.at(
		runtime().Message(bytearray([0x07, 0x01, 0x02, 0x17]) + b"alpha"), 0, 9)

	assert (held.payload_size, held.rowid) == (7, 1)
	assert bytes(held.payload)[2:] == b"alpha"


BE128 = "varint_type sq { encoding = be128; max_bits = 64; max_bytes = 9; }"
CELL  = BE128 + "struct cell { sq payload_size; sq rowid; u8 payload[payload_size]; }"


def test_a_be128_field_uses_the_big_endian_reader() -> None:
	module = emit(BE128 + "struct S { sq n; u16 after; }")

	assert "varint_be_get(data, at, 9, 8)" in module
	assert "\tvarint_be_get," in module


def test_a_be128_field_reads_what_sqlite_wrote(tmp_path: Path) -> None:
	"""2^56-1 is the longest eight-byte value and 2^60-1 needs the ninth,
	whose eight bits and absent continuation flag are the whole of what
	distinguishes this encoding from every other base-128."""
	module = load(tmp_path, CELL)
	rt     = runtime()

	small = module.cell.at(rt.Message(bytearray(
		bytes([0x07, 0x01, 0x02, 0x17]) + b"alpha")), 0, 9)
	assert small.rowid == 1
	assert bytes(small.payload)[2:] == b"alpha"

	eight = bytearray(bytes([0x03] + [0xFF] * 7 + [0x7F, 0x02, 0x0F]) + b"x")
	held  = module.cell.at(rt.Message(eight), 0, len(eight))
	assert held.rowid == (1 << 56) - 1
	assert held.rowid_len == 8

	nine = bytearray(bytes([0x03, 0x87] + [0xFF] * 8 + [0x02, 0x0F]) + b"y")
	held = module.cell.at(rt.Message(nine), 0, len(nine))
	assert held.rowid == (1 << 60) - 1
	assert held.rowid_len == 9


# -- a coded region that ends at a delimiter (13.6) -------------------------

STUFFED = ("codec stuff { kernel = stuffing(worst_case = 4, per = 3,"
	" unit = stream, code = smtp_dot); }\nimpl stuff derived;\n"
	'struct S { coded body(stuff) until "\\r\\n.\\r\\n" '
	"{ u8 content[remaining]; } }")


def test_a_delimited_coded_region_says_the_bytes_are_encoded() -> None:
	"""It emitted the bytes and nothing else, so a Python reader had nothing
	saying they were stuffed."""
	module = emit(STUFFED)

	assert "is `stuff` output, and the bytes" in module
	assert "the order the format specifies" in module


def test_the_note_names_the_symbol_to_call() -> None:
	"""The decode is not emitted here, deliberately: the codec is C's (0017)
	and calling it means loading a shared object from a path this generator
	would have to invent. What the note can do is say which symbol."""
	module = emit(STUFFED)

	assert "No `body_decode`" in module
	assert "situ_stuff_decode" in module


def test_an_unbounded_region_is_told_how_to_size_the_buffer() -> None:
	"""There is no static bound for a `[remaining]` region, so the note names
	the accessor that gives the encoded length instead of a number."""
	module = emit(STUFFED)

	assert "`body_len` gives" in module


# -- an endian marker (section 8.3) -----------------------------------------

MARKED = ("endian_marker order : u16 { little = 0x4949, big = 0x4D4D, }\n"
	"struct hdr [endian = from(order)] { endian_marker order; u16 magic;"
	" u32 offset; }")


def test_a_marker_gets_its_constants_and_predicate() -> None:
	module = emit(MARKED)

	assert "ORDER_LITTLE = 0x4949" in module
	assert "def order_is_little(self) -> bool:" in module
	assert "not emitted by this backend yet" not in module


def test_sys_is_imported_only_where_a_marker_needs_it() -> None:
	"""The host constant is built from `sys.byteorder`, and importing it where
	nothing uses one is the noise the other import gates exist to avoid."""
	assert "import sys" in emit(MARKED)
	assert "import sys" not in emit("struct s { u16 a; }")


def test_both_byte_orders_read_the_same_values(tmp_path: Path) -> None:
	"""Little-endian is the common case and is the one that was wrong: every
	field the marker governs was read big-endian regardless."""
	module = load(tmp_path, MARKED)
	rt     = runtime()

	little = bytearray(b"II" + (42).to_bytes(2, "little") + (8).to_bytes(4, "little"))
	big    = bytearray(b"MM" + (42).to_bytes(2, "big") + (8).to_bytes(4, "big"))

	held = module.hdr.at(rt.Message(little), 0)
	assert (held.magic, held.offset, held.order_is_little) == (42, 8, True)

	held = module.hdr.at(rt.Message(big), 0)
	assert (held.magic, held.offset, held.order_is_little) == (42, 8, False)


def test_a_write_agrees_with_the_read(tmp_path: Path) -> None:
	"""Or a round trip through one view swaps the value."""
	module = load(tmp_path, MARKED)
	buf    = bytearray(b"II" + (42).to_bytes(2, "little") + (8).to_bytes(4, "little"))
	held   = module.hdr.at(runtime().Message(buf), 0)

	held.offset = 0x12345678
	assert held.offset == 0x12345678
	assert buf[4] == 0x78		# stored little end first


# -- a fixed-width text number (section 8.6.2) ------------------------------

TEXT = "struct reply { decimal u16 code[3]; u8 sep; }"


def test_a_fixed_width_text_number_parses() -> None:
	module = emit(TEXT)

	assert "def code(self) -> int:" in module
	assert "parse_uint(self.code_digits, 10, 999)" in module


def test_the_digits_parse_as_smtp_writes_them(tmp_path: Path) -> None:
	"""Padded, and the leading zero required rather than tolerated."""
	module = load(tmp_path, TEXT)
	rt     = runtime()

	for line, want in ((b"250 ", 250), (b"007 ", 7)):
		held = module.reply.at(rt.Message(bytearray(line)), 0)
		assert held.code == want

	for line in (b"2x0 ", b"25  "):
		held = module.reply.at(rt.Message(bytearray(line)), 0)
		with pytest.raises(rt.ConstraintError):
			_ = held.code


MINIMAL = ('struct s { decimal u16 code until " " max 8 [minimal];'
	' u8 rest[] until "\\r\\n" max 8; }')


def test_the_minimal_check_reads_the_digits(tmp_path: Path) -> None:
	"""`[minimal]` is what makes a text number `Canonical`, and this backend
	handed the predicate the parsed *number*: `bytes(6)` in Python is six zero
	bytes rather than the digit `6`, so the check passed whatever the spelling
	-- and refused the one value whose digits are empty under that conversion,
	`0`. No exception either way, and the other three passed the bytes.

	`test_every_backend_enforces_minimal` was watching, and it asserts that the
	source contains `digits_minimal`. It did. Section 26.34's rule, one layer
	down: the test checked the text, and the check is what runs.
	"""
	module = load(tmp_path, MINIMAL)
	rt     = runtime()

	for line in (b"7 \r\n", b"0 \r\n", b"404 \r\n"):
		module.s.at(rt.Message(bytearray(line)), 0, len(line)).validate()

	for line in (b"007 \r\n", b"04 \r\n"):
		held = module.s.at(rt.Message(bytearray(line)), 0, len(line))
		with pytest.raises(rt.ConstraintError):
			held.validate()


# -- a tag's coverage, dirty bit and finalize (section 14.2) ----------------

TAGGED = ("struct s { u8 hop; authenticated { u16 seq; u8 body[4]; }"
	" tag u8 mac[16]; }")


def test_a_tag_gets_its_bytes_span_and_dirty_bit() -> None:
	module = emit(TAGGED)

	assert "def mac(self) -> memoryview:" in module
	assert "def mac_covered(self) -> tuple[int, int]:" in module
	assert "def mac_is_dirty(self) -> bool:" in module
	assert "def mac_finalize(self) -> None:" in module
	assert "not emitted by this backend yet" not in module


def test_the_dirty_bits_are_named(tmp_path: Path) -> None:
	"""They were literals at the call sites, so a reader comparing
	`mark_dirty(1)` here against `DIRTY_MAC` elsewhere had to work out that
	they were the same bit."""
	module = emit(TAGGED)

	assert "DIRTY_MAC = 0x1" in module
	assert "msg.mark_dirty(self.DIRTY_MAC)" in module


def test_the_span_and_the_bit_behave(tmp_path: Path) -> None:
	module = load(tmp_path, TAGGED)
	rt     = runtime()
	msg    = rt.Message(bytearray(32))
	held   = module.s.at(msg, 0)

	assert held.mac_covered() == (1, 6)	# the authenticated region
	assert len(held.mac) == 16
	assert not held.mac_is_dirty()

	held.set_seq(msg, 0x1234)
	assert held.mac_is_dirty()
	held.mac_finalize()
	assert not held.mac_is_dirty()


# -- the offset cache (decision 0022) ---------------------------------------

# -- an offset the message chooses (26.27) ---------------------------------


def test_a_member_past_the_frame_is_empty_not_short(tmp_path: Path) -> None:
	"""A message that says its payload is a thousand bytes, in seventy of them.

	Python is the one backend where this was never unsafe -- a `memoryview`
	slice past the end is short rather than a read out of bounds -- and a short
	slice read as an integer is still a number nobody wrote. The other three
	answer nothing rather than something wrong, and so does this one now
	(26.27)."""
	module = load(
		tmp_path,
		(ROOT / "example" / "packet" / "packet.situ").read_text(encoding="ascii"),
		preamble="")

	raw = bytearray(70)
	raw[4] = 1			# hdr.version, [must_eq = 1]
	raw[5] = 1			# hdr.type = hello
	raw[6] = 0x03			# hdr.length = 1000, inside [max = 1024]
	raw[7] = 0xe8

	msg  = module.Message(raw)
	view = module.packet(msg, 0, len(raw))

	assert len(view.tag) == 0
	with pytest.raises(module.BoundsError):
		view.validate()

	raw[6] = 0
	raw[7] = 8
	assert len(view.tag) == 16
	view.validate()


# -- framing a run (20.3) ---------------------------------------------------


def test_every_prefix_of_a_real_request_answers_honestly(tmp_path: Path) -> None:
	"""The claim the entry made: an HTTP header block could not be framed.

	`example/http` rather than a schema written for this, because that is what
	the entry named and 26.32's rule is that the worked example is the claim.
	Every prefix of a real request must come back truncated, and every bound
	must be a bound: greater than what is in hand, and never more than the
	message turns out to be. A framer that overshoots stalls waiting for bytes
	that will not come."""
	module = load(
		tmp_path,
		(ROOT / "example" / "http" / "http.situ").read_text(encoding="ascii"),
		preamble="")
	req = (b"GET /index.html HTTP/1.1\r\n"
	       b"Host: example.com\r\n"
	       b"Accept: */*\r\n"
	       b"\r\n")

	for i in range(len(req)):
		with pytest.raises(module.TruncatedError) as raised:
			module.request_head.required(req[:i])
		assert i < raised.value.needed <= len(req)

	assert module.request_head.required(req) == len(req)


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
	module = emit(KV)

	assert "at = advance(at, self.entries_span_from(at), self._len)" in module
	assert "def entries_span_from(self, at: int) -> int:" in module


CHAIN = ('struct line { u8 method[] until " "; u8 target[] until " ";'
	' u8 version[] until "\\r\\n"; }')


def test_the_offset_cache_is_behind_the_flag() -> None:
	assert "resolve_offsets" not in emit(CHAIN)


def test_the_offset_cache_is_a_dict() -> None:
	"""Where this backend departs from the other three: the caller has one
	already, and a class per struct would be ceremony for a mapping Python
	spells inline. The keys are the member names, so a reader of one
	language's generated code recognises the other's."""
	module = emit_materialized(CHAIN)

	assert "def resolve_offsets(self) -> dict[str, int]:" in module
	assert 'found["target"] = at' in module


def test_the_cache_agrees_with_the_per_member_offsets(tmp_path: Path) -> None:
	module = load(tmp_path, CHAIN, materialize=True)
	line   = b"GET /index.html HTTP/1.1\r\n"
	held   = module.line.at(runtime().Message(bytearray(line)), 0, len(line))

	off = held.resolve_offsets()
	assert off == {"target": 4, "version": 16}
	assert off["target"] == held.target_offset
	assert off["version"] == held.version_offset


def test_an_opaque_region_hands_back_its_bytes(tmp_path: Path) -> None:
	module = load(tmp_path, "struct s { u16 n; opaque payload [n]; }")
	buf    = bytearray(bytes([0, 5]) + b"hello" + bytes(9))
	held   = module.s.at(runtime().Message(buf), 0, 16)

	assert bytes(held.payload) == b"hello"


def test_a_member_after_a_sealed_region_is_placed() -> None:
	module = emit("codec seal { granularity = byte; length_preserving;"
	              " seekable; authenticated; invertible; deterministic; }\n"
	              "impl seal extern \"x\";\n"
	              "struct s { u16 n; sealed(seal) { u8 body[n]; }"
	              " tag u8 mac[16]; }")

	assert "def mac_covered" in module
	assert "cannot resolve where the tag sits" not in module


WIDE = "struct w { u8 kind; u16 samples[4]; i32 deltas[2]; }"


def test_an_array_of_wide_scalars_gets_an_indexed_getter(tmp_path: Path) -> None:
	module = load(tmp_path, WIDE)
	buf    = bytearray(32)
	for i in range(4):
		buf[1 + i * 2] = 1
		buf[2 + i * 2] = 0x10 + i
	buf[9:13] = bytes([0xFF, 0xFF, 0xFF, 0xFB])
	held = module.w.at(runtime().Message(buf), 0)

	assert [held.samples(i) for i in range(4)] == [272, 273, 274, 275]
	assert held.deltas(0) == -5		# signed, and sign-extended

	with pytest.raises(IndexError):
		held.samples(4)


# -- the module a caller type-checks (26.35) --------------------------------

#: `python3 -m mypy` rather than a `mypy` on the path: that is how `make check`
#: runs it, and the compiler's own suite does not require it to be installed as
#: a command.
HAS_MYPY = subprocess.run(
	[sys.executable, "-m", "mypy", "--version"],
	capture_output=True).returncode == 0


@pytest.mark.skipif(not HAS_MYPY, reason="no mypy")
def test_every_generated_module_type_checks(tmp_path: Path) -> None:
	"""The annotations are for a caller who runs a type checker over them, and
	nothing ran one. Thirty-one errors in fifteen of the twenty-five modules
	the first time this did: `as_enum` returned `object`, so every enum field
	in the tree was a type error; the `tlv` walk was emitted unannotated, so
	every use of it was an untyped call; `__all__ = []` has no element type.

	One invocation over every schema at once, which is a second or so -- the
	cost of a check that lives in the suite rather than in a habit.

	`--strict` because that is what the compiler holds itself to (`make
	check`), and generated code a caller cannot check as strictly as the
	generator was is an annotation that stops at the boundary.
	"""
	shutil.copy(RUNTIME / "python" / "situ_runtime.py",
	            tmp_path / "situ_runtime.py")

	for schema in SCHEMAS:
		parsed   = parse_text(schema.read_text(encoding="utf-8"))
		resolved = resolve(parsed, solve(parsed))
		(tmp_path / f"{schema.stem}.py").write_text(
			generate_py(parsed, resolved, schema.stem).module, encoding="ascii")

	checked = subprocess.run(
		[sys.executable, "-m", "mypy", "--strict", "--no-pretty",
		 "--no-error-summary", str(tmp_path)],
		capture_output=True, text=True)

	assert checked.returncode == 0, checked.stdout + checked.stderr


def test_an_arm_of_wide_values_is_read_by_index(tmp_path: Path) -> None:
	"""Executed. The arm the discriminant selects hands back values by index;
	the other one raises, which is this backend's spelling of the refusal
	every backend makes.

	No backend emitted either accessor before: a variant arm may be a scalar,
	a byte run or a struct, and a run of values is none of those."""
	module = load(tmp_path, (
		"enum k : u8 { one = 0x11, two = 0x22, default = error }\n"
		"struct s {\n"
		"\tk   which;\n"
		"\tu8  n;\n"
		"\tvariant body switch (which) {\n"
		"\t\tcase k.one: u16  wide[n];\n"
		"\t\tcase k.two: u8   raw[n];\n"
		"\t}\n"
		"}\n"))

	buf = bytearray(8)
	buf[0] = 0x11				# the counted-run arm
	buf[1] = 3				# three claimed, three carried
	buf[2], buf[3] = 0x12, 0x34
	buf[4], buf[5] = 0xAB, 0xCD
	buf[6], buf[7] = 0x00, 0x01

	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.body_wide_count == 3
	assert held.body_wide(0) == 0x1234
	assert held.body_wide(2) == 1
	with pytest.raises(IndexError):
		held.body_wide(3)

	buf[0] = 0x22				# ...and now it is the other arm
	held = module.s.at(module.Message(buf), 0, len(buf))
	with pytest.raises(module.VersionError):
		held.body_wide_count


def test_a_marker_governs_a_member_behind_a_variable_one(
		tmp_path: Path) -> None:
	"""Executed, in both orders. Every marker in the tree governs fields at
	constant offsets, so a marker-conditional read at an offset the message
	decides had never run."""
	module = load(tmp_path, (
		"endian_marker mark : u16 { little = 0x4949, big = 0x4D4D }\n"
		"struct s [endian = from(mark)] {\n"
		"\tendian_marker  mark;\n"
		"\tu8             n;\n"
		"\tu8             pad[n];\n"
		"\tu16            after;\n"
		"}\n"), preamble="target buffer;\n")

	buf = bytearray(b"\x4d\x4d\x02\x00\x00\x12\x34")	# "MM": big
	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.after == 0x1234

	buf[0], buf[1] = 0x49, 0x49				# "II": little
	held = module.s.at(module.Message(buf), 0, len(buf))
	assert held.after == 0x3412


def test_value_bounds_are_exported_as_class_constants() -> None:
	"""`[min]`/`[max]` shared with hand-written callers (26.125)."""
	source = emit("const CAP = 9216;\n"
	              "struct s { u16 mtu [min = 576, max = CAP];"
	              " i8 bias [min = -20]; u16 size [max = 100]; }")
	assert "MTU_VALUE_MIN = 576" in source
	assert "MTU_VALUE_MAX = 9216" in source
	assert "BIAS_VALUE_MIN = -20" in source
	assert "SIZE_VALUE_MAX = 100" in source


def test_value_bounds_agree_with_validate(tmp_path: Path) -> None:
	"""The exported constant and the emitted check are one fact, executably.

	Both come from the same `[max]` today, so this is a canary rather than a
	discovery: if the constant's folding and `validate`'s rendering ever
	diverge -- one changed without the other -- a value at `VALUE_MAX` no
	longer passes or one past it no longer fails, and the drift the constants
	exist to prevent has reached the compiler itself."""
	module = load(tmp_path, "struct s { u16 mtu [min = 576, max = 9216]; }")

	def view(value: int) -> Any:
		buf = value.to_bytes(2, "big")
		return module.s.at(module.Message(buf))

	view(module.s.MTU_VALUE_MAX).validate()
	view(module.s.MTU_VALUE_MIN).validate()
	with pytest.raises(module.ConstraintError):
		view(module.s.MTU_VALUE_MAX + 1).validate()
	with pytest.raises(module.ConstraintError):
		view(module.s.MTU_VALUE_MIN - 1).validate()
