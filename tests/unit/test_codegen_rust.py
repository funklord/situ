"""The Rust backend (section 26.18).

The claim that matters is the same one every backend carries: that it describes
the same bytes as the C. The claim specific to Rust is that section 12.3's
invalidation rule stops being a run-time check and becomes a compile error, so
there is a test that requires the offending program to fail to build.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "rust" / "situ_rt.rs"
RUSTC   = shutil.which("rustc")

FORGE = 'fn main() {\n\tlet buf = [0u8; 64];\n\tlet forged = unit::SSealedGate { bytes: &buf };\n\tlet _ = forged.kind();\n}\n'
COMPOSE = 'fn main() {\n\tlet w = unit::CtrlWord::new(0).with_enable(1).with_mode(5);\n\tassert_eq!(w.raw(), 0x15);\n\tassert_eq!(w.enable(), 1);\n\tassert_eq!(w.mode(), 5);\n\tassert_eq!(w.busy(), 0);\n}\n'
DYNAMIC = 'fn main() {\n\tlet mut buf = [0u8; 32];\n\tbuf[2] = 2;\n\tlet s = unit::S::new(&buf).unwrap();\n\tassert_eq!(s.recs_count(), 2);\n\tassert_eq!(s.recs_offset(), 3);\n\tassert!(s.recs(0).is_ok());\n\tassert!(s.recs(2).is_err());\n}\n'

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"


def emit(body: str, preamble: str = PREAMBLE) -> str:
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_rs(schema, resolved, "unit").module


def build(tmp_path: Path, body: str, main: str = "",
		preamble: str = "") -> subprocess.CompletedProcess[str]:
	"""Generate, lay out a crate, and compile it."""
	src = tmp_path / "src"
	src.mkdir(exist_ok=True)

	# The runtime is `no_std` on its own; as a module of a larger crate the
	# attribute belongs to the crate root, not to it.
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "unit.rs").write_text(emit(body, preamble or PREAMBLE),
	                            encoding="ascii")

	if main:
		(src / "main.rs").write_text(
			"mod situ_rt;\nmod unit;\n" + main, encoding="ascii")
		entry, kind = src / "main.rs", []
	else:
		(src / "lib.rs").write_text("pub mod situ_rt;\npub mod unit;\n",
		                            encoding="ascii")
		entry, kind = src / "lib.rs", ["--crate-type", "lib"]

	assert RUSTC is not None
	return subprocess.run(
		[RUSTC, "--edition", "2021", *kind, str(entry),
		 "-o", str(tmp_path / "out")],
		capture_output=True, text=True, check=False, cwd=tmp_path)


# -- what it emits ----------------------------------------------------------


def test_an_enum_is_repr_and_carries_its_membership_test() -> None:
	module = emit("enum kind : u8 { one = 1, two = 2 }\nstruct s { kind k; }")

	assert "#[repr(u8)]" in module
	assert "pub enum Kind {" in module
	assert "pub fn is_known(raw: u8) -> bool {" in module
	assert "matches!(raw, 1 | 2)" in module


def test_an_enum_field_reads_as_an_option() -> None:
	"""A field may hold a value no member names. Section 8.7 rejects those on
	parse rather than on read, and the type says so."""
	module = emit("enum kind : u8 { one = 1 }\nstruct s { kind k; }")

	assert "pub fn k(&self) -> Option<Kind> {" in module


def test_a_field_named_for_a_keyword_becomes_a_raw_identifier() -> None:
	"""A schema is free to call a field `type`; Rust is not. Raw identifiers
	exist for exactly this, and decision 0013 says the naming is the author's."""
	module = emit("struct s { u8 type; u8 match; }")

	assert "pub fn r#type(&self)" in module
	assert "pub fn r#match(&self)" in module
	# `set_type` is not itself a keyword, so it needs no escape -- and
	# `set_r#type` would not be an identifier at all.
	assert "pub fn set_type(&mut self" in module
	assert "set_r#" not in module


