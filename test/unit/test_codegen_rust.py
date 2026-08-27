"""The Rust backend (section 26.18).

The claim that matters is the same one every backend carries: that it describes
the same bytes as the C. The claim specific to Rust is that section 12.3's
invalidation rule stops being a run-time check and becomes a compile error, so
there is a test that requires the offending program to fail to build.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from situc.codegen.rust import generate as generate_rs
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids

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


def emit_materialized(body: str, preamble: str = PREAMBLE) -> str:
	"""The second accessor family as well (decision 0022)."""
	schema   = parse_text(preamble + body)
	resolved = resolve(schema, solve(schema))
	return generate_rs(schema, resolved, "unit", materialize=True).module


def build(tmp_path: Path, body: str, main: str = "",
		preamble: str | None = None,
		link: str = "", materialize: bool = False
		) -> subprocess.CompletedProcess[str]:
	"""Generate, lay out a crate, and compile it.

	`link` names a directory holding a static archive of the C codec
	implementations, for the one accessor that crosses to them (0017).
	"""
	src = tmp_path / "src"
	src.mkdir(exist_ok=True)

	# The runtime is `no_std` on its own; as a module of a larger crate the
	# attribute belongs to the crate root, not to it.
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	# `preamble=""` for a schema carrying its own target and endianness, which
	# is every worked example; the default is for the fragments written here.
	head = PREAMBLE if preamble is None else preamble
	(src / "unit.rs").write_text(
		emit_materialized(body, head) if materialize else emit(body, head),
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
	# `-D warnings`, because a great deal of CI does and generated code that
	# only compiles without it is generated code that fails for the user. It
	# was absent here, and `unused_parens` in a clamp went unnoticed until a
	# hand-run probe used the flag.
	linkage = ["-L", link, "-l", "static=stuff"] if link else []
	return subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", *kind, str(entry),
		 *linkage, "-o", str(tmp_path / "out")],
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
	# A nested struct with no single size, and a member after it. Every
	# backend got this wrong in a different way and none of them compiled or
	# ran it before.
	"struct inner { u8 n; u8 body[n]; }\nstruct outer { inner a; u16 tail; }",
	# Section 8.6.6: a run ending on a condition, whose element is sized by
	# arithmetic over one of its own fields.
	"struct e { u8 next; u8 len; u8 d[(len + 1) * 8 - 2]; }\n"
	"struct s { e chain[] while (next == 43) max 8; u8 rest[remaining]; }",
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


SCANNED = ('struct s { u8 name[] until "\\r\\n" max 8; u16 tag; }')

WRITE_SCANNED = ('fn main() {\n'
	'\tlet mut buf = *b"ab\\r\\n\\x00\\x00padding";\n'
	'\t{\n'
	'\t\tlet mut view = unit::SMut::new(&mut buf).unwrap();\n'
	'\t\tview.set_tag(0xBEEF);\n'
	'\t}\n'
	'\tlet view = unit::S::new(&buf).unwrap();\n'
	'\tassert_eq!(view.tag(), 0xBEEF);\n'
	'\tassert_eq!(&buf[4..6], &[0xBE, 0xEF]);\n'
	'}\n')


def test_a_scalar_at_a_dynamic_offset_gets_a_setter() -> None:
	"""It got nothing at all here -- no setter and no note -- while the map
	called it `mutate = InPlaceFixed` and the other three backends wrote it.
	A field readable in four languages and writable in three is the schema
	meaning something narrower in one of them (26.35).

	Found by making the differential drivers write, which they never had."""
	module = emit(SCANNED)

	assert "pub fn set_tag(&mut self, value: u16)" in module
	assert "Does nothing where the member does not fit" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_that_setter_writes_where_the_scan_ends(tmp_path: Path) -> None:
	"""And writes at the offset the *data* put the member at: `name` ends at
	the CRLF, so the tag is at 4 rather than at any constant this backend
	could have used."""
	result = build(tmp_path, SCANNED, main=WRITE_SCANNED)
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
	authenticated { h hdr; u8 nonce[12]; }
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


def test_an_unmeasurable_nested_member_gets_no_accessor() -> None:
	"""A struct whose extent nothing can compute gets no accessor.

`label` ends in an `opaque` default arm, which swallows whatever is left, so
one is exactly as long as the view it was handed; `name` is a run of those, so
its own length is unknown in turn; and `question.qname` is one of *those*. Each backend emitted
the accessor anyway and reached for an extent it had declined to emit --
rustc caught it, which is the only mercy in it.

Nothing after such a member can be placed either, which is why `qtype` goes
with it: its offset is the extent nobody can compute.
"""
	source = emit(UNMEASURABLE)

	# The member names still appear -- in the note saying why each was
	# declined, which is the whole point of emitting one.
	assert "fn qname_extent" not in source
	assert "fn qtype" not in source
	assert "question.qname: one `name` has no" in source
	assert "extent this backend can compute" in source


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_and_the_module_compiles(tmp_path: Path) -> None:
	"""It did not: no method named `extent` on `Name`."""
	built = build(tmp_path, UNMEASURABLE)
	assert built.returncode == 0, built.stderr


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


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_compressed_name_walks(tmp_path: Path) -> None:
	"""The four shapes a DNS name comes in, against a hand-checked count and
	extent -- the same table the C suite walks, because agreeing with the
	other backends is the property under test."""
	built = build(tmp_path, DNS_LABEL, main="""
fn walk(b: &[u8], labels: usize, extent: usize, ok: bool) -> bool {
	let held = match unit::Name::new(b) { Ok(v) => v, Err(_) => return false };

	if held.labels_count() != labels || held.labels_span() != extent {
		return false;
	}
	for i in 0..labels {
		match held.labels(i) {
			Ok(one) => if one.validate().is_ok() != ok { return false },
			Err(_)  => return false,
		}
	}
	true
}

