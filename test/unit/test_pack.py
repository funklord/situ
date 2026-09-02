"""`situc pack`: the packed layout image (26.33, decision 0026).

The image is the one artifact here with no consumer yet, which makes it the
one that could ship wrong and nobody would know. 0026 rejected a hand-rolled format on exactly that ground:
it would be "the only artifact in the project that nothing checks, in the
component whose input is least trusted".

So the check is a round trip, and it goes through the *generated accessors*
for `std/image.situ` rather than through `struct.unpack`. Reading the image
back the way an interpreter will read it is what makes this a test of the
format rather than a test of this file's opinion about the format: if the
schema and the packer disagree, one of them is wrong and the accessors are
where that surfaces.

What this does not do is walk bytes. A walker that answers the same
questions as the four backends is 26.33's next slice, and 0026's amendment
puts it in this repository as its own binary so that it can join the
differential check; until it exists the image is proven to carry the layout,
not to be sufficient for a parse.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from typing import Any

from situc import ast
from situc.capability import Axis, DOMAINS
from situc import pack as packer
from situc import traverse
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

from every_schema import ROOT, SCHEMAS, ids

sys.path.insert(0, str(ROOT))
from walker import image as image_reader                # noqa: E402

IMAGE_SCHEMA = ROOT / "std" / "image.situ"


def _resolved(text: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse_text(text)
	return schema, resolve(schema, solve(schema))


@pytest.fixture(scope="module")
def image_module(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
	"""The generated Python accessors for `std/image.situ`.

	Generated rather than committed, so that editing the schema and forgetting
	the packer fails here instead of drifting.
	"""
	from situc.codegen.python import generate as generate_py

	tmp = tmp_path_factory.mktemp("image")
	schema, resolved = _resolved(IMAGE_SCHEMA.read_text(encoding="ascii"))
	(tmp / "image.py").write_text(
		generate_py(schema, resolved, "image").module, encoding="ascii")

	runtime = tmp / "situ_runtime.py"
	runtime.write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")

	sys.path.insert(0, str(tmp))
	try:
		spec = importlib.util.spec_from_file_location("image", tmp / "image.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		return module
	finally:
		sys.path.remove(str(tmp))


def directory(module: ModuleType,
              blob: bytes) -> list[tuple[int, int, int, int]]:
	"""Every directory entry in order, as (kind, offset, count, stride).

	A list rather than a dict, and that is the whole point of it: a dict
	keyed by kind is what both walkers build, so a second section under a
	tag replaces the first there and disappears from any test that looks
	through one. Two `strings = 4` sections shipped for exactly that long.
	"""
	msg  = module.Message(bytearray(blob))
	head = module.image.at(msg).head
	out = []
	for i in range(head.section_count):
		at = module.image_section(
			msg, head.section_offset + i * packer.SECTION_BYTES,
			packer.SECTION_BYTES)
		out.append((int(at.kind), at.offset, at.count, at.stride))
	return out


def sections(module: ModuleType, blob: bytes) -> dict[int, tuple[int, int, int]]:
	"""The section directory, read through the generated accessors.

	kind -> (offset, count, stride). A walker does exactly this and then
	keeps the kinds it knows, which is what makes the format extensible.

	`std/image.situ` declares each tag once, so building the table is only
	lossless while that holds -- and where it does not, a walker keeps the
	last entry and the first section is unreachable. Refused here rather
	than collapsed, so that every test reading a directory sees a duplicate
	instead of quietly reading whichever section came last.
	"""
	entries = directory(module, blob)
	kinds   = [kind for kind, _off, _count, _stride in entries]
	repeated = sorted({kind for kind in kinds if kinds.count(kind) > 1})
	assert not repeated, \
		f"two sections share a tag {repeated}; a walker keeps only the last"
	return {kind: (off, count, stride)
	        for kind, off, count, stride in entries}


def records(module: ModuleType, blob: bytes, view: type, kind: int,
            found: dict[int, tuple[int, int, int]]) -> list[Any]:
	"""Every record of one section, as generated views over the image."""
	if kind not in found:
		return []
	offset, count, stride = found[kind]
	msg = module.Message(bytearray(blob))
	return [view(msg, offset + i * stride, stride) for i in range(count)]


def read_back(module: ModuleType, blob: bytes) -> dict[str, Any]:
	"""Reconstruct the layout from the image, through the accessors."""
	msg   = module.Message(bytearray(blob))
	head  = module.image.at(msg).head
	found = sections(module, blob)

	return {
		"magic":       bytes(head.magic),
		"version":     head.format_version,
		"flags":       head.flags,
		"image_bytes": head.image_bytes,
		"sections":    found,
		"structs": [
			(r.first_placement, r.placement_count, r.size_bits)
			for r in records(module, blob, module.image_struct,
			                 packer.SECTION_STRUCTS, found)],
		"placements": [
			{
				"kind":          int(r.kind),
				"endian":        int(r.endian),
				"offset_bits":   r.offset_bits,
				"size_bits":     r.size_bits,
				"size_max_bits": r.size_max_bits,
				"array_count":   r.array_count,
				"size_code":     r.size_code,
				"located_code":  r.located_code,
				"repeat_code":   r.repeat_code,
				"radix":         r.radix,
				"flags":         r.flags,
			}
			for r in records(module, blob, module.image_placement,
			                 packer.SECTION_PLACEMENTS, found)],
	}


# ---------------------------------------------------------------------------
# The image describes itself
# ---------------------------------------------------------------------------

def test_the_image_schema_packs_and_reads_back(image_module: ModuleType) -> None:
	"""The format describing itself is the first thing that has to work.

	If `std/image.situ` cannot be packed and read through its own generated
	accessors, nothing downstream is worth checking.
	"""
	schema, resolved = _resolved(IMAGE_SCHEMA.read_text(encoding="ascii"))
	blob, coverage   = packer.pack(schema, resolved)
	seen = read_back(image_module, blob)

	assert seen["magic"] == b"SITU"
	assert seen["version"] == packer.FORMAT_VERSION
	assert seen["image_bytes"] == len(blob)
	assert len(seen["structs"]) == coverage.structs
	assert len(seen["placements"]) == coverage.placements
	assert coverage.structs > 0 and coverage.placements > 0



def test_the_format_version_is_one_number_in_four_places() -> None:
	"""The schema, the writer, and both walkers, compared against each other.

	`std/image.situ` opens by naming four things that protect it -- `gen-fuzz`
	fuzzes the parser, `situc wire` pins the format, `map --check` catches a
	layout change, the differential compares four backends reading one image.
	Not one of them compares the schema to the code that writes an image, and
	the version drifted through two bumps because of it: the schema said
	`[must_eq = 2]` while all three of the others said 4, so C's generated
	`validate` rejected every image the packer has ever produced, and the wire
	signature faithfully pinned a version no image has ever carried.

	The round-trip above cannot see it -- it reads the field the packer wrote
	and compares it to the constant that wrote it, which agrees with itself
	whatever the schema says. This asks the other three.

	The C walker's copy is read out of its source because nothing imports it.
	That is the point rather than a shortcut: a constant in a file no test
	loads is exactly where the fourth statement of a number goes unchecked.
	"""
	schema = parse_text(IMAGE_SCHEMA.read_text(encoding="ascii"))
	declared: list[int] = []
	for decl in schema.decls:
		if not isinstance(decl, ast.StructDecl) or decl.name != "image_header":
			continue
		for member in decl.members:
			if not isinstance(member, ast.Field) or member.name != "format_version":
				continue
			for attr in member.attrs:
				if attr.name == "must_eq":
					assert isinstance(attr.value, ast.IntLiteral)
					declared.append(attr.value.value)
	assert len(declared) == 1, "the schema states the version exactly once"

	walk_c = (ROOT / "walker" / "c" / "situ_walk.c").read_text(encoding="ascii")
	found  = re.findall(r"u16_at\(image \+ 4\) != (\d+)u", walk_c)
	assert len(found) == 1, "the C walker states the version exactly once"

	assert declared[0] == packer.FORMAT_VERSION
	assert image_reader.FORMAT_VERSION == packer.FORMAT_VERSION
	assert int(found[0]) == packer.FORMAT_VERSION

@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_schema_packs_and_reads_back(path: Path,
                                           image_module: ModuleType) -> None:
	"""Every schema in the tree, through the accessors, compared field by
	field against what the resolver said.

	The comparison is against `resolved` rather than against a golden file on
	purpose: a golden image would pin whatever the packer did on the day it
	was written, including its mistakes.
	"""
	schema, resolved = _resolved(path.read_text(encoding="ascii"))
	blob, coverage   = packer.pack(schema, resolved)
	seen = read_back(image_module, blob)

	assert seen["image_bytes"] == len(blob)
	assert len(seen["structs"]) == len(resolved.structs)

	# Own members first, in declaration order, and then the members a
	# variant's arms name -- which are not any struct's own entries and had
	# no index at all until arms were rendered. A walker iterating a struct
	# reads only the span the struct record gives, so the tail is invisible
	# to it and an arm record has something to point at.
	rows = [p for _, st in resolved.structs.items()
	        for p in (e.placement for e in traverse.own_entries(st))]
	assert len(seen["placements"]) >= len(rows)

	for placement, got in zip(rows, seen["placements"]):
		where = f"{path.name}:{placement.path}"
		if placement.offset_bits is None:
			assert not got["flags"] & packer.OFFSET_KNOWN, where
		else:
			assert got["flags"] & packer.OFFSET_KNOWN, where
			assert got["offset_bits"] == placement.offset_bits, where
		assert got["size_bits"] == packer._u32(placement.size_bits), where
		assert got["size_max_bits"] == packer._u32(placement.size_max_bits), where
		assert got["array_count"] == packer._u32(placement.array_count), where


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_packing_is_deterministic(path: Path) -> None:
	"""Two runs over one schema produce one image.

	An image somebody commits beside a schema is only diffable if this holds,
	and a dict iteration order or a set would break it silently.
	"""
	schema, resolved = _resolved(path.read_text(encoding="ascii"))
	first, _  = packer.pack(schema, resolved)
	second, _ = packer.pack(schema, resolved)
	assert first == second


# ---------------------------------------------------------------------------
# Coverage, asserted positively
# ---------------------------------------------------------------------------

def test_the_packer_says_what_it_could_not_encode() -> None:
	"""26.76's lesson: a run that examined nothing must not read as clean.

	The packer reports what it dropped rather than emitting `none` and
	continuing, because an image whose size expression silently became
	nothing computes a wrong length in a program nobody here runs.
	"""
	schema, resolved = _resolved(
		"target buffer;\nendian big;\n"
		"struct s { u8 n; u8 data[n * 2 + 1]; }\n")
	blob, coverage = packer.pack(schema, resolved)

	assert coverage.expressions == 1, "the size expression was not encoded"
	assert coverage.unencodable == {}
	assert coverage.placements == 2


def test_a_relation_the_planner_refuses_is_reported() -> None:
	"""A relation dropped in silence is the shape `Coverage` exists to stop.

	`situc pack` runs no backend, so nothing else in the command says a word
	about a relation `situc.relation` refuses -- the image simply does not
	carry it, and the report read exactly as it does for a schema that
	declares none. The planner's own reason is what goes in, because it is
	the one already written and a second wording here would be a second
	opinion about why.
	"""
	schema, resolved = _resolved(
		"target buffer;\nendian big;\n"
		"struct msg { u64 wide; i8 narrow; }\n"
		"relation pairs(a: msg, b: msg) { must b.wide == a.narrow; }\n")
	_, coverage = packer.pack(schema, resolved)

	assert coverage.relations == 0, "the image encoded a refused relation"
	assert "relation pairs" in coverage.unencodable, \
		"a relation left the schema and reached no image, and nothing said so"
	why = coverage.unencodable["relation pairs"]
	assert "unsigned" in why and "signed" in why, why


def test_the_whole_tree_encodes_every_expression_it_carries() -> None:
	"""No schema in the tree has an expression the bytecode cannot say.

	Stated as a total rather than per-schema so that the number is visible:
	a silent drop to zero encodable expressions would otherwise pass every
	other test in this file.
	"""
	total, dropped = 0, {}
	for path in SCHEMAS:
		schema, resolved = _resolved(path.read_text(encoding="ascii"))
		_, coverage = packer.pack(schema, resolved)
		total += coverage.expressions
		for where, why in coverage.unencodable.items():
			dropped[f"{path.name}:{where}"] = why

	assert not dropped, f"expressions the image cannot carry: {dropped}"
	assert total > 0, "no schema in the tree exercised the bytecode"


# ---------------------------------------------------------------------------
# The bytecode
# ---------------------------------------------------------------------------

def compile_one(source: str) -> bytes:
	"""The bytecode section of the image for `source`, via the directory."""
	import struct as _s

	schema, resolved = _resolved(source)
	blob, coverage = packer.pack(schema, resolved)
	assert not coverage.unencodable, coverage.unencodable

	count, at = _s.unpack_from("<II", blob, 12)
	for i in range(count):
		kind, offset, records, stride = _s.unpack_from(
			"<IIII", blob, at + i * packer.SECTION_BYTES)
		if kind == packer.SECTION_CODE:
			return blob[offset:offset + records * stride]
	raise AssertionError("the image carries no code section")


def test_the_bytecode_is_postfix_and_terminated() -> None:
	"""`n * 2 + 1` is push, push, mul, push, add -- and then END.

	Checked as bytes rather than by evaluating, because nothing in this
	repository evaluates one yet -- the walker is a separate binary and is
	not written -- and a test that wrote its own evaluator would be checking
	the evaluator.
	"""
	code = compile_one("target buffer;\nendian big;\n"
	                   "struct s { u8 n; u8 data[n * 2 + 1]; }\n")
	Op = packer.Op
	assert code[0] == Op.FIELD
	assert code[-1] == Op.END
	assert Op.MUL in code and Op.ADD in code
	assert Op.PUSH in code


def test_an_expression_outside_section_10_is_refused() -> None:
	"""A construct the bytecode cannot say raises rather than encoding zero.

	The failure mode this exists to prevent is a walker that computes a
	length of nothing and reports a truncated message as a well-formed one.
	"""
	program = packer.Program()
	with pytest.raises(packer.PackError):
		program.compile(
			parse_text("target buffer;\nendian big;\n"
			           "struct s { u8 a; u8 b[a]; }\n")
			and __import__("situc.ast", fromlist=["ast"]).StringLiteral(
				span=None, value="no"),		# type: ignore[arg-type]
			lambda path: (0, 0))


#: Schemas this file packs both ways, and whether each has a *core* string
#: pool -- one a walk needs in order to function, as against the tail's.
#: `std/image.situ` has no codec, no varint and no relation, so it has no
#: core pool at all: the test that was meant to hold the tail additive ran
#: only on that, and could not have seen the tail's own pool shadowing a
#: core one. The two examples are here so that it can.
POOLED: tuple[tuple[Path, bool], ...] = (
	(IMAGE_SCHEMA, False),
	(ROOT / "example" / "packet" / "packet.situ", True),	# codec aes_gcm_128
	(ROOT / "example" / "dns" / "dns.situ", True),		# relation reply_to
)


def _pooled_string(blob: bytes, found: dict[int, tuple[int, int, int]],
                   offset: int) -> str:
	"""The string at `offset` in the image's one string pool."""
	assert packer.SECTION_STRINGS in found, "the image carries no strings"
	at, count, stride = found[packer.SECTION_STRINGS]
	pool = blob[at:at + count * stride]
	end  = pool.find(b"\0", offset)
	return pool[offset:end if end >= 0 else len(pool)].decode("ascii")