def test_a_byte_array_is_a_slice() -> None:
	"""The length travels with the pointer and cannot be lost."""
	module = emit("struct s { u8 octets[4]; }")

	assert "pub fn octets(&self) -> &[u8] {" in module
	assert "&self.bytes[0..4]" in module


def test_reads_and_writes_are_separate_types() -> None:
	"""So a reader who only parses never holds a `&mut` they do not need."""
	module = emit("struct s { u16 a; }")

	assert "pub struct S<'a> {\n\tbytes: &'a [u8]," in module
	assert "pub struct SMut<'a> {\n\tbytes: &'a mut [u8]," in module


def test_a_covered_write_takes_the_dirty_word() -> None:
	"""It used to be refused outright, which is sound and too narrow: the map
	calls the field writable, and a schema that means something smaller in Rust
	than in C is a schema that means two things."""
	module = emit("struct s { u8 hop; authenticated { u16 seq; } tag u8[16]; }")

	assert "pub fn set_seq(&mut self, dirty: &mut Dirty, value: u16) {" in module
	assert "dirty.mark(Self::DIRTY_TAG);" in module


# -- what it compiles to ----------------------------------------------------


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("body", [
	"struct s { u8 a; u16 b; u32 c; }",
	"struct s [allow_straddle] { u4 a; u4 b; bit c; u13 d; u2 e; }",
	"struct inner { u16 x; }\nstruct s { u8 tag; inner nested; }",
	"enum k : u8 { one = 1 }\nstruct s { k kind; u8 rest[3]; }",
	"struct s { q16_16 gain; bcd4 counter; }",
	"struct s { u8 name[8] [nul_terminated, encoding = utf8]; }",
	"struct s { u8 v [must_eq = 1]; reserved u8 [must_be_zero]; }",
	"struct s { u8 type; u8 match; u8 move; }",
	# A covered write emits two statements where there used to be one, and the
	# first had no semicolon: it was a function's last expression until it
	# stopped being the last thing in the function.
	"struct s { u8 hop; authenticated { u16 seq; } tag u8[16]; }",
	# An invariant, and a struct carrying both kinds of obligation at once.
	"struct s { u16 total; u8 a; u32 b; }\ninvariant s.total == size(s.a) + size(s.b);",
	"struct s { u16 n; u8 a; authenticated { u16 q; } tag u8[4]; }"
	"\ninvariant s.n == size(s.a);",
	# Section 8.6: a delimited byte run, a quoted one, a text length driving
	# an array, and a run of records.
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
])
def test_it_compiles(tmp_path: Path, body: str) -> None:
	result = build(tmp_path, body)

	assert result.returncode == 0, result.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_it_agrees_with_the_layout(tmp_path: Path) -> None:
	"""Every field, against the offsets the map states."""
	body = """enum protocol : u8 { tcp = 6, udp = 17 }
	struct hdr [allow_straddle] {
		u4        version;
		u4        ihl;
		u16       total;
		bit       flag;
		u15       offset;
		protocol  proto;
		u8        ttl;
	}
	"""
	result = build(tmp_path, body, main='''
fn main() {
	let mut buf = [0u8; 7];
	for i in 0..7 { buf[i] = (i * 11 + 3) as u8; }

	let h = unit::Hdr::new(&buf).unwrap();
	assert_eq!(h.version(), 0);
	assert_eq!(h.ihl(), 3);
	assert_eq!(h.total(), 0x0e19);
	assert_eq!(h.ttl(), 69);

	let mut m = unit::HdrMut::new(&mut buf).unwrap();
	m.set_ttl(64);
	assert_eq!(m.as_ref().ttl(), 64);
}
''')
	assert result.returncode == 0, result.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True,
	                     text=True, check=False)
	assert run.returncode == 0, run.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_write_while_a_read_view_lives_does_not_compile(tmp_path: Path) -> None:
	"""Section 12.3, as a compile error rather than a generation counter.

	This is the claim the Rust backend exists for. The C runtime carries a
	generation and checks it at run time in a `SITU_CHECKED` build; here the
	borrow checker refuses the program.
	"""
	result = build(tmp_path, "struct s { u16 a; u16 b; }", main='''
fn main() {
	let mut buf = [0u8; 4];
	let reader = unit::S::new(&buf).unwrap();
	let mut writer = unit::SMut::new(&mut buf).unwrap();
	writer.set_a(1);
	let _ = reader.b();
}
''')
	assert result.returncode != 0, "a stale read compiled and should not"
	assert "borrow" in result.stderr


