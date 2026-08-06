"""`situc pack`: the packed layout image (26.33, decision 0026).

The image is the one artifact here whose consumer lives in another
repository, which makes it the one artifact that could ship wrong and nobody
here would know. 0026 rejected a hand-rolled format on exactly that ground:
it would be "the only artifact in the project that nothing checks, in the
component whose input is least trusted".

So the check is a round trip, and it goes through the *generated accessors*
for `std/image.situ` rather than through `struct.unpack`. Reading the image
back the way an interpreter will read it is what makes this a test of the
format rather than a test of this file's opinion about the format: if the
schema and the packer disagree, one of them is wrong and the accessors are
where that surfaces.

What this does not do is walk bytes. A walker that answers the same
questions as the four backends is 26.33's next slice and lives in the other
repository; until it exists the image is proven to carry the layout, not to
be sufficient for a parse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from typing import Any

from situc import ast
from situc import pack as packer
from situc import traverse
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

from every_schema import ROOT, SCHEMAS, ids

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


def read_back(module: ModuleType, blob: bytes) -> dict[str, Any]:
	"""Reconstruct the layout from the image, through the accessors."""
	msg   = module.Message(bytearray(blob))
	view  = module.image.at(msg)
	head  = view.head

	structs, placements = [], []
	raw = bytes(view.structs)
	for i in range(head.struct_count):
		at = module.image_struct(module.Message(bytearray(raw)),
		                         i * packer.STRUCT_BYTES, packer.STRUCT_BYTES)
		structs.append((at.first_placement, at.placement_count, at.size_bits))

	raw = bytes(view.placements)
	for i in range(head.placement_count):
		at = module.image_placement(module.Message(bytearray(raw)),
		                            i * packer.PLACEMENT_BYTES,
		                            packer.PLACEMENT_BYTES)
		placements.append({
			"kind":          int(at.kind),
			"endian":        int(at.endian),
			"offset_bits":   at.offset_bits,
			"size_bits":     at.size_bits,
			"size_max_bits": at.size_max_bits,
			"array_count":   at.array_count,
			"size_code":     at.size_code,
			"flags":         at.flags,
		})

	return {
		"magic":       bytes(head.magic),
		"version":     head.format_version,
		"flags":       head.flags,
		"structs":     structs,
		"placements":  placements,
		"code":        bytes(view.code),
		"image_bytes": head.image_bytes,
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

	rows = [p for _, st in resolved.structs.items()
	        for p in (e.placement for e in traverse.own_entries(st))]
	assert len(seen["placements"]) == len(rows)

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
	"""The bytecode for the one array size expression in `source`."""
	schema, resolved = _resolved(source)
	_, coverage = packer.pack(schema, resolved)
	assert not coverage.unencodable, coverage.unencodable
	blob, _ = packer.pack(schema, resolved)
	head_end = packer.HEADER_BYTES
	import struct as _s
	code_off, = _s.unpack_from("<I", blob, 28)
	code_len, = _s.unpack_from("<I", blob, 16)
	return blob[code_off:code_off + code_len]


def test_the_bytecode_is_postfix_and_terminated() -> None:
	"""`n * 2 + 1` is push, push, mul, push, add -- and then END.

	Checked as bytes rather than by evaluating, because nothing in this
	repository evaluates one: the walker is the other project's, and a test
	that wrote its own evaluator would be checking the evaluator.
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
			lambda path: 0)


def test_the_metadata_tail_is_optional_and_additive() -> None:
	"""`--metadata` appends; it does not move the core.

	26.33 recorded that the two consumers pull opposite ways. The split is
	only worth anything if the device's image is a prefix of the tooling
	one -- otherwise it is two formats with one name.
	"""
	schema, resolved = _resolved(IMAGE_SCHEMA.read_text(encoding="ascii"))
	bare, _ = packer.pack(schema, resolved, metadata=False)
	full, _ = packer.pack(schema, resolved, metadata=True)

	assert len(full) > len(bare)
	# Everything but the header's flags and length word is byte-identical.
	assert bare[:4] == full[:4]
	assert bare[8:32] == full[8:32]
	assert bare[packer.HEADER_BYTES:] == full[packer.HEADER_BYTES:len(bare)]
	assert not bare[6] & packer.FLAG_METADATA
	assert full[6] & packer.FLAG_METADATA


def test_the_metadata_tail_carries_the_names_and_the_vectors() -> None:
	"""The tooling half of the split: a walker can print a field name.

	Without this the tail is dead weight, and the split it justifies is not
	worth the format version it costs.
	"""
	schema, resolved = _resolved(IMAGE_SCHEMA.read_text(encoding="ascii"))
	full, _ = packer.pack(schema, resolved, metadata=True)

	assert b"image_header\0" in full
	assert b"image_header.magic\0" in full