@pytest.mark.parametrize("source, pooled", POOLED,
                         ids=[path.stem for path, _ in POOLED])
def test_the_metadata_tail_is_optional_and_additive(
		image_module: ModuleType, source: Path, pooled: bool) -> None:
	"""`--metadata` adds sections; it does not change the core's content.

	26.33 recorded that the two consumers pull opposite ways, and the split
	is only worth anything if the device's image says exactly what the
	tooling one says about the layout. Under a section directory that is no
	longer "the bare image is a prefix" -- adding directory entries shifts
	every body -- so the invariant that carries the claim is that each core
	section's *bytes* are identical, which is what a walker reads.

	The string pool is the one section that grows, because the tail interns
	into it rather than opening a second `strings = 4` a walker would keep
	instead of the core one. Additive means something exact there: the core
	pool is a *prefix* of the merged one, so every offset a core section
	holds names the same string with the tail as without it.

	The core set is taken as "every tag but the tail's", rather than listed:
	a list is what left `strings` out of it, and a section added later would
	be left out the same way.
	"""
	schema, resolved = _resolved(source.read_text(encoding="ascii"))
	bare, _ = packer.pack(schema, resolved, metadata=False)
	full, _ = packer.pack(schema, resolved, metadata=True)

	assert len(full) > len(bare)
	assert not bare[6] & packer.FLAG_METADATA
	assert full[6] & packer.FLAG_METADATA

	bare_at = sections(image_module, bare)
	full_at = sections(image_module, full)
	tail = {packer.SECTION_NAMES, packer.SECTION_VECTORS}
	core = set(bare_at) - tail

	assert (packer.SECTION_STRINGS in core) is pooled, \
		"this schema no longer says what it was chosen to say about the pool"
	assert packer.SECTION_STRINGS in full_at, \
		"the tail's own names went into no pool"

	# The pool is the one section the tail may add to, and the one it may
	# bring into existence: a schema with no codec, no varint and no
	# relation needs no strings to be walked and has none until names are
	# asked for. Everything else the tail must leave exactly as it was.
	pool = {packer.SECTION_STRINGS}
	assert core - pool == set(full_at) - tail - pool, \
		"the tail added or removed a core section"
	for kind in sorted(core - pool):
		a_off, a_count, a_stride = bare_at[kind]
		b_off, b_count, b_stride = full_at[kind]
		assert (a_count, a_stride) == (b_count, b_stride), kind
		assert bare[a_off:a_off + a_count * a_stride] == \
			full[b_off:b_off + b_count * b_stride], \
			f"core section {kind} differs between bare and --metadata"

	if pooled:
		a_off, a_count, a_stride = bare_at[packer.SECTION_STRINGS]
		b_off, b_count, b_stride = full_at[packer.SECTION_STRINGS]
		assert a_stride == b_stride
		assert full[b_off:b_off + b_count * b_stride].startswith(
			bare[a_off:a_off + a_count * a_stride]), \
			"the tail moved the core pool's strings"

	assert packer.SECTION_NAMES not in bare_at
	assert packer.SECTION_NAMES in full_at