fn main() {
	let plain  = [3,b'w',b'w',b'w', 7,b'e',b'x',b'a',b'm',b'p',b'l',b'e',
	              3,b'c',b'o',b'm', 0];
	let whole  = [0xC0u8, 0x0C];
	let suffix = [3,b'w',b'w',b'w', 0xC0, 0x0C];
	let bad    = [0x40u8, 0x00];

	assert!(walk(&plain,  4, 17, true));
	assert!(walk(&whole,  1,  2, true));
	assert!(walk(&suffix, 2,  6, true));
	assert!(walk(&bad,    1,  1, false));
}
""")

	assert built.returncode == 0, built.stderr

	# Built *and run*. `assert!` in a binary nobody executes asserts nothing,
	# which is how the extent bug survived its first test.
	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- a length the message declares, and the frame it has to fit -------------

OVERLONG = "struct s { u8 n; u16 want; u8 body[want]; u8 tail[remaining]; }"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_declared_length_is_clamped_and_reported(tmp_path: Path) -> None:
	"""`&bytes[at..at + declared]` panics rather than reading out of bounds,
	which is memory-safe and is still a denial of service in a `no_std` build
	where a panic aborts. A message chooses that length."""
	built = build(tmp_path, OVERLONG, main="""
fn main() {
	let mut buf = [0u8; 16];
	buf[1] = 0x03; buf[2] = 0xE8;		// says 1000 bytes of body

	let held = unit::S::new(&buf).unwrap();

	assert_eq!(held.body().len(), 13);
	assert!(held.validate().is_err());
}
""")
	assert built.returncode == 0, built.stderr

	let_run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert let_run.returncode == 0, let_run.stderr


# -- a member the data positions (section 9.8) ------------------------------

LOCATED = "struct s { u32 off; u16 n; u8 body[n] at off; u16 after; }"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_located_member_borrows_the_message_not_the_frame(tmp_path: Path) -> None:
	"""A generated struct holds the frame slice, and a located member is not
	in it -- so the returned slice borrows the message, which is a lifetime
	the signature has to say out loud."""
	built = build(tmp_path, LOCATED, main="""
