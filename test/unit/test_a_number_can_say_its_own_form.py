"""Signed LEB128, EBML, UTF-8's numbers, the bijective form, git's size (26.925).

The expected numbers are the publications': DWARF 5's figure of signed LEB128,
RFC 8794's table of the same number at four widths, the Unicode standard's
worked code points, the comment over Bitcoin Core's VARINT, and git's pack
format. Each writer is checked against an encoder written here from the rule.
"""

from __future__ import annotations

import random
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import fourway
from every_schema import ROOT
from situc.diagnostics import SituError, Source
from situc.parser import parse
from test_an_integer_can_start_inside_a_byte import _all, _built, _module
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VALUES, VERDICT, _build_driver, c_answers, c_verdict,
                           python_answers, python_verdict, shape_named_text)

RUNTIME = ROOT / "runtime"

SCHEMA = """target buffer;
endian big;
bit_order msb_first;
varint_type sl   { encoding = sleb128; max_bits = 64; }
varint_type slm  { encoding = sleb128; max_bits = 64; minimal; }
varint_type en   { encoding = ebml; max_bits = 56; }
varint_type enm  { encoding = ebml; max_bits = 56; minimal; }
varint_type eid  { encoding = ebml_id; max_bits = 32; }
varint_type un   { encoding = utf8; max_bits = 36; }
varint_type unm  { encoding = utf8; max_bits = 36; minimal; }
varint_type bi   { encoding = bijective128; max_bits = 64; }
varint_type gs   { encoding = lebprefix; prefix = 4; max_bits = 64; }
varint_type gsm  { encoding = lebprefix; prefix = 4; max_bits = 64; minimal; }
struct a_sl  { sl v; u8 last; }
struct a_slm { slm v; u8 last; }
struct a_en  { en v; u8 last; }
struct a_enm { enm v; u8 last; }
struct a_eid { eid v; u8 last; }
struct a_un  { un v; u8 last; }
struct a_unm { unm v; u8 last; }
struct a_bi  { bi v; u8 last; }
struct a_gs  { u1 more; u3 kind; gs v; u8 last; }
struct a_gsm { u1 more; u3 kind; gsm v; u8 last; }
struct sized_en { en n; u8 data[n]; u8 last; }
struct sized_bi { bi n; u8 data[n]; u8 last; }
struct sized_gs { u1 more; u3 kind; gs n; u8 data[n]; u8 last; }
struct sized_un { un n; u8 data[n]; u8 last; }
"""

#: struct -> (message, bytes used, value)
SAMPLES: dict[str, list[tuple[str, int, int]]] = {
	# DWARF 5, figure 7.
	"a_sl": [("02", 1, 2), ("7e", 1, -2), ("ff00", 2, 127), ("817f", 2, -127),
	         ("8001", 2, 128), ("807f", 2, -128), ("8101", 2, 129),
	         ("ff7e", 2, -129),
	         ("00", 1, 0),
	         ("ffffffffffffffffff00", 10, (1 << 63) - 1),
	         ("8080808080808080807f", 10, -(1 << 63))],
	# RFC 8794, table 2: the number 2 at four widths.
	"a_en": [("82", 1, 2), ("4002", 2, 2), ("200002", 3, 2),
	         ("10000002", 4, 2), ("ff", 1, 127),
	         ("01ffffffffffffff", 8, (1 << 56) - 1)],
	# The EBML header and Matroska's Segment, as written.
	"a_eid": [("1a45dfa3", 4, 0x1A45DFA3), ("18538067", 4, 0x18538067),
	          ("4286", 2, 0x4286), ("ec", 1, 0xEC)],
	# The Unicode standard's own examples, then FLAC's widest.
	"a_un": [("24", 1, 0x24), ("c2a2", 2, 0xA2), ("e282ac", 3, 0x20AC),
	         ("f0908d88", 4, 0x10348),
	         ("fe83bfbfbfbfbf", 7, (1 << 32) - 1),
	         ("febfbfbfbfbfbf", 7, (1 << 36) - 1)],
	# Bitcoin Core, serialize.h.
	"a_bi": [("00", 1, 0), ("01", 1, 1), ("7f", 1, 127), ("8000", 2, 128),
	         ("807f", 2, 255), ("8100", 2, 256), ("fe7f", 2, 16383),
	         ("ff00", 2, 16384), ("ff7f", 2, 16511), ("82fe7f", 3, 65535),
	         ("8efefeff00", 5, 1 << 32)],
	# A commit of 100 bytes, and a blob of 15 and of 16.
	"a_gs": [("9406", 2, 100), ("3f", 1, 15), ("b001", 2, 16)],
}