def test_the_image_carries_one_string_pool_and_the_tail_shares_it(
		image_module: ModuleType) -> None:
	"""Two sections cannot share a tag, and the tail's pool did.

	`std/image.situ` declares exactly one `strings = 4`, and both walkers
	read the directory into a table keyed by kind -- `found[kind] = ...` in
	walker/image.py, a field per tag in walker/c/situ_walk.c. So the tail's
	own pool did not sit beside the core one, it replaced it: with
	`--metadata`, every offset the core sections hold resolved against the
	names pool. dns's relation `reply_to` read back as `dns_header` and
	packet's codec `aes_gcm_128` as `header`.

	Read through `directory` rather than `sections`, because a dict is what
	hid it: the collapse a walker performs is the bug, so the test has to
	look at the entries the packer wrote.
	"""
	for source, _ in POOLED:
		schema, resolved = _resolved(source.read_text(encoding="ascii"))
		for metadata in (False, True):
			blob, _ = packer.pack(schema, resolved, metadata=metadata)
			kinds = [kind for kind, _o, _c, _s
			         in directory(image_module, blob)]
			assert len(kinds) == len(set(kinds)), \
				f"{source.name} --metadata={metadata}: a tag twice: {kinds}"

	# ...and the strings the core points at are the core's, with the tail
	# present. The offset is read out of the section that holds it, so this
	# fails if the pools are ever swapped rather than merged.
	schema, resolved = _resolved(
		(ROOT / "example" / "dns" / "dns.situ").read_text(encoding="ascii"))
	full, _ = packer.pack(schema, resolved, metadata=True)
	found   = sections(image_module, full)
	relations = records(image_module, full, image_module.image_relation,
	                    packer.SECTION_RELATIONS, found)
	assert relations, "dns no longer carries a relation"
	assert [_pooled_string(full, found, r.name) for r in relations] \
		== ["reply_to"]

	schema, resolved = _resolved(
		(ROOT / "example" / "packet" / "packet.situ").read_text(
			encoding="ascii"))
	full, _ = packer.pack(schema, resolved, metadata=True)
	found   = sections(image_module, full)
	codecs  = records(image_module, full, image_module.image_codec,
	                  packer.SECTION_CODECS, found)
	assert codecs, "packet no longer carries a codec"
	assert [_pooled_string(full, found, c.name) for c in codecs] \
		== ["aes_gcm_128"]


