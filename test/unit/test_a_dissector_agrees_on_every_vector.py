"""The dissector and the walker, over every committed vector.

`test_dissector.compare` hands both random bytes, which reach a format's
shallow paths and seldom its deep ones: radiotap's arms read byte-reversed
with it green (26.933), and a JSON value with whitespace in it, a protobuf
tag past 2^48 and a Cluster of unknown size were each found by a real
message rather than by it. The vectors are real messages -- most of them
written by somebody else's encoder -- so every one goes through both, and
every member both speak about is held equal, as `compare` holds them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from situc import pack as packer
from situc import traverse
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import DECLINED, LUA, PROTOFIELD, _shown, _walked, read_back
from walker.image import load

ROOT    = Path(__file__).resolve().parents[2]
VECTORS = sorted(path for path in (ROOT / "example").glob("*/*.vectors")
                 if path.with_suffix(".situ").exists())


def _agree(vectors: Path) -> tuple[int, list[str]]:
	"""Member answers compared over one example's vectors, and those that
	differ."""
	schema = vectors.with_suffix(".situ")
	source, resolved, _ = analyse(schema)
	parsed = parse(source)
	text   = generate(parsed, resolved, schema.stem)
	image  = load(packer.pack(parsed, resolved, metadata=True)[0])
	kinds  = {abbrev: (kind, base)
	          for kind, abbrev, base in PROTOFIELD.findall(text)}
	declined = set(DECLINED.findall(text))
	declined |= {f"{struct.name}.{entry.placement.name}"
	             for struct in resolved.structs.values()
	             for entry in struct.entries
	             if traverse.holds_of(entry.placement)
	             in traverse.recursion_cycle(resolved.structs, struct.name)}
	itemized = {f"{struct.name}.{entry.placement.name}"
	            for struct in resolved.structs.values()
	            for entry in struct.entries if entry.placement.kind == "tlv"}

	compared = 0
	differ: list[str] = []
	with tempfile.TemporaryDirectory() as held:
		lua = Path(held) / f"{schema.stem}.lua"
		lua.write_text(text, encoding="ascii")
		for line in vectors.read_text(encoding="ascii").splitlines():
			parts = line.split()
			if len(parts) < 3 or parts[0] not in resolved.structs \
					or resolved.structs[parts[0]].layout.register is not None:
				continue
			try:
				packet = bytes.fromhex("".join(parts[2:]))
			except ValueError:
				continue
			_, rows = read_back(lua, parts[0], packet)
			_, said = _shown(rows, parts[0], kinds, declined, itemized)
			_, read = _walked(image, parts[0], packet)
			for member in said.keys() & read.keys():
				compared += 1
				if said[member] != read[member]:
					differ.append(f"{parts[0]} {parts[1]}: {member} is"
					              f" {said[member]} to the dissector and"
					              f" {read[member]} to the walker")
	return compared, differ


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
@pytest.mark.parametrize("vectors", VECTORS, ids=lambda path: path.stem)
def test_every_vector_reads_alike(vectors: Path) -> None:
	_, differ = _agree(vectors)
	assert not differ, "\n".join(differ)


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_vectors_are_compared_at_all() -> None:
	"""Measured 2026-11-08: 909 member answers over 193 vectors in the
	examples that keep them. A floor, so that the comparison above cannot
	pass by comparing nothing."""
	assert sum(_agree(one)[0] for one in VECTORS) >= 880
