"""A pad the data places is part of the element it ends (26.928).

    struct chunk { u8 id[4]; u32 size; u8 data[size]; pad_to(2); }

C's extent summed it as nothing, so a chunk of odd size ended a byte early and
the next was read from its pad; Python had no `_extent` and raised, and C++ and
Rust did not build. Every byte below is worked by hand.
"""

from __future__ import annotations

import struct
import wave
from io import BytesIO
from pathlib import Path

import pytest

import fourway
from every_schema import ROOT
from test_an_integer_can_start_inside_a_byte import _all, _built, _module
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian little;
struct chunk { u8 id; u8 size; u8 data[size]; pad_to(2); }
struct file  { chunk chunks[remaining]; }
struct fours { u8 size; u8 data[size]; pad_to(4); u8 last; }
struct many  { fours all[2]; }
"""

WHOLE, SHORT = "0", "1"

#: struct, message, verdict.
CASES = [
	# Odd data, its pad, and a second chunk that is where the pad ends.
	("file", "61" "01" "aa" "00" "62" "00", WHOLE),
	("file", "61" "02" "aaaa" "62" "00", WHOLE),
	# A pad past the frame is as many bytes as are there (26.578), which is
	# what a WAV that drops its last pad byte needs.
	("file", "61" "01" "aa", WHOLE),
	("file", "61" "02" "aa", SHORT),
	# Four, from the element's own start: 1 + 2 and one of pad.
	("many", "02" "aabb" "00" "07" "00" "000000" "08", WHOLE),
]


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_step_over_it(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for name, sample, verdict in CASES:
		assert _all(command, bytes.fromhex(sample), name, "validate",
		            tmp_path) == verdict, (name, sample)
	assert _all(command, bytes.fromhex("6101aa006200"), "file", "chunks",
	            tmp_path) == "count=2"


def test_both_walkers_step_over_it(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for name, sample, verdict in CASES:
		shape = shape_named_text(SCHEMA, name)
		data  = bytes.fromhex(sample)
		assert python_verdict(blob, data, shape) == verdict, (name, sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(name, sample)


def _wave() -> bytes:
	line = (ROOT / "example" / "riff" / "riff.vectors").read_text(
		encoding = "ascii").splitlines()[-1]
	return bytes.fromhex("".join(line.split()[2:]))


def test_ffmpeg_s_file_reads_as_python_s_wave_does(tmp_path: Path) -> None:
	module  = _module("riff", tmp_path)
	message = _wave()
	view    = module.riff.at(module.Message(message), 0, len(message))
	view.validate()
	held = {bytes(view.chunks(i).id): bytes(view.chunks(i).data)
	        for i in range(view.chunks_count)}
	with wave.open(BytesIO(message)) as reference:
		format_ = module.format.at(module.Message(held[b"fmt "]), 0)
		assert format_.channels == reference.getnchannels()
		assert format_.samples_per_second == reference.getframerate()
		assert format_.bits_per_sample == 8 * reference.getsampwidth()
		assert len(held[b"data"]) == reference.getnframes()
		assert held[b"data"] == reference.readframes(reference.getnframes())
	# The chunk the pad follows, which is why this file was made.
	assert len(held[b"bext"]) == 605
	assert struct.unpack_from("<I", message, 4)[0] == len(message) - 8