def test_the_metadata_tail_carries_the_names_and_the_vectors(
	image_module: ModuleType,
) -> None:
	"""The tooling half of the split: a walker can print a field name.

	Without this the tail is dead weight, and the split it justifies is not
	worth the format version it costs.

	It checked the names and not the vectors, which is half of what it is
	named for -- and the vectors were the half that was wrong.
	"""
	schema, resolved = _resolved(IMAGE_SCHEMA.read_text(encoding="ascii"))
	full, _ = packer.pack(schema, resolved, metadata=True)

	assert b"image_header\0" in full
	assert b"image_header.magic\0" in full

	at = sections(image_module, full)
	assert packer.SECTION_VECTORS in at, "the tail carries no vectors"
	offset, count, stride = at[packer.SECTION_VECTORS]
	assert stride == len(list(Axis))

	vectors = full[offset:offset + count * stride]
	assert vectors, "the vectors section is empty"
	assert 0xFF not in vectors, (
		"a vector byte says `this walker could not tell` for a value the "
		"compiler knows")


def test_a_parameterised_value_encodes_as_its_base_not_as_unknown(
	image_module: ModuleType,
) -> None:
	"""`0xFF` is the byte for "could not tell", and it was also the byte for
	every value carrying a parameter.

	The encoder compared `str(value)` against the domain, and a domain lists
	base names: `Covered(checksum)`, `AbsoluteStatic(0x06)` and `Fixed(2)`
	are in none of them, so each wrote `0xFF`. **A field a tag authenticates
	was indistinguishable from a field whose auth nobody worked out** --
	invariant 154, and the distinction a reader most needs before writing.
	Across five examples 289 of 1573 bytes were `0xFF` and every one of them
	had a value.

	The base is what the domain holds, so this costs no format change: the
	stride, the meaning of a byte, and every walker that skips the section
	are all unchanged. The parameter is still not carried, which would be
	one.
	"""
	schema, resolved = _resolved(
		"target buffer;\nendian big;\n"
		"struct s {\n"
		"\tauthenticated summed {\n"
		"\t\tu16 a;\n"
		"\t\tchecksum u8 sum[2] covers(summed) [self_as = 0];\n"
		"\t}\n"
		"}\n")
	full, _ = packer.pack(schema, resolved, metadata=True)

	offset, count, stride = sections(image_module, full)[packer.SECTION_VECTORS]
	vectors = full[offset:offset + count * stride]
	axes    = list(Axis)
	auth    = axes.index(Axis.AUTH)
	domain  = [str(name) for name in DOMAINS[Axis.AUTH]]

	seen = {domain[vectors[row * stride + auth]]
	        for row in range(count)
	        if vectors[row * stride + auth] != 0xFF}
	assert "Covered" in seen, (
		f"a covered field's auth did not survive the image: {sorted(seen)}")


