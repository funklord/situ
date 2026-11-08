"""The dissector reads a whole streamed WebM file (26.939).

GStreamer's `webmmux streamable=true` writes every Cluster's size as the
eight-byte unknown, which `wide_cluster` tells apart by peeking all eight
bytes as a u64. The dissector read nothing wider than four bytes, so the
Cluster's variant was declined and nothing inside a Cluster was shown. A
wider read is two halves and arithmetic now.

Held to ffprobe, which nothing in situ wrote: every SimpleBlock the
dissector shows is a packet ffprobe reads, at the same place and of the
same size -- its data less the four bytes of track number, timecode and
flags. The fourth packet is a Block inside a BlockGroup, which the schema
describes as an element and no further, so it is not among them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from test_dissector import LUA, dissect

ROOT     = Path(__file__).resolve().parents[2]
MATROSKA = ROOT / "example" / "matroska" / "matroska.situ"


@pytest.mark.skipif(LUA is None or shutil.which("ffprobe") is None,
                    reason="needs Lua and ffprobe")
def test_every_simple_block_is_a_packet_ffprobe_reads(tmp_path: Path) -> None:
	line = next(one for one in (ROOT / "example/matroska/matroska.vectors")
	            .read_text(encoding="ascii").splitlines()
	            if one.startswith("document streamed"))
	data = bytes.fromhex("".join(line.split()[2:]))
	consumed, rows = dissect(tmp_path, MATROSKA, "document", data)

	blocks: list[tuple[int, int]] = []
	held = ""
	for field, at, length, value in rows:
		if field == "element.id":
			held = value
		# 0xA3, SimpleBlock's ID, which is shown as the number it is.
		elif field == "element.data" and held == str(0xA3):
			blocks.append((at, length - 4))

	(tmp_path / "streamed.webm").write_bytes(data)
	probed = subprocess.run(
		["ffprobe", "-v", "error", "-show_entries", "packet=pos,size",
		 "-of", "csv=p=0", str(tmp_path / "streamed.webm")],
		check=True, capture_output=True, text=True, timeout=60)
	packets = {(int(pos), int(size)) for size, pos in
	           (line.split(",")[:2] for line in probed.stdout.split())}

	assert consumed == len(data)
	assert len(blocks) == 3
	assert set(blocks) <= packets