# -- dynamic layout ---------------------------------------------------------


DYN = "struct h { u8 v; u16 n; }\nstruct r { u32 id; }\nstruct s { h hdr; r recs[hdr.n]; }\n"


def test_a_variable_struct_needs_no_length_parameter() -> None:
	"""A slice carries its own length, so unlike C and C++ nothing has to be
	told where the frame ends."""
	module = emit(DYN)

	assert "pub const SIZE_MIN: usize = 3;" in module
	assert "pub fn new(bytes: &'a [u8]) -> Result<Self>" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_dynamic_offsets_and_counts_work(tmp_path: Path) -> None:
	result = build(tmp_path, DYN, main=DYNAMIC)
	assert result.returncode == 0, result.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True,
	                     text=True, check=False)
	assert run.returncode == 0, run.stderr


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


def test_a_gate_has_a_private_field() -> None:
	"""Rust privacy is module-scoped, so a private field means no code outside
	this module can construct one -- and the open is the only thing that does."""
	module = emit(SEALED)

	assert "pub struct SSealedGate<'a> {\n\tbytes: &'a [u8],\n}" in module
	assert "pub fn open_sealed(&self, verified: bool)" in module
	assert "return Err(Error::Tag);" in module


def test_a_secret_field_gets_no_accessor_inside_the_gate() -> None:
	module = emit(SEALED)

	assert "session_key is [secret]" in module
	assert "pub fn session_key" not in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_forging_a_gate_does_not_compile(tmp_path: Path) -> None:
	"""The claim. If this starts compiling, the backend has lost the guarantee
	that makes it worth having."""
	result = build(tmp_path, SEALED, main=FORGE)

	assert result.returncode != 0, "a gate was built without verifying"
	assert "private" in result.stderr


# -- registers --------------------------------------------------------------


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


def test_a_register_separates_the_word_from_the_bus() -> None:
	"""A word is a copy of the bits; the register is a place. Composing costs
	no transaction and writing costs exactly one."""
	module = emit(REGISTER, preamble=MMIO)

	assert "pub struct CtrlWord(u32);" in module
	assert "pub const fn with_enable(self, value: u32) -> Self {" in module
	assert "write_volatile" in module


def test_the_unsafe_is_marked_where_it_happens() -> None:
	"""Decision 0017 puts situ's unsafe surface at the bus and the codec calls.
	A reader auditing this needs to see it, not find it."""
	module = emit(REGISTER, preamble=MMIO)

	assert "/// # Safety" in module
	assert module.count("// SAFETY:") >= 2


def test_an_access_mode_decides_which_operations_exist() -> None:
	module = emit(REGISTER, preamble=MMIO)

	assert "// No with_busy(): the mode is ro." in module
	assert "// No start(): the mode is wo" in module
	assert "// No with_error(): `w1c` is not an assignment" in module
	assert "pub fn clear_error(&self)" in module
	assert "pub fn trigger_start(&self)" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_register_composition_produces_the_right_bits(tmp_path: Path) -> None:
	result = build(tmp_path, REGISTER, main=COMPOSE, preamble=MMIO)
	assert result.returncode == 0, result.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True,
	                     text=True, check=False)
	assert run.returncode == 0, run.stderr


def test_a_constrained_field_at_a_dynamic_offset_is_validated() -> None:
	"""The C backend always did; this one declared the field unplaceable, so a
	`must_eq` after a variable member went unchecked."""
	module = emit("struct h { u8 v; u16 n; }\n"
	              "struct s { h hdr; u8 opts[hdr.n]; u16 after [must_eq = 7]; }")

	assert "pub fn after(&self) -> u16 {" in module
	assert "!= 7 {" in module