# ---------------------------------------------------------------------------
# The side tables
# ---------------------------------------------------------------------------

def test_every_construct_the_tree_uses_is_encoded() -> None:
	"""No family is carried by a schema and dropped by the image.

	This is the check that caught the first version being far less complete
	than its own coverage report implied: it reported expressions only, and
	said nothing dropped over an image with no delimiter, no variant arm and
	no index table in it.
	"""
	carried: dict[str, int] = {}
	dropped: dict[str, dict[str, int]] = {}
	for path in SCHEMAS:
		schema, resolved = _resolved(path.read_text(encoding="ascii"))
		_, coverage = packer.pack(schema, resolved)
		for family, count in coverage.carried.items():
			carried[family] = carried.get(family, 0) + count
		if coverage.unencoded:
			dropped[path.name] = coverage.unencoded

	assert not dropped, f"constructs the image drops: {dropped}"
	# Named rather than counted: a family vanishing from the tree would
	# otherwise silently reduce what this asserts.
	assert set(carried) == {
		"region", "delimiter", "radix", "variant", "codec",
		"repeat", "varint", "located", "tlv", "indexed",
	}, sorted(carried)


def test_a_variant_reaches_the_image_with_its_arms(
		image_module: ModuleType) -> None:
	"""MQTT selects on a fixed header's packet type, and the walker needs
	every arm to pick one."""
	schema, resolved = _resolved(
		(ROOT / "example/mqtt/mqtt.situ").read_text(encoding="ascii"))
	blob, _ = packer.pack(schema, resolved)
	arms = records(image_module, blob, image_module.image_arm,
	               packer.SECTION_ARMS, sections(image_module, blob))

	assert arms, "a schema with variants produced no arm records"
	assert any(a.arm_flags == 0 for a in arms), "no ordinary case arm"
	assert all(a.placement != packer.NONE for a in arms)