#: Numbers a form admits and `minimal` does not, read as the getter's zero.
PADDED = {
	"a_slm": ["8100", "ff7f"],
	"a_enm": ["4002", "200002", "ff"],
	"a_unm": ["c181", "e08181"],
	"a_gsm": ["8100", "818000"],
}

#: The same numbers shortest, so the zero above is a refusal and no reading.
SHORTEST = {
	"a_slm": [("01", 1), ("7f", -1)],
	"a_enm": [("82", 2), ("407f", 127)],
	"a_unm": [("c280", 128)],
	"a_gsm": [("01", 1)],
}

#: Bytes that are no number at all.
NUMBERLESS = {
	"a_en": ["00"],
	"a_eid": ["00"],
	"a_un": ["80", "ff", "c041", "e28041"],
	"a_sl": ["ffffffffffffffffff01", "ffffffffffffffffff7e"],
	"a_bi": ["ffffffffffffffffffff00"],
}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_what_was_published(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, samples in SAMPLES.items():
		for text, used, value in samples:
			message = bytes.fromhex(text) + b"\xaa"
			assert _all(command, message, struct, "v", tmp_path) \
				== f"len={used} value={value}", (struct, text)
			assert _all(command, message, struct, "last", tmp_path) == "170", \
				(struct, text)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_keep_the_fields_beside_git_s_size(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for text, kind in [("9406", 1), ("3f", 3), ("b001", 3)]:
		message = bytes.fromhex(text) + b"\xaa"
		assert _all(command, message, "a_gs", "kind", tmp_path) == str(kind)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_refuse_what_is_no_number(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, padded in PADDED.items():
		for sample in padded:
			message = bytes.fromhex(sample) + b"\x07"
			assert _all(command, message, struct, "v", tmp_path).endswith(
				"value=0"), (struct, sample)
	for struct, shortest in SHORTEST.items():
		for sample, value in shortest:
			message = bytes.fromhex(sample) + b"\x07"
			assert _all(command, message, struct, "v", tmp_path).endswith(
				f"value={value}"), (struct, sample)
	for struct, numberless in NUMBERLESS.items():
		for sample in numberless:
			message = bytes.fromhex(sample) + b"\x07"
			assert _all(command, message, struct, "validate", tmp_path) \
				in ("1", "no-view"), (struct, sample)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_number_sizes_what_follows_it(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, head in [("sized_en", "4003"), ("sized_bi", "03"),
	                     ("sized_gs", "93" "00"), ("sized_un", "03")]:
		message = bytes.fromhex(head + "616263" "07")
		assert _all(command, message, struct, "last", tmp_path) == "7", struct


def test_both_walkers_measure_what_the_backends_do(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	asked: dict[tuple[str, str], str] = {}
	for struct, samples in SAMPLES.items():
		for sample, used, _value in samples:
			asked[struct, sample + "07"] = "0"
			if used > 1:
				asked[struct, sample[:-2]] = "1"
	for struct, numberless in NUMBERLESS.items():
		for sample in numberless:
			asked[struct, sample + "07"] = "1"
	for (struct, sample), verdict in asked.items():
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(struct, sample)
		assert python_verdict(blob, data, shape) == verdict, (struct, sample)


def test_both_walkers_read_the_value(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VALUES)

	def both(struct: str, sample: str) -> list[str]:
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(sample + "07")
		said  = c_answers(tmp_path, blob, data, shape, drive)
		assert said == python_answers(blob, data, shape), (struct, sample)
		return said

	for struct, samples in SAMPLES.items():
		for sample, _used, value in samples:
			assert str(value) in both(struct, sample), (struct, sample)
	for struct, shortest in SHORTEST.items():
		for sample, value in shortest:
			assert str(value) in both(struct, sample), (struct, sample)
	for struct, padded in PADDED.items():
		for sample in padded:
			assert "refused" in both(struct, sample), (struct, sample)


def _sleb(value: int) -> bytes:
	"""DWARF 5, figure C.3, as written there."""
	out = bytearray()
	while True:
		byte = value & 0x7F
		value >>= 7
		if (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40):
			return bytes(out) + bytes([byte])
		out.append(byte | 0x80)


def _ebml(value: int) -> bytes:
	width = next(n for n in range(1, 9)
	             if value < (1 << (7 * n)) - 1 or n == 8)
	return (value | (1 << (7 * width))).to_bytes(width, "big")


def _utf8(value: int) -> bytes:
	if value < 0x80:
		return bytes([value])
	width = next(n for n in range(2, 8) if value.bit_length() <= 5 * n + 1)
	tail  = [0x80 | ((value >> (6 * i)) & 0x3F) for i in range(width - 2, -1, -1)]
	return bytes([((0xFF00 >> width) & 0xFF) | (value >> (6 * (width - 1))),
	              *tail])


def _bijective(value: int) -> bytes:
	"""git's `write_no_reuse_object`, and Bitcoin Core's `WriteVarInt`."""
	out = [value & 0x7F]
	while value >> 7:
		value = (value >> 7) - 1
		out.insert(0, 0x80 | (value & 0x7F))
	return bytes(out)


def _git(value: int, top: int) -> bytes:
	"""git's `encode_in_pack_object_header`."""
	first = top | (value & 15)
	value >>= 4
	out = bytearray()
	while value:
		out.append(first | 0x80)
		first = value & 0x7F
		value >>= 7
	return bytes(out) + bytes([first])


def _zig(value: int) -> int:
	return value << 1 if value >= 0 else (~value << 1) | 1


EDGES = [0, 1, 15, 16, 63, 64, 126, 127, 128, 129, 255, 256, 8191, 8192, 16383,
         16384, 16510, 16511, 16512, 65535, (1 << 21) - 1, (1 << 28) - 1,
         (1 << 32) - 1, 1 << 32, (1 << 35) + 7, (1 << 36) - 1]


def _draws(most: int) -> list[int]:
	rng = random.Random(925)
	return [value for value in EDGES if value <= most] + [
		rng.getrandbits(rng.randint(1, most.bit_length())) for _ in range(200)]


#: form -> (tail, value to the runtime, bytes the rule says, values)
FORMS: dict[str, tuple[str, Callable[[int], int], Callable[[int], bytes],
                       list[int]]] = {
	"sleb": ("", _zig, _sleb,
	         [sign * value for value in _draws((1 << 63) - 1)
	          for sign in (1, -1)] + [-(1 << 63)]),
	"ebml": ("", int, _ebml, _draws((1 << 56) - 1)),
	"utf8": ("", int, _utf8, _draws((1 << 36) - 1)),
	"bijective": ("", int, _bijective, _draws((1 << 64) - 1)),
	"lebprefix": (", 4", int, lambda value: _git(value, 0x50),
	              _draws((1 << 64) - 1)),
}

C_WRITER = """#include "situ.h"
#include <stdio.h>
int main(void)
{
	static const uint64_t values[] = { VALUES };
	for (unsigned i = 0; i < sizeof values / sizeof *values; i++) {
		uint8_t out[12] = { 0x50u };
		uint32_t n = situ_varint_FORM_put(out, 12u, values[i]TAIL);
		for (uint32_t k = 0; k < n; k++) {
			printf("%02x", out[k]);
		}
		printf("\\n");
	}
	return 0;
}
"""

RUST_WRITER = """#[path = "RT"] mod situ_rt;
fn main() {
	for value in [VALUES] {
		let mut out = [0u8; 12];
		out[0] = 0x50;
		let n = situ_rt::varint_FORM_put(&mut out, valueTAIL);
		let text: String = out[..n].iter().map(|b| format!("{:02x}", b)).collect();
		println!("{}", text);
	}
}
"""


@pytest.mark.parametrize("form", list(FORMS))
def test_all_three_runtimes_write_what_the_rule_says(form: str,
		tmp_path: Path) -> None:
	tail, raw_of, rule, values = FORMS[form]
	raws   = [raw_of(value) for value in values]
	wanted = [rule(value).hex() for value in values]
	sys.path.insert(0, str(RUNTIME / "python"))
	import situ_runtime  # type: ignore[import-not-found]
	put = getattr(situ_runtime, f"varint_{form}_put")
	get = getattr(situ_runtime, f"varint_{form}_get")
	measure = getattr(situ_runtime, f"varint_{form}_len")
	extra = (4,) if tail else ()
	lines = []
	for raw in raws:
		out = bytearray(12)
		out[0] = 0x50
		n = put(out, 0, raw, *extra)
		assert n == measure(raw, *extra), raw
		assert get(out, 0, *extra, 12) == (raw, n), raw
		lines.append(out[:n].hex())
	assert lines == wanted

	if fourway.HOST_CC is not None:
		source = tmp_path / "w.c"
		source.write_text(C_WRITER.replace("FORM", form).replace(
			"TAIL", tail.replace("4", "4u")).replace(
			"VALUES", ", ".join(f"{raw}ull" for raw in raws)),
			encoding = "ascii")
		subprocess.run([fourway.HOST_CC, f"-I{RUNTIME / 'c'}", str(source),
		                "-o", str(tmp_path / "wc")], check = True)
		said = subprocess.run([str(tmp_path / "wc")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == wanted
	if fourway.RUSTC is not None:
		source = tmp_path / "w.rs"
		source.write_text(RUST_WRITER.replace("FORM", form).replace(
			"TAIL", tail).replace(
			"VALUES", ", ".join(f"{raw}u64" for raw in raws)).replace(
			"RT", str(RUNTIME / "rust" / "situ_rt.rs")), encoding = "ascii")
		subprocess.run([fourway.RUSTC, "--edition", "2021", "-A", "warnings",
		                str(source), "-o", str(tmp_path / "wr")], check = True)
		said = subprocess.run([str(tmp_path / "wr")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == wanted


def test_an_id_is_refused_where_its_marker_does_not_stand() -> None:
	sys.path.insert(0, str(RUNTIME / "python"))
	import situ_runtime
	assert situ_runtime.varint_ebml_id_len(0x1A45DFA3) == 4
	assert situ_runtime.varint_ebml_id_len(0x4286) == 2
	for value in (0, 0x0100, 0x8100, 0x7F):
		assert situ_runtime.varint_ebml_id_len(value) == 0, hex(value)


def _refusal(text: str) -> str:
	try:
		parse(Source("r.situ", "target buffer;\n" + text))
	except SituError as error:
		return str(error)
	return ""


@pytest.mark.parametrize(("text", "said"), [
	("varint_type v { encoding = ebml; max_bits = 57; }", "57 bits"),
	("varint_type v { encoding = utf8; max_bits = 37; }", "37 bits"),
	("varint_type v { encoding = ebml_id; max_bits = 28; }", "cannot be 28 bits"),
	("varint_type v { encoding = lebprefix; max_bits = 8; }", "needs `prefix`"),
	("varint_type v { encoding = lebprefix; prefix = 7; max_bits = 8; }",
	 "7 bits of its first byte"),
	("varint_type v { encoding = sleb128; transform = zigzag; max_bits = 8; }",
	 "already signed"),
	("varint_type v { encoding = sleb128; max_bits = 8; max_bytes = 2; }",
	 "declares `max_bytes`"),
])
def test_a_form_that_cannot_be_is_refused(text: str, said: str) -> None:
	assert said in _refusal(text)


def test_sleb128_reads_back_as_it_was_written() -> None:
	from situc.unparse import unparse
	written = "varint_type v { encoding = sleb128; max_bits = 64; }"
	schema  = parse(Source("r.situ", "target buffer;\n" + written))
	assert "transform" not in unparse(schema)


def _vectors(name: str) -> dict[str, bytes]:
	lines = (ROOT / "example" / name / f"{name}.vectors").read_text(
		encoding = "ascii").splitlines()
	return {line.split()[1]: bytes.fromhex("".join(line.split()[2:]))
	        for line in lines if line and not line.startswith("#")}


def test_clang_s_module_reads_as_llvm_objdump_prints_it(tmp_path: Path) -> None:
	module  = _module("wasm", tmp_path)
	message = _vectors("wasm")["object"]
	view    = module.module.at(module.Message(message), 0, len(message))
	view.validate()
	found   = [view.sections(i) for i in range(view.sections_count)]
	assert [one.id for one in found] == [1, 2, 3, 12, 10, 11, 0, 0, 0]
	assert {one.size_len for one in found} == {5}	# padded, for a linker
	code = next(one for one in found if one.id == 10).body_code.bytes_view
	body = code.functions(0).body_view
	said = [body.code(i) for i in range(body.code_count)]
	assert [one.op for one in said] == [0x20, 0xAC, 0x42, 0x7E]
	assert said[2].immediate_as_i64.value_value == -3000
	data = next(one for one in found if one.id == 11).body_data.bytes_view
	assert data.segments(0).offset_value == 0
	assert int.from_bytes(bytes(data.segments(0).bytes), "little",
	                      signed = True) == -3000


def test_ffmpeg_s_piped_document_reads_as_ffprobe_says(tmp_path: Path) -> None:
	module  = _module("matroska", tmp_path)
	message = _vectors("matroska")["piped"]
	view    = module.document.at(module.Message(message), 0, len(message))
	view.validate()
	assert view.segment_size_value == (1 << 56) - 1		# unknown
	fields = view.header.fields_view
	held   = {fields.children(i).id_value: bytes(fields.children(i).data)
	          for i in range(fields.children_count)}
	assert held[0x4282] == b"matroska"
	tops = [view.children(i) for i in range(view.children_count)]
	assert [_top_id(one) for one in tops] == [
		0x114D9B74, 0xEC, 0x1549A966, 0x1654AE6B, 0x1254C367, 0x1F43B675]


def _top_id(child: object) -> int:
	"""A Segment child's ID: a Cluster's, or any other element's."""
	held: Any = child
	if held.lead == 0x1F:
		return int(held.body_cluster.id_value)
	return int(held.body_other.id_value)


def test_gstreamer_s_streamed_clusters_end_before_the_next(
		tmp_path: Path) -> None:
	"""Four Clusters of unknown size, each before the next one's ID (26.930).

	The children are what ffprobe's four packets come from: a Timestamp and
	a SimpleBlock, then three with the previous Cluster's size (PrevSize,
	0xAB) between them, the last block in a BlockGroup.
	"""
	module  = _module("matroska", tmp_path)
	message = _vectors("matroska")["streamed"]
	view    = module.document.at(module.Message(message), 0, len(message))
	view.validate()
	tops = [view.children(i) for i in range(view.children_count)]
	assert [_top_id(one) for one in tops] == [
		0x1254C367, 0x1549A966, 0x1654AE6B] + [0x1F43B675] * 4
	kids = []
	for one in tops[3:]:
		run = one.body_cluster.body_eight.body_open
		kids.append([run.children(i).id_value
		             for i in range(run.children_count)])
	assert kids == [[0xE7, 0xA3], [0xE7, 0xAB, 0xA3], [0xE7, 0xAB, 0xA3],
	                [0xE7, 0xAB, 0xA0]]


def _element(module: object, data: bytes, wanted: int) -> bytes:
	"""The first element of an ID in a master element's bytes."""
	view = module.elements.at(module.Message(data), 0, len(data))	# type: ignore[attr-defined]
	return next(bytes(view.children(i).data)
	            for i in range(view.children_count)
	            if view.children(i).id_value == wanted)


def test_the_track_is_the_one_ffprobe_names(tmp_path: Path) -> None:
	module  = _module("matroska", tmp_path)
	message = _vectors("matroska")["piped"]
	view    = module.document.at(module.Message(message), 0, len(message))
	tracks  = next(bytes(view.children(i).body_other.data)
	               for i in range(view.children_count)
	               if _top_id(view.children(i)) == 0x1654AE6B)
	entry   = _element(module, tracks, 0xAE)
	# ffmpeg files FFV1 under a Windows codec ID, the FourCC in its private data.
	assert _element(module, entry, 0x86) == b"V_MS/VFW/FOURCC"
	assert _element(module, entry, 0x63A2)[16:20] == b"FFV1"
	video   = _element(module, entry, 0xE0)
	assert _element(module, video, 0xB0) == b"\x10"		# width
	assert _element(module, video, 0xBA) == b"\x10"		# height


def _crc8(data: bytes) -> int:
	held = 0
	for byte in data:
		held ^= byte
		for _ in range(8):
			held = ((held << 1) ^ 7) & 0xFF if held & 0x80 else (held << 1) & 0xFF
	return held


def test_flac_s_frames_carry_the_numbers_it_printed(tmp_path: Path) -> None:
	module  = _module("flac", tmp_path)
	vectors = _vectors("flac")
	opening = vectors["opening"]
	head    = module.stream_head.at(module.Message(opening), 0, len(opening))
	head.validate()
	info = head.blocks(0).body_as_info.info
	assert (info.sample_rate, info.total_samples) == (8000, 1200000)
	assert head.blocks(head.blocks_count - 1).last == 1
	widths = {0: 1, 127: 1, 128: 2, 2047: 2, 2048: 3, 65535: 3, 65536: 4,
	          74999: 4}
	for number, width in widths.items():
		message = vectors[f"frame_{number}"]
		view    = module.frame_header.at(module.Message(message), 0,
		                                 len(message))
		view.validate()
		assert view.fields.number_value == number
		assert view.fields.number_len == width
		assert view.fields.block_as_u8.size_less_one == 15
		assert _crc8(message[:-1]) == message[-1]


#: Where `git verify-pack -v` put each, and what it said is there.
PACKED = {
	"commit_at_12": (1, 165, None),
	"blob_at_726": (3, 57866, None),
	"delta_at_28339": (6, 33, 27613),
	"delta_at_28385": (6, 30, 46),
}


def test_a_pack_s_entries_are_what_verify_pack_said(tmp_path: Path) -> None:
	module  = _module("git", tmp_path)
	vectors = _vectors("git")
	head    = module.pack_header.at(module.Message(vectors["head"]), 0)
	head.validate()
	assert head.objects == 18
	for name, (kind, size, back) in PACKED.items():
		message = vectors[name]
		view    = module.entry.at(module.Message(message), 0, len(message))
		view.validate()
		assert (view.type, view.size_value) == (kind, size), name
		assert view.more == 1, name
		if back is not None:
			assert view.base_by_offset.back_value == back, name


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
@pytest.mark.parametrize(("name", "struct", "vector", "field", "said"), [
	("wasm", "module", "object", "version", "1"),
	("matroska", "document", "piped", "segment",
	 f"len=4 value={0x18538067}"),
	("flac", "stream_head", "opening", "validate", "0"),
	("git", "entry", "delta_at_28339", "type", "6"),
])
def test_all_four_read_the_tools_bytes(name: str, struct: str, vector: str,
		field: str, said: str, tmp_path: Path) -> None:
	schema  = ROOT / "example" / name / f"{name}.situ"
	command = fourway.build(tmp_path, schema)
	assert _all(command, _vectors(name)[vector], struct, field, tmp_path) == said