fn main() {
	let mut buf = [0u8; 28];
	buf[4 + 3] = 16;
	buf[4 + 5] = 4;
	buf[4 + 6] = 0xBE; buf[4 + 7] = 0xEF;
	buf[16..20].copy_from_slice(b"DATA");

	let held = unit::S::new(&buf[4..28]).unwrap();
	assert_eq!(held.after(), 0xBEEF);
	assert_eq!(held.body(&buf).unwrap(), b"DATA");

	let mut bad = buf;
	bad[4 + 3] = 200;
	let held = unit::S::new(&bad[4..28]).unwrap();
	assert!(held.body(&bad).is_err());
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- framing a stream -------------------------------------------------------

STREAM_FRAMED = "struct s { u8 version; u16 n; u8 body[n]; u16 trailer; }"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_required_never_reads_a_length_that_has_not_arrived(tmp_path: Path) -> None:
	"""`Framing` rather than `Result<usize>`: both arms carry a number and
	they mean different things -- one is the length, the other a lower bound
	on it. A caller framing a stream needs both."""
	built = build(tmp_path, STREAM_FRAMED, main="""
use situ_rt::Framing;

fn main() {
	let whole = [1u8, 0, 4, b'D', b'A', b'T', b'A', 0xBE, 0xEF];

	for have in 0..whole.len() {
		match unit::S::required(&whole[..have]) {
			Framing::Complete(_) => panic!("complete at {}", have),
			Framing::Need(n) => {
				assert!(n > have && n <= whole.len());
				// `n` not wholly here: the minimum is all that can be said.
				if have < 3 { assert_eq!(n, unit::S::SIZE_MIN); }
				// Past the gate: `n` has been read and the answer is exact.
				if have >= unit::S::SIZE_MIN { assert_eq!(n, whole.len()); }
			}
		}
	}

	assert_eq!(unit::S::required(&whole), Framing::Complete(whole.len()));

	let mut longer = whole.to_vec();
	longer.extend_from_slice(b"next");
	assert_eq!(unit::S::required(&longer), Framing::Complete(whole.len()));
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


TWO_LENGTHS = "struct s { u16 n; u8 a[n]; u16 m; u8 b[m]; }"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_length_behind_a_variable_member_resolves(tmp_path: Path) -> None:
	"""`m` sits at `2 + n`, so there is no constant to read it at -- and this
	backend read length drivers only at constant offsets, so `b` was dropped
	with a note and `required` declined along with it. Its own accessor knows
	where it is. C++, Python and Rust all had this; C did not."""
	built = build(tmp_path, TWO_LENGTHS, main="""
use situ_rt::Framing;

fn main() {
	/* n = 3 "abc", m = 2 "xy": 2 + 3 + 2 + 2 = 9. */
	let buf = [0u8, 3, b'a', b'b', b'c', 0, 2, b'x', b'y'];
	let held = unit::S::new(&buf).unwrap();

	assert_eq!(held.b_offset(), 7);
	assert_eq!(held.b(), b"xy");
	assert_eq!(unit::S::required(&buf), Framing::Complete(buf.len()));
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- a base the message puts past the end -----------------------------------

OVERREACHING = 'struct s { u16 n; u8 a[n]; u8 b[] until ";"; }'


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_scan_base_past_the_frame_reads_nothing(tmp_path: Path) -> None:
	"""`n` is a `u16` the message chooses, so `b`'s base can sit past the end
	of a ten-byte frame. The scan limit was `len - base`, which underflows to
	about four billion, and the scan then searched that much memory.

	C++ read out of bounds -- an AddressSanitizer SEGV. Rust panicked on the
	slice before any limit applied. Python returned a wrong number. C had been
	saturating here since the `[remaining]` fix and the other three were not.
	All four now answer as C does: an empty scan."""
	built = build(tmp_path, OVERREACHING, main="""
fn main() {
	let mut buf = [0u8; 10];
	buf[0] = 0xFF;
	buf[1] = 0xFF;			// n = 65535 in a ten-byte frame

	let held = unit::S::new(&buf).unwrap();
	// The offset stops at the frame rather than running past it: every term
	// of it is a length the message chose (26.27).
	assert_eq!(held.b_offset(), 10);
	assert_eq!(held.b_len(), 0);
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


def test_an_offset_sum_keeps_a_running_total() -> None:
	"""Statements rather than one expression, because the running total has
	to be a variable. `0 + a_span + b_span` re-derives each term's base by
	rescanning everything before it, so the sum costs far more than the terms
	in it -- an eight-member record measured 1590ms, then 53ms."""
	module = emit('struct s { u8 a[] until ";"; u8 b[] until ";"; u8 c[] until ";"; }')

	assert "let mut at = 0usize;" in module
	assert "at = situ_rt::advance(at, self.a_span_from(at),"\
		" self.bytes.len());" in module
	assert "0 + self.a_span()" not in module


# -- the second accessor family (decision 0022) -----------------------------

INDEXED_RUN = """
struct l { u2 f; u6 r; u8 t[r]; }
struct n { l ls[] while (f == 0 && r != 0) max 128; }
"""


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_run_index_agrees_with_the_walk(tmp_path: Path) -> None:
	"""C's shape rather than Python's list: this backend is `no_std` and has
	no allocator, so `max N` bounds an array the caller owns. Measured at 6ms
	against under one."""
	schema   = parse_text(PREAMBLE + INDEXED_RUN)
	resolved = resolve(schema, solve(schema))
	module   = generate_rs(schema, resolved, "unit", materialize=True).module

	src = tmp_path / "src"
	src.mkdir(exist_ok=True)
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "unit.rs").write_text(module, encoding="ascii")
	(src / "main.rs").write_text("""
mod situ_rt;
mod unit;

fn main() {
	let mut buf = Vec::new();
	for _ in 0..40 { buf.push(1u8); buf.push(b'a'); }
	buf.push(0u8);

	let v = unit::N::new(&buf).unwrap();
	let idx = v.ls_indexed();

	assert_eq!(idx.count, v.ls_count());
	assert_eq!(idx.count, 41);
	for i in 0..idx.count {
		let a = v.ls(i).unwrap();
		let b = v.ls_at(&idx, i).unwrap();
		assert_eq!((a.f(), a.r()), (b.f(), b.r()));
	}
	assert!(v.ls_at(&idx, idx.count).is_err());
}
""", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", str(src / "main.rs"),
		 "-o", str(tmp_path / "out")],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


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


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_each_arm_refuses_when_it_is_not_the_one_present(tmp_path: Path) -> None:
	"""A `Result`, because the arm may not be the one there. `Error::Version`
	is what an unrecognised discriminant gets."""
	built = build(tmp_path, ARMS, main="""
fn main() {
	let text = unit::Label::new(&[3, b'w', b'w', b'w']).unwrap();
	assert_eq!(text.body_text().unwrap(), b"www");
	assert!(text.body_pointer_low().is_err());

	let ptr = unit::Label::new(&[0xC0, 0x0C]).unwrap();
	assert_eq!(ptr.body_pointer_low().unwrap(), 0x0C);
	assert!(ptr.body_text().is_err());
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


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


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_an_enum_discriminant_compiles_and_selects(tmp_path: Path) -> None:
	"""An enum discriminant, which no test in this file had.

	Section 9.6's own example uses one -- `case msg_type.hello:` -- and this
	backend did not compile such a schema at all: the extent chain, the
	`default: error` check and the arm guards all compared the enum getter
	against a number, and that getter hands back `Option<K>`, which `as usize` cannot cast. Three separate
	constructs, one missing test."""
	built = build(tmp_path, ENUM_ARMS, main="""
fn main() {
	let a = [1u8, 0xBE, 0xEF, 0, 0, 0, 0, 0];
	let s = unit::S::new(&a).unwrap();
	assert_eq!(s.v_p().unwrap().x(), 0xBEEF);
	assert!(s.v_q().is_err());

	let b = [2u8, 0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 0];
	let s = unit::S::new(&b).unwrap();
	assert_eq!(s.v_q().unwrap().y(), 0xDEAD_BEEF);
	assert!(s.v_p().is_err());
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- every example, compiled ------------------------------------------------


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_compiles(schema: Path, tmp_path: Path) -> None:
	"""The C suite has had this since phase 4 and this one had not, which is
	how `packet` came to import a name it never used and `smtp` to call scan
	helpers this backend does not emit. Generating is not compiling.

	Every schema rather than every example, for the reason the C++ suite
	records: the file carrying the constructs no worked example has is the one
	a check globbing `example/` never reads (26.31)."""
	src = tmp_path / "src"
	src.mkdir(exist_ok=True)
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")

	parsed   = parse_text(schema.read_text(encoding="utf-8"))
	resolved = resolve(parsed, solve(parsed))
	module   = generate_rs(parsed, resolved, schema.stem).module

	(src / "unit.rs").write_text(module, encoding="ascii")
	(src / "lib.rs").write_text("pub mod situ_rt;\npub mod unit;\n",
	                            encoding="ascii")

	assert RUSTC is not None
	result = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type",
		 "lib", str(src / "lib.rs"), "-o", str(tmp_path / "out")],
		capture_output=True, text=True, cwd=tmp_path)
	assert result.returncode == 0, f"{schema.stem}: {result.stderr}"


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_generates(schema: Path) -> None:
	"""The half that runs without rustc: generating is not compiling, but a
	generator that raises does not get as far as either."""
	parsed   = parse_text(schema.read_text(encoding="utf-8"))
	resolved = resolve(parsed, solve(parsed))

	assert generate_rs(parsed, resolved, schema.stem).module


# -- a coded region that ends at a delimiter (13.6) -------------------------

CODED = 'codec dot_stuffing {\n\tkernel = stuffing(worst_case = 4, per = 3, unit = stream, code = smtp_dot);\n}\nimpl dot_stuffing derived;\nstruct data_block {\n\tcoded body(dot_stuffing) until "\\r\\n.\\r\\n" {\n\t\tu8 content[remaining];\n\t}\n}\n'


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
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
	built = build(tmp_path, CODED, main="""
fn main() {
	let raw = b"Hello\\r\\n..dotted\\r\\n\\r\\n.\\r\\nX";
	let v = unit::DataBlock::new(raw).unwrap();

	assert_eq!(v.body_len(), 17);
	assert_eq!(v.body_span(), 22);
	assert!(v.body_terminated());
}
""")
	assert built.returncode == 0, built.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


# -- a coded region's bytes, and its transform (13.5) -----------------------

CODED_PRE  = 'target buffer;\nendian big;\nbit_order msb_first;\ncodec halve { kernel = table(input_bits = 1, output_bits = 2, code = manchester_802_3); }\nimpl halve derived;\n'
CODED_BODY = 'struct S { coded body(halve) { u8 raw[4]; } }'


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_coded_region_decodes_through_the_c_codec(tmp_path: Path) -> None:
	"""The codec is C's (decision 0017), so this goes through `extern "C"`
	and therefore through `unsafe` -- and section 26.18 says where that
	belongs: at the call site with a note saying what is being promised,
	rather than buried in a helper."""
	schema   = parse_text(CODED_PRE + CODED_BODY)
	resolved = resolve(schema, solve(schema))
	module   = generate_rs(schema, resolved, "unit").module

	assert 'extern "C" {' in module
	assert "fn situ_halve_decode(" in module
	assert "// SAFETY: the codec is the C implementation" in module

	src = tmp_path / "src"
	src.mkdir(exist_ok=True)
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "unit.rs").write_text(module, encoding="ascii")

	from situc.codegen.c import derived, generate as generate_c
	(tmp_path / "unit.h").write_text(
		generate_c(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "unit_derived.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")

	cc = shutil.which("gcc") or shutil.which("cc")
	if cc is None:
		pytest.skip("no C compiler to build the codec")
	built = subprocess.run(
		[cc, "-c", "-O2", f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "unit_derived.c"), "-o", str(tmp_path / "derived.o")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	(src / "main.rs").write_text("""
mod situ_rt;
mod unit;

extern "C" { fn situ_halve_encode(i: *const u8, b: u32, o: *mut u8) -> u32; }

fn main() {
	let plain = [0xA5u8, 0x3C, 0xF0, 0x0F];
	let mut buf = [0u8; 8];
	unsafe { situ_halve_encode(plain.as_ptr(), 32, buf.as_mut_ptr()); }

	let v = unit::S::new(&buf).unwrap();
	assert_eq!(v.body().len(), 8);

	let mut out = [0u8; unit::S::BODY_DECODED_MAX];
	assert_eq!(v.body_decode(&mut out).unwrap(), 4);
	assert_eq!(&out[..4], &plain);

	// A byte short is refused rather than half-filled.
	let mut small = [0u8; 3];
	assert!(v.body_decode(&mut small).is_err());
}
""", encoding="ascii")

	assert RUSTC is not None
	compiled = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", str(src / "main.rs"),
		 "-o", str(tmp_path / "out"),
		 "-C", f"link-arg={tmp_path / 'derived.o'}"],
		capture_output=True, text=True, cwd=tmp_path)
	assert compiled.returncode == 0, compiled.stderr

	run = subprocess.run([str(tmp_path / "out")], capture_output=True, text=True)
	assert run.returncode == 0, run.stderr


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
	"""It answered REGION in the shared classifier and this backend said "not
	in the static subset yet", which reads as a missing feature rather than the
	fallthrough it was."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "pub fn fields_first(&self)" in module
	assert "pub fn fields_next(&self, item: &SFieldsItem)" in module
	assert "pub fn fields_count(&self)" in module
	assert "not in the static subset yet" not in module


def test_the_item_is_a_module_scope_struct() -> None:
	"""Rust has no struct declaration inside an `impl`, so it goes beside the
	run-index types for the same reason."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "pub struct SFieldsItem {" in module
	assert "pub field: u32,\t// tag >> 3" in module
	assert "pub wire: u32,\t// tag & 0x7" in module


def test_the_walk_returns_the_item_rather_than_filling_one_in() -> None:
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "pub fn fields_read(&self, at: usize) -> Result<SFieldsItem>" in module


def test_the_value_is_a_slice() -> None:
	"""The borrow the other three backends cannot express."""
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "pub fn fields_value(&self, item: &SFieldsItem) -> &[u8]" in module


def test_each_known_tag_gets_an_accessor() -> None:
	module = emit(TLV, preamble=TLV_PREAMBLE)

	assert "pub fn user_id(&self) -> Result<SFieldsItem>" in module
	assert "pub fn label(&self) -> Result<SFieldsItem>" in module
	assert "self.fields_find(1)" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_generated_walk_reads_protoc_output(tmp_path: Path) -> None:
	"""The same vectors the C suite uses, which came out of protoc.

	`item.at` is asserted because it was wrong first time round: `used` is
	shadowed inside the dispatch arms and `at` moves past a length prefix, so
	deriving the item's start by subtraction gave the value's start for one
	wire type and the item's for the others."""
	result = build(tmp_path, TLV, preamble=TLV_PREAMBLE, main="""
const WIRE: &[u8] = &[
	0x08, 0x96, 0x01,
	0x12, 0x04, b's', b'i', b't', b'u',
	0x1d, 0x00, 0x00, 0xC0, 0x3F,
];

fn main() {
	let msg = unit::S::new(WIRE).unwrap();
	assert_eq!(msg.fields_count(), 3);

	let item = msg.user_id().unwrap();
	assert_eq!(item.wire, 0);
	assert_eq!(situ_rt::varint_get(WIRE, item.value_at, 10).unwrap().0, 150);

	let item = msg.label().unwrap();
	assert_eq!(item.wire, 2);
	assert_eq!(msg.fields_value(&item), b"situ");

	// Where each item starts, not where its value does.
	let first = msg.fields_first().unwrap();
	let second = msg.fields_next(&first).unwrap();
	let third = msg.fields_next(&second).unwrap();
	assert_eq!((first.at, second.at, third.at), (0, 3, 9));

	// A wire type the schema refuses.
	let group = unit::S::new(&[0x0B]).unwrap();
	assert!(matches!(group.fields_first(), Err(situ_rt::Error::Constraint)));
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- indexed regions (section 9.3) ------------------------------------------

INDEXED = ("struct R { u32 id; u16 kind; }"
	"struct V { u16 len; u8 body[len]; }"
	"struct S { u16 n; indexed(offset_type = u16, count = n)"
	" { R fixed[]; } }"
	"struct T { u16 n; indexed(offset_type = u16, count = n)"
	" { V varying[]; } }")


def test_an_indexed_region_gets_its_table_walked() -> None:
	module = emit(INDEXED)

	assert "pub fn fixed_count(&self) -> usize" in module
	assert "pub fn fixed_offset(&self, index: usize) -> Result<usize>" in module
	assert "pub fn fixed_at(&self, index: usize) -> Result<R<'_>>" in module
	assert "not in the static subset yet" not in module


def test_an_index_entry_is_read_in_the_region_s_byte_order() -> None:
	module = emit(INDEXED)

	assert "situ_rt::read_be(self.bytes, at, 2)" in module


def test_an_index_over_variable_elements_measures_one() -> None:
	module = emit(INDEXED)

	assert "let probe = V { bytes: &self.bytes[start..] };" in module
	assert "let size  = probe.extent();" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_index_reaches_elements_in_any_order(tmp_path: Path) -> None:
	"""Offsets deliberately out of order: a walk over an ascending table would
	prove nothing about a construct that exists to reach them in any."""
	result = build(tmp_path, INDEXED, main="""
const S_BYTES: &[u8] = &[
	0x00, 0x03,
	0x00, 0x12, 0x00, 0x06, 0x00, 0x0C,
	0x00, 0x00, 0x00, 0xBB, 0x00, 0x02,
	0x00, 0x00, 0x00, 0xCC, 0x00, 0x03,
	0x00, 0x00, 0x00, 0xAA, 0x00, 0x01,
];
const T_BYTES: &[u8] = &[
	0x00, 0x02,
	0x00, 0x04, 0x00, 0x0B,
	0x00, 0x05, b'h', b'e', b'l', b'l', b'o',
	0x00, 0x02, b'h', b'i',
];

fn main() {
	let s = unit::S::new(S_BYTES).unwrap();
	assert_eq!(s.fixed_count(), 3);
	assert_eq!(s.fixed_offset(0).unwrap(), 0x12);
	assert_eq!(s.fixed_at(0).unwrap().id(), 170);
	assert_eq!(s.fixed_at(1).unwrap().id(), 187);
	assert_eq!(s.fixed_at(2).unwrap().id(), 204);
	assert!(s.fixed_at(3).is_err());

	// Each element is narrowed to its own extent, not to the rest.
	let t = unit::T::new(T_BYTES).unwrap();
	assert_eq!(t.varying_at(0).unwrap().body(), b"hello");
	assert_eq!(t.varying_at(1).unwrap().body(), b"hi");
	assert!(t.varying_at(2).is_err());
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- varint fields (section 8.1.1) ------------------------------------------

VARINT = "varint_type v { encoding = leb128; max_bits = 64; minimal; }"


def test_a_varint_field_decodes() -> None:
	"""It classified as NOTHING and this backend emitted nothing at all --
	not an accessor and not a note."""
	module = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "pub fn n(&self) -> Result<u64>" in module
	assert "pub fn n_len(&self) -> usize" in module


def test_a_member_after_a_varint_is_placed_past_it() -> None:
	module = emit(VARINT + "struct S { u8 kind; v n; u16 after; }")

	assert "self.n_len()" in module
	assert "its offset cannot be resolved" not in module


def test_a_varint_may_size_an_array() -> None:
	module = emit(VARINT + "struct S { v n; u8 payload[n]; }")

	assert "self.n_value() as usize" in module


def test_a_minimal_varint_refuses_a_padded_encoding() -> None:
	module = emit(VARINT + "struct S { v n; }")

	assert "if used != situ_rt::varint_len(raw) {" in module
	assert "return Err(Error::Constraint);" in module


def test_a_zigzag_varint_decodes_signed() -> None:
	module = emit("varint_type z { encoding = leb128; max_bits = 64;"
	              " transform = zigzag; }struct S { z n; }")

	assert "pub fn n(&self) -> Result<i64>" in module
	assert "situ_rt::zigzag_decode(raw)" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_varint_reads_the_bytes_after_it(tmp_path: Path) -> None:
	result = build(tmp_path, VARINT + "struct S { u8 kind; v n; u16 after; }",
	               main="""
fn main() {
	// kind = 1, n = 300 (leb128 AC 02), after = 0xBEEF
	let buf: &[u8] = &[0x01, 0xAC, 0x02, 0xBE, 0xEF];
	let s = unit::S::new(buf).unwrap();

	assert_eq!(s.n().unwrap(), 300);
	assert_eq!(s.n_len(), 2);
	assert_eq!(s.after(), 0xBEEF);

	// A padded encoding of 1, which `minimal` refuses.
	let padded: &[u8] = &[0x01, 0x81, 0x00, 0xBE, 0xEF];
	let p = unit::S::new(padded).unwrap();
	assert!(matches!(p.n(), Err(situ_rt::Error::Constraint)));
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


BE128 = "varint_type sq { encoding = be128; max_bits = 64; max_bytes = 9; }"


def test_a_be128_field_uses_the_big_endian_reader() -> None:
	module = emit(BE128 + "struct S { sq n; u16 after; }")

	assert "situ_rt::varint_be_get(self.bytes, at, 9, 8)" in module


def test_a_member_after_a_be128_is_placed_past_it() -> None:
	module = emit(BE128 + "struct S { sq n; u16 after; }")

	assert "self.n_len()" in module
	assert "its offset cannot be resolved" not in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_be128_field_reads_what_sqlite_wrote(tmp_path: Path) -> None:
	"""2^56-1 is the longest eight-byte value and 2^60-1 needs the ninth,
	whose eight bits and absent continuation flag are the whole of what
	distinguishes this encoding from every other base-128."""
	result = build(tmp_path,
	               BE128 + "struct cell { sq payload_size; sq rowid;"
	               " u8 payload[payload_size]; }",
	               main="""
fn main() {
	// sqlite3, rowid 1
	let small: &[u8] = &[0x07, 0x01, 0x02, 0x17, b'a', b'l', b'p', b'h', b'a'];
	let c = unit::Cell::new(small).unwrap();
	assert_eq!(c.rowid().unwrap(), 1);
	assert_eq!(&c.payload()[2..], b"alpha");

	// sqlite3, rowid 2^56-1: eight bytes
	let eight: &[u8] = &[0x03, 0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0x7F, 0x02,0x0F, b'x'];
	let c = unit::Cell::new(eight).unwrap();
	assert_eq!(c.rowid().unwrap(), 72057594037927935);
	assert_eq!(c.rowid_len(), 8);

	// sqlite3, rowid 2^60-1: nine
	let nine: &[u8] = &[0x03, 0x87,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF, 0x02,0x0F, b'y'];
	let c = unit::Cell::new(nine).unwrap();
	assert_eq!(c.rowid().unwrap(), 1152921504606846975);
	assert_eq!(c.rowid_len(), 9);
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- a coded region that ends at a delimiter (13.6) -------------------------

STUFFED = ("codec stuff { kernel = stuffing(worst_case = 4, per = 3,"
	" unit = stream, code = smtp_dot); }\nimpl stuff derived;\n"
	'struct S { coded body(stuff) until "\\r\\n.\\r\\n" '
	"{ u8 content[remaining]; } }")


def test_a_delimited_coded_region_says_the_bytes_are_encoded() -> None:
	"""It emitted the bytes and nothing else, so a reader had no way to know
	they were not the value."""
	module = emit(STUFFED)

	assert "is `stuff` output, and the" in module
	assert "The scan runs on the encoded bytes" in module


def test_a_stuffing_kernel_gets_a_decode_accessor() -> None:
	module = emit(STUFFED)

	assert "pub fn body_decode(&self, out: &mut [u8]) -> Result<usize>" in module
	assert "let encoded = self.body_len();" in module


def test_a_byte_kernel_is_handed_bytes() -> None:
	"""`unit` decides. A byte count into a bit loop decodes an eighth of the
	region and returns confidently."""
	module = emit(STUFFED)

	assert "(encoded) as u32," in module
	assert "len: u32, out: *mut u8" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_decode_unstuffs_a_real_body(tmp_path: Path) -> None:
	"""RFC 5321 section 4.5.2: the receiver removes one period from a line
	that starts with one, and the terminator is the only bare dot."""
	from situc.codegen.c import derived
	from situc.codegen.c import generate as generate_c

	schema   = parse_text(PREAMBLE + STUFFED)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.h").write_text(
		generate_c(schema, resolved, "unit").header, encoding="ascii")
	(tmp_path / "impl.c").write_text(
		derived.generate(schema, "unit"), encoding="ascii")

	object_file = tmp_path / "impl.o"
	compiled = subprocess.run(
		["cc", "-std=c11", "-O1", "-c", f"-I{RUNTIME.parent.parent / 'c'}",
		 f"-I{tmp_path}", str(tmp_path / "impl.c"), "-o", str(object_file)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	archive = tmp_path / "libstuff.a"
	subprocess.run(["ar", "rcs", str(archive), str(object_file)], check=True)

	result = build(tmp_path, STUFFED, main="""
const WIRE: &[u8] = b"a\\r\\n..b\\r\\n\\r\\n.\\r\\n";
const WANT: &[u8] = b"a\\r\\n.b\\r\\n";

fn main() {
	let s = unit::S::new(WIRE).unwrap();
	let mut out = [0u8; 64];
	let n = s.body_decode(&mut out).unwrap();
	assert_eq!(&out[..n], WANT);
	assert!(s.body_decode(&mut out[..1]).is_err());
}
""", link=str(tmp_path))
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- an endian marker (section 8.3) -----------------------------------------

MARKED = ("endian_marker order : u16 { little = 0x4949, big = 0x4D4D, }\n"
	"struct hdr [endian = from(order)] { endian_marker order; u16 magic;"
	" u32 offset; }")


def test_a_marker_gets_its_constants_and_predicate() -> None:
	module = emit(MARKED)

	assert "ORDER_LITTLE: u16 = 0x4949;" in module
	assert "pub fn order_is_little(&self) -> bool" in module
	assert "not in the static subset yet" not in module


def test_a_governed_field_branches_on_the_marker() -> None:
	"""The map said `ConditionallyConverted(order)` the whole time and the
	read was unconditional, so a little-endian frame came back byte-swapped."""
	module = emit(MARKED)

	assert "if self.order_is_little() { situ_rt::read_le" in module


def test_the_setter_reaches_the_marker_through_as_ref() -> None:
	"""The setters are on the `Mut` struct and the accessor is on the read
	one, which is this backend's split everywhere rather than anything about
	markers."""
	module = emit(MARKED)

	assert "self.as_ref().order_is_little()" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_both_byte_orders_read_the_same_values(tmp_path: Path) -> None:
	result = build(tmp_path, MARKED, main="""
fn main() {
	for raw in [[b'I', b'I', 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00],
	            [b'M', b'M', 0x00, 0x2A, 0x00, 0x00, 0x00, 0x08]] {
		let h = unit::Hdr::new(&raw).unwrap();
		assert_eq!((h.magic(), h.offset()), (42, 8));
	}

	// A write has to agree with the read.
	let mut buf = [b'I', b'I', 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00];
	{
		let mut m = unit::HdrMut::new(&mut buf).unwrap();
		m.set_offset(0x12345678);
	}
	let h = unit::Hdr::new(&buf).unwrap();
	assert_eq!(h.offset(), 0x12345678);
	assert_eq!(buf[4], 0x78);
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- a fixed-width text number (section 8.6.2) ------------------------------

TEXT = "struct reply { decimal u16 code[3]; u8 sep; }"


def test_a_fixed_width_text_number_parses() -> None:
	"""It reported "element type u16 has no fixed size" about a type that
	plainly has one: the bracket is a width in bytes, not a count."""
	module = emit(TEXT)

	assert "pub fn code(&self) -> Result<u16>" in module
	assert "situ_rt::parse_uint(self.code_digits(), 10, 999)" in module
	assert "has no fixed size" not in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_digits_parse_as_smtp_writes_them(tmp_path: Path) -> None:
	result = build(tmp_path, TEXT, main="""
fn check(line: &[u8], want: Option<u16>) {
	let mut buf = [0u8; 8];
	buf[..line.len()].copy_from_slice(line);
	let r = unit::Reply::new(&buf).unwrap();
	match want {
		Some(v) => assert_eq!(r.code().unwrap(), v),
		None => assert!(r.code().is_err()),
	}
}

fn main() {
	check(b"250 ", Some(250));
	check(b"007 ", Some(7));	// the leading zero is required
	check(b"2x0 ", None);
	check(b"25  ", None);		// a space is not a digit
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- a tag's coverage, dirty bit and finalize (section 14.2) ----------------

TAGGED = ("struct s { u8 hop; authenticated { u16 seq; u8 body[4]; }"
	" tag u8 mac[16]; }")


def test_a_tag_gets_its_bytes_span_and_dirty_bit() -> None:
	module = emit(TAGGED)

	assert "pub fn mac(&self) -> &[u8]" in module
	assert "pub fn mac_covered(&self) -> Result<(usize, usize)>" in module
	assert "pub fn mac_is_dirty(dirty: &Dirty) -> bool" in module
	assert "pub fn mac_finalize(dirty: &mut Dirty)" in module
	assert "not in the static subset yet" not in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_span_and_the_bit_behave(tmp_path: Path) -> None:
	result = build(tmp_path, TAGGED, main="""
use situ_rt::Dirty;

fn main() {
	let mut buf = [0u8; 32];
	let mut dirty = Dirty::new();

	{
		let v = unit::S::new(&buf).unwrap();
		assert_eq!(v.mac_covered().unwrap(), (1, 6));
		assert_eq!(v.mac().len(), 16);
		assert!(!unit::S::mac_is_dirty(&dirty));
	}
	{
		let mut m = unit::SMut::new(&mut buf).unwrap();
		m.set_seq(&mut dirty, 0x1234);
	}
	assert!(unit::S::mac_is_dirty(&dirty));
	unit::S::mac_finalize(&mut dirty);
	assert!(!unit::S::mac_is_dirty(&dirty));
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- the offset cache (decision 0022) ---------------------------------------

# -- an offset the message chooses (26.27) ---------------------------------


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_member_past_the_frame_is_empty_not_a_panic(tmp_path: Path) -> None:
	"""A message that says its payload is a thousand bytes, in seventy of them.

	`example/packet` puts its tag after a sealed region sized by `hdr.length`.
	Rust's answer to a slice past the end is a panic, which in a `no_std` build
	is an abort -- a denial of service rather than a mitigation. Found by
	`make fuzz` (26.27); the accessor answers empty and `validate` reports the
	message as malformed.
	"""
	result = build(
		tmp_path,
		(ROOT / "example" / "packet" / "packet.situ").read_text(encoding="ascii"),
		preamble="", main=r"""
fn main() {
	let mut raw = [0u8; 70];
	raw[4] = 1;			// hdr.version, [must_eq = 1]
	raw[5] = 1;			// hdr.type = hello
	raw[6] = 0x03;			// hdr.length = 1000, inside [max = 1024]
	raw[7] = 0xe8;

	{
		let view = unit::Packet::new(&raw).unwrap();
		assert!(view.tag().is_empty());
		assert!(matches!(view.validate(), Err(situ_rt::Error::Bounds)));
	}

	raw[6] = 0;
	raw[7] = 8;
	let view = unit::Packet::new(&raw).unwrap();
	assert_eq!(view.tag().len(), 16);
	assert!(view.validate().is_ok());
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


# -- framing a run (20.3) ---------------------------------------------------


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_every_prefix_of_a_real_request_answers_honestly(tmp_path: Path) -> None:
	"""The claim the entry made: an HTTP header block could not be framed.

	`example/http` rather than a schema written for this, because that is what
	the entry named and 26.32's rule is that the worked example is the claim.
	Every prefix of a real request must come back truncated, and every bound
	must be a bound: greater than what is in hand, and never more than the
	message turns out to be. A framer that overshoots stalls waiting for bytes
	that will not come."""
	result = build(
		tmp_path,
		(ROOT / "example" / "http" / "http.situ").read_text(encoding="ascii"),
		preamble="", main=r"""
fn main() {
	let req: &[u8] = b"GET /index.html HTTP/1.1\r\n\
		Host: example.com\r\n\
		Accept: */*\r\n\
		\r\n";
	let whole = req.len();

	for i in 0..whole {
		match unit::RequestHead::required(&req[..i]) {
			situ_rt::Framing::Complete(_) => panic!("complete at {}", i),
			situ_rt::Framing::Need(n) => assert!(n > i && n <= whole,
				"have {} need {}", i, n),
		}
	}
	assert_eq!(unit::RequestHead::required(req),
		situ_rt::Framing::Complete(whole));
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


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

	assert "at = situ_rt::advance(at, self.entries_span_from(at),"\
		" self.bytes.len());" in module
	assert "pub fn entries_span_from(&self, start: usize) -> usize {" in module


CHAIN = ('struct line { u8 method[] until " "; u8 target[] until " ";'
	' u8 version[] until "\\r\\n"; }')


def test_the_offset_cache_is_behind_the_flag() -> None:
	assert "resolve_offsets" not in emit(CHAIN)


def test_the_offset_cache_resolves_every_dynamic_member() -> None:
	module = emit_materialized(CHAIN)

	assert "pub struct LineOffsets {" in module
	assert "pub target: usize," in module
	assert "pub fn resolve_offsets(&self) -> LineOffsets" in module


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_cache_agrees_with_the_per_member_offsets(tmp_path: Path) -> None:
	"""Which is the whole point: one pass instead of a rescan per member, and
	the same answer."""
	result = build(tmp_path, CHAIN, materialize=True, main="""
fn main() {
	let line: &[u8] = b"GET /index.html HTTP/1.1\\r\\n";
	let r = unit::Line::new(line).unwrap();
	let off = r.resolve_offsets();

	assert_eq!(off.target, r.target_offset());
	assert_eq!(off.version, r.version_offset());
	assert_eq!((off.target, off.version), (4, 16));
}
""")
	assert result.returncode == 0, result.stderr
	assert subprocess.run([str(tmp_path / "out")]).returncode == 0


def test_an_opaque_region_hands_back_its_bytes() -> None:
	module = emit("struct s { u16 n; opaque payload [n]; }")

	assert "pub fn payload(&self) -> &[u8]" in module
	assert "not in the static subset yet" not in module


def test_a_member_after_a_sealed_region_is_placed() -> None:
	module = emit("codec seal { granularity = byte; length_preserving;"
	              " seekable; authenticated; invertible; deterministic; }\n"
	              "impl seal extern \"x\";\n"
	              "struct s { u16 n; sealed(seal) { u8 body[n]; }"
	              " tag u8 mac[16]; }")

	assert "pub fn mac_covered" in module
	assert "cannot resolve where the tag sits" not in module


WIDE = "struct w { u8 kind; u16 samples[4]; i32 deltas[2]; }"


def test_an_array_of_wide_scalars_gets_an_indexed_getter() -> None:
	"""It reported "element type u16 has no fixed size" of a type that plainly
	has one: the branch was only ever written for struct elements."""
	module = emit(WIDE)

	assert "pub fn samples(&self, index: usize) -> Result<u16>" in module
	assert "pub fn deltas(&self, index: usize) -> Result<i32>" in module
	assert "has no fixed size" not in module


def test_an_index_past_the_end_is_refused() -> None:
	module = emit(WIDE)

	assert "if index >= 4 {" in module


def test_value_bounds_are_exported_as_assoc_consts() -> None:
	"""`[min]`/`[max]` shared with hand-written callers (26.125)."""
	source = emit("const CAP = 9216;\n"
	              "struct s { u16 mtu [min = 576, max = CAP];"
	              " i8 bias [min = -20]; u16 size [max = 100]; }")
	assert "pub const MTU_VALUE_MIN: u16 = 576;" in source
	assert "pub const MTU_VALUE_MAX: u16 = 9216;" in source
	assert "pub const BIAS_VALUE_MIN: i8 = -20;" in source
	assert "pub const SIZE_VALUE_MAX: u16 = 100;" in source


# -- a relation keyed on an enum field (26.97) ------------------------------

#: A key whose second component is an enum member. `kind` names 0 and 1, so
#: 7 is a value the schema admits and does not name -- which is the case the
#: whole fix is about.
#:
#: `bit_order` is not decoration: a `u4` is sub-byte and the compiler refuses
#: a schema that does not say which end it counts from. The pin this replaces
#: omitted it, so its build failed at *generation* and the compile it was
#: written to watch never ran -- it was red for the wrong reason, which is the
#: vacuous pass wearing an xfail.
ENUM_KEY = """target buffer;
endian big;
bit_order msb_first;

enum kind : u4 {
	ask = 0,
	answer = 1,
}

struct tagged {
	u16   id;
	kind  what;
	u4    rest;
}

relation reply_to(query: tagged, reply: tagged)
		[timeout_ms = 150, retries = 2] {
	must reply.id == query.id;
	must reply.what == query.what;
}
"""


def test_the_key_builders_do_not_cast_an_enum_option() -> None:
	"""An enum getter answers `Option<T>`, and nothing casts that to a number.

	`relate`, `converse` and `drive` all reduce a key -- and a `must`
	comparing two enum fields -- to `u64`. Reading them through the ordinary
	getter emitted `u64::from(view.what())` and `view.what() as u64`, neither
	of which exists for an `Option`, so a schema keyed on an enum did not
	compile in Rust at all. `example/dns` keys on `opcode` and was exactly
	that shape.

	The read goes through `_bits`, which hands back the raw number. That is
	what the other three backends key on, and it is the only answer that
	correlates a message whose enum field holds a value no member names:
	mapping such a value to a named member's number would collide the two.
	"""
	from situc.codegen.rust import converse as converse_rs
	from situc.codegen.rust import drive as drive_rs
	from situc.codegen.rust import relate as relate_rs

	schema   = parse_text(ENUM_KEY)
	resolved = resolve(schema, solve(schema))

	emitted = [relate_rs.generate(schema, resolved, "unit")["unit_relate.rs"],
	           converse_rs.generate(schema, resolved, "unit")["unit_converse.rs"],
	           drive_rs.generate(schema, resolved, "unit")["unit_drive.rs"]]

	for module in emitted:
		assert "what_bits()" in module, module
		assert "u64::from(query.what())" not in module, module
		assert "u64::from(reply.what())" not in module, module
		assert "query.what() as u64" not in module, module
		assert "reply.what() as u64" not in module, module
		# The plain getter still reads the id, which is not an enum.
		assert "id_bits()" not in module, module


def test_a_bound_naming_an_enum_sibling_compiles(tmp_path: Path) -> None:
	"""The same cast, reached by a different road.

	`[max = kind]` renders the sibling as `self.kind() as i64`, and an enum
	getter answers `Option<T>`, so the shape did not compile either. Found by
	sweeping the backend for every site that reads a field *as a number*
	rather than by the relation work that started it -- the key builders were
	two of three, not three of three.
	"""
	source = emit("enum k : u8 { a = 0, b = 1 }\n"
	              "struct s { k kind; u8 n [max = kind]; }")

	assert "self.kind_bits() as i64" in source, source
	assert "self.kind() as i64" not in source, source


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_bound_naming_an_enum_sibling_builds(tmp_path: Path) -> None:
	result = build(tmp_path, "enum k : u8 { a = 0, b = 1 }\n"
	               "struct s { k kind; u8 n [max = kind]; }")

	assert result.returncode == 0, result.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_a_relation_keyed_on_an_enum_compiles(tmp_path: Path) -> None:
	"""And the whole drive layer builds, which is what the key is for.

	Every rung from `relate` up reads the key, so this compiles the top one
	and gets the three below it for free. Before the `_bits` accessor this
	failed with two `E0605`s -- `non-primitive cast: Option<Kind> as u64` --
	and `example/dns`, the one worked example stating a retransmission
	policy, could not be built for Rust at any rung above `view`.
	"""
	schema = tmp_path / "tagged.situ"
	schema.write_text(ENUM_KEY, encoding="ascii")

	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "rust", "--layer", "drive", "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	src = tmp_path / "src"
	src.mkdir()
	for part in gen.glob("*.rs"):
		(src / part.name).write_text(part.read_text(encoding="ascii"),
		                             encoding="ascii")
	(src / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\n" + "".join(
			f"pub mod {part.stem};\n" for part in sorted(gen.glob("*.rs"))),
		encoding="ascii")

	assert RUSTC is not None
	compiled = subprocess.run(
		[RUSTC, "--edition", "2021", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