def test_a_delimiter_reaches_the_image_with_its_bytes(
		image_module: ModuleType) -> None:
	"""HTTP ends a header name at `:` and a line at CRLF. A walker that
	cannot see those two byte strings cannot parse the message at all."""
	schema, resolved = _resolved(
		(ROOT / "example/http/http.situ").read_text(encoding="ascii"))
	blob, _ = packer.pack(schema, resolved)
	found = records(image_module, blob, image_module.image_delimiter,
	                packer.SECTION_DELIMITERS, sections(image_module, blob))

	assert found, "a text protocol produced no delimiter records"
	seen = {bytes(d.octets)[:d.length] for d in found}
	assert b"\r\n" in seen and b":" in seen, sorted(seen)


def test_a_codec_is_named_in_the_core_not_the_tail(
		image_module: ModuleType) -> None:
	"""A walker cannot dispatch a transform it cannot identify, so codec
	names are in the core string pool and survive without `--metadata`."""
	schema, resolved = _resolved(
		(ROOT / "example/smtp/smtp.situ").read_text(encoding="ascii"))
	bare, _ = packer.pack(schema, resolved, metadata=False)
	found   = sections(image_module, bare)

	assert packer.SECTION_CODECS in found
	assert packer.SECTION_STRINGS in found
	offset, count, stride = found[packer.SECTION_STRINGS]
	assert b"dot_stuffing" in bare[offset:offset + count * stride]


def test_an_unknown_section_kind_is_skippable() -> None:
	"""The directory is what lets the format grow: a walker predating a
	section reads the image and ignores it, rather than refusing to load.

	Asserted on the schema rather than on a walker, there being no walker
	yet: `image_section_tag` must be `default = pass`, because
	`default = error` would make every future section a breaking change.
	"""
	text = IMAGE_SCHEMA.read_text(encoding="ascii")
	block = text[text.index("enum image_section_tag"):]
	block = block[:block.index("}")]
	assert "default      = pass" in block, \
		"a section kind must be skippable, or the directory buys nothing"
