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

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.python import generate as generate_py
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
HOST_CC = shutil.which("gcc") or shutil.which("cc")
LIBSITU = ROOT / "build" / "host" / "runtime" / "libsitu.a"

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_py(schema, resolved, "unit").module


def load(tmp_path: Path, body: str, preamble: str = PREAMBLE) -> ModuleType:
	"""Generate the module, import it, and hand it back."""
	(tmp_path / "unit.py").write_text(emit(body, preamble), encoding="ascii")

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
	assert "msg.mark_dirty(1)" in module


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
	authenticated { h hdr; u8 nonce[12] [nonce]; }
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
