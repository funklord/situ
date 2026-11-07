"""CBOR's head, HPACK's integer and a BER tag number (26.922).

Each begins in the low bits of a byte whose top bits are other fields, which
the struct states before it. The expected numbers are the RFCs' own: RFC 7541's
appendix C.1, RFC 8949's appendix A, and tags EMV and ICAO publish.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import fourway
from every_schema import ROOT
from situc.diagnostics import SituError, Source
from situc.layout import solve
from situc.parser import parse
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

RUNTIME = ROOT / "runtime"

SCHEMA = """target buffer;
endian big;
bit_order msb_first;
varint_type head   { encoding = cbor; max_bits = 64; }
varint_type headm  { encoding = cbor; max_bits = 64; minimal; }
varint_type h5     { encoding = hpack; prefix = 5; max_bits = 32; }
varint_type h8     { encoding = hpack; prefix = 8; max_bits = 32; }
varint_type h7     { encoding = hpack; prefix = 7; max_bits = 32; }
varint_type h7m    { encoding = hpack; prefix = 7; max_bits = 32; minimal; }
varint_type tagno  { encoding = bertag; max_bits = 28; }
varint_type tagnom { encoding = bertag; max_bits = 28; minimal; }
struct a_head { u3 major; head v; u8 last; }
struct a_headm { u3 major; headm v; u8 last; }
struct a_h5 { u3 flags; h5 v; u8 last; }
struct a_h8 { h8 v; u8 last; }
struct a_h7m { u1 flag; h7m v; u8 last; }
struct a_tag { u2 class; u1 constructed; tagno v; u8 last; }
struct a_tagm { u2 class; u1 constructed; tagnom v; u8 last; }
struct string { u1 huffman; h7 n; u8 data[n]; u8 last; }
struct text { u3 major; head n; u8 data[n]; u8 last; }
"""

#: struct -> top bits -> (message, bytes used, value)
SAMPLES: dict[str, list[tuple[str, int | None, int, int]]] = {
	# RFC 7541, C.1.1 and C.1.2, the prefix's top bits set as they would be.
	"a_h5": [("ea", 7, 1, 10), ("ff9a0a", 7, 3, 1337), ("1f9a0a", 0, 3, 1337)],
	# C.1.3.
	"a_h8": [("2a", None, 1, 42)],
	# RFC 8949, appendix A.
	"a_head": [
		("00", 0, 1, 0), ("0a", 0, 1, 10), ("17", 0, 1, 23),
		("1818", 0, 2, 24), ("1819", 0, 2, 25), ("1864", 0, 2, 100),
		("1903e8", 0, 3, 1000), ("1a000f4240", 0, 5, 1000000),
		("1b000000e8d4a51000", 0, 9, 1000000000000),
		("1bffffffffffffffff", 0, 9, (1 << 64) - 1),
		("20", 1, 1, 0),		# -1
		("3903e7", 1, 3, 999),		# -1000
	],
	# EMV's 5F20 and 9F02, and ICAO's 7F49.
	"a_tag": [("5f20", 1, 2, 32), ("9f02", 2, 2, 2), ("7f49", 1, 2, 73),
	          ("5f8149", 1, 3, 201), ("30", 0, 1, 16)],
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _field(section: str, name: str) -> str:
	if "no-view" in section:
		return "no-view"
	return next(line.split(" ", 1)[1] for line in section.splitlines()
	            if line.startswith(name + " "))


def _all(command: dict[str, list[str]], message: bytes, struct: str,
		name: str, cwd: Path) -> str:
	given = {backend: _field(_section(fourway.answers(argv, message, cwd),
	                                  struct), name)
	         for backend, argv in command.items()}
	assert len(set(given.values())) == 1, (struct, name, message.hex(), given)
	return given["c"]


def _built(tmp_path: Path, text: str) -> dict[str, list[str]]:
	schema = tmp_path / "s.situ"
	schema.write_text(text, encoding = "ascii")
	return fourway.build(tmp_path, schema)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_what_the_rfcs_say(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, samples in SAMPLES.items():
		for text, top, used, value in samples:
			message = bytes.fromhex(text) + b"\xaa"
			assert _all(command, message, struct, "v", tmp_path) \
				== f"len={used} value={value}", (struct, text)
			assert _all(command, message, struct, "last", tmp_path) == "170", \
				(struct, text)
			if top is not None:
				name = {"a_h5": "flags", "a_head": "major",
				        "a_tag": "class"}[struct]
				assert _all(command, message, struct, name, tmp_path) \
					== str(top), (struct, text)


def _vectors(name: str) -> dict[str, bytes]:
	lines = (ROOT / "example" / name / f"{name}.vectors").read_text(
		encoding = "ascii").splitlines()
	return {line.split()[1]: bytes.fromhex("".join(line.split()[2:]))
	        for line in lines if line and not line.startswith("#")}


def _module(name: str, tmp_path: Path) -> ModuleType:
	subprocess.run([sys.executable, "-m", "situc", "build",
	                str(ROOT / "example" / name / f"{name}.situ"),
	                "--target", "python", "--out", str(tmp_path)],
	               cwd = ROOT, check = True, capture_output = True)
	sys.path[:0] = [str(RUNTIME / "python")]
	spec = importlib.util.spec_from_file_location(f"{name}_{tmp_path.name}",
	                                              tmp_path / f"{name}.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def _cbor(view: Any) -> Any:
	"""A data item as the value RFC 8949's appendix prints it."""
	major = view.major
	if major == 0:
		return view.body_as_unsigned.value_value
	if major == 1:
		return -1 - view.body_as_negative.value_value
	if major == 2:
		return bytes(view.body_as_bytes.bytes)
	if major == 3:
		return bytes(view.body_as_text.text).decode("ascii")
	if major == 4:
		held = view.body_as_array
		return [_cbor(held.items(i)) for i in range(held.items_count)]
	if major == 5:
		held = view.body_as_map
		pairs = [held.pairs(i) for i in range(held.pairs_count)]
		return {_cbor(one.key): _cbor(one.value) for one in pairs}
	if major == 6:
		held = view.body_as_tagged
		return (held.tag_value, _cbor(held.content))
	return ("simple", view.body_as_simple.value_value)


#: RFC 8949's appendix A, by the name `cbor.vectors` gives each.
APPENDIX_A = {
	"zero": 0, "ten": 10, "twenty_three": 23, "twenty_four": 24,
	"hundred": 100, "thousand": 1000, "million": 1000000,
	"trillion": 1000000000000, "largest": (1 << 64) - 1, "minus_one": -1,
	"minus_thousand": -1000, "half_zero": ("simple", 0),
	"double_1_1": ("simple", 0x3FF199999999999A), "false_value": ("simple", 20),
	"null_value": ("simple", 22), "empty_bytes": b"",
	"four_bytes": b"\x01\x02\x03\x04", "ietf": "IETF", "empty_array": [],
	"nested_array": [1, [2, 3], [4, 5]], "empty_map": {},
	"integer_map": {1: 2, 3: 4}, "mixed_map": {"a": 1, "b": [2, 3]},
	"array_with_map": ["a", {"b": "c"}],
	"date_string": (0, "2013-03-21T20:04:00Z"), "epoch": (1, 1363896240),
	"bignum": (2, b"\x01" + bytes(8)),
}


def test_rfc_8949_s_appendix_reads_as_it_prints(tmp_path: Path) -> None:
	module = _module("cbor", tmp_path)
	vectors = _vectors("cbor")
	assert set(vectors) == set(APPENDIX_A)
	for name, message in vectors.items():
		view = module.item.at(module.Message(message), 0, len(message))
		view.validate()
		assert _cbor(view) == APPENDIX_A[name], name
		if len(message) > 1 and name != "half_zero":
			cut = module.item.at(module.Message(message[:-1]), 0,
			                     len(message) - 1)
			with pytest.raises(module.SituError):
				cut.validate()


def _header(view: Any) -> tuple[Any, ...]:
	kind = view.kind
	if kind >= 8:
		return ("indexed", view.body_from_table.index_value)
	if kind in (2, 3):
		return ("size", (view.body_resize if kind == 2
		                 else view.body_resize_high).max_size_value)
	held: Any = getattr(view, {0: "body_plain", 1: "body_sensitive",
	                           4: "body_added", 5: "body_added_5",
	                           6: "body_added_6", 7: "body_added_7"}[kind])
	index = held.index_value
	name = index if index else bytes(held.name_named.name.data).decode("ascii")
	return ("never" if kind == 1 else "literal" if kind == 0 else "added",
	        name, held.value.huffman, bytes(held.value.data))


#: RFC 7541's appendix C.
APPENDIX_C = {
	"c_2_1": [("added", "custom-key", 0, b"custom-header")],
	"c_2_2": [("literal", 4, 0, b"/sample/path")],
	"c_2_3": [("never", "password", 0, b"secret")],
	"c_2_4": [("indexed", 2)],
	"c_3_1": [("indexed", 2), ("indexed", 6), ("indexed", 4),
	          ("added", 1, 0, b"www.example.com")],
	"c_3_2": [("indexed", 2), ("indexed", 6), ("indexed", 4), ("indexed", 62),
	          ("added", 24, 0, b"no-cache")],
	"c_3_3": [("indexed", 2), ("indexed", 7), ("indexed", 5), ("indexed", 63),
	          ("added", "custom-key", 0, b"custom-value")],
	"c_4_1": [("indexed", 2), ("indexed", 6), ("indexed", 4),
	          ("added", 1, 1, bytes.fromhex("f1e3c2e5f23a6ba0ab90f4ff"))],
}


def test_rfc_7541_s_appendix_reads_as_it_prints(tmp_path: Path) -> None:
	module = _module("hpack", tmp_path)
	vectors = _vectors("hpack")
	assert set(vectors) == set(APPENDIX_C)
	for name, message in vectors.items():
		view = module.block.at(module.Message(message), 0, len(message))
		view.validate()
		said = [_header(view.fields(i)) for i in range(view.fields_count)]
		assert said == APPENDIX_C[name], name


#: What `minimal` refuses, read as the getter's zero.
PADDED = {
	"a_headm": ["1817", "190005", "1a00000005", "1b0000000000000005"],
	"a_h7m":   ["ff8000", "7f8000"],
	"a_tagm":  ["1f05", "1f8020"],
}

#: What a shortest encoding is, which `minimal` lets through.
SHORTEST = {
	"a_headm": [("1818", 24), ("190100", 256)],
	"a_h7m":   [("7e", 126), ("7f00", 127), ("ff7f", 254)],
	"a_tagm":  [("1e", 30), ("1f1f", 31), ("1f8100", 128)],
}

#: What no reading of the form admits: a verdict of short.
NUMBERLESS = {
	"a_head": ["1c", "1d", "1e", "1f"],		# reserved, and indefinite
	"a_h5":   ["1f", "1f80", "1fffffffffff"],	# cut, or past `max_bits`
	"a_tag":  ["1f", "1f80", "1f8080808080"],
}


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_hold_a_number_to_its_shortest(tmp_path: Path) -> None:
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
			assert _all(command, bytes.fromhex(sample), struct, "validate",
			            tmp_path) in ("1", "no-view"), (struct, sample)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_number_sizes_what_follows_it(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	assert _all(command, bytes.fromhex("83616263" "07"), "string", "last",
	            tmp_path) == "7"
	assert _all(command, bytes.fromhex("6449455446" "07"), "text", "last",
	            tmp_path) == "7"
	padded = bytes.fromhex("78" "03" "616263" "07")
	assert _all(command, padded, "text", "last", tmp_path) == "7"


def test_both_walkers_measure_what_the_backends_do(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	asked: dict[tuple[str, str], str] = {}
	for struct, samples in SAMPLES.items():
		for sample, _top, used, _value in samples:
			asked[struct, sample + "07"] = "0"
			if used > 1:
				asked[struct, sample[:-2]] = "1"
	for struct, numberless in NUMBERLESS.items():
		for sample in numberless:
			asked[struct, sample] = "1"
	for struct, shortest in SHORTEST.items():
		for sample, _value in shortest:
			asked[struct, sample + "07"] = "0"
	asked["string", "83616263" "07"] = "0"
	asked["string", "83616263"] = "1"
	asked["text", "6449455446" "07"] = "0"
	asked["text", "64494554"] = "1"
	for (struct, sample), verdict in asked.items():
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(struct, sample)
		assert python_verdict(blob, data, shape) == verdict, (struct, sample)


BOUNDARIES = [0, 1, 23, 24, 30, 31, 32, 126, 127, 128, 255, 256, 1337, 65535,
              65536, (1 << 32) - 1, 1 << 32, (1 << 63), (1 << 64) - 2]

#: stem -> by hand from the RFC's own rule, taking the top bits it must keep.
def _hpack(value: int, prefix: int, top: int) -> str:
	"""RFC 7541, 5.1's pseudocode."""
	mask = (1 << prefix) - 1
	if value < mask:
		return f"{top | value:02x}"
	out = [top | mask]
	value -= mask
	while value >= 128:
		out.append(value % 128 + 128)
		value //= 128
	out.append(value)
	return bytes(out).hex()


def _cbor_head(value: int, top: int) -> str:
	"""RFC 8949, 3."""
	if value < 24:
		return f"{top | value:02x}"
	for extra, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
		if value < 1 << (8 * size):
			return f"{top | extra:02x}" + value.to_bytes(size, "big").hex()
	raise AssertionError(value)


def _ber(value: int, top: int) -> str:
	"""X.690, 8.1.2.4: base 128, high first, every octet but the last marked."""
	if value < 31:
		return f"{top | value:02x}"
	groups: list[int] = []
	while value:
		groups.insert(0, value & 0x7F)
		value >>= 7
	marked = [group | 0x80 for group in groups[:-1]] + groups[-1:]
	return bytes([top | 31, *marked]).hex()


FORMS: dict[str, tuple[str, Callable[[int], str]]] = {
	"hpack": (", 5", lambda value: _hpack(value, 5, 0xA0)),
	"cbor":  ("", lambda value: _cbor_head(value, 0xA0)),
	"bertag": ("", lambda value: _ber(value, 0xA0)),
}

C_WRITER = """#include "situ.h"
#include <stdio.h>
int main(void)
{
	static const uint64_t values[] = { VALUES };
	for (unsigned i = 0; i < sizeof values / sizeof *values; i++) {
		uint8_t out[12] = { 0xA0u };
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
		out[0] = 0xA0;
		let n = situ_rt::varint_FORM_put(&mut out, valueTAIL);
		let text: String = out[..n].iter().map(|b| format!("{:02x}", b)).collect();
		println!("{}", text);
	}
}
"""


@pytest.mark.parametrize("form", list(FORMS))
def test_all_three_runtimes_write_it_and_keep_the_bits_above(form: str,
		tmp_path: Path) -> None:
	"""The top bits are another field's, and each writer finds them set."""
	tail, rule = FORMS[form]
	wanted = [rule(value) for value in BOUNDARIES]
	sys.path.insert(0, str(RUNTIME / "python"))
	import situ_runtime  # type: ignore[import-not-found]
	put = getattr(situ_runtime, f"varint_{form}_put")
	get = getattr(situ_runtime, f"varint_{form}_get")
	measure = getattr(situ_runtime, f"varint_{form}_len")
	extra = (5,) if tail else ()
	lines = []
	for value in BOUNDARIES:
		out = bytearray(12)
		out[0] = 0xA0
		n = put(out, 0, value, *extra)
		assert n == measure(value, *extra)
		assert get(out, 0, *extra, 12) == (value, n)
		lines.append(out[:n].hex())
	assert lines == wanted

	if fourway.HOST_CC is not None:
		source = tmp_path / "w.c"
		source.write_text(C_WRITER.replace("FORM", form).replace(
			"TAIL", tail.replace("5", "5u")).replace(
			"VALUES", ", ".join(f"{value}ull" for value in BOUNDARIES)),
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
			"VALUES", ", ".join(f"{value}u64" for value in BOUNDARIES)).replace(
			"RT", str(RUNTIME / "rust" / "situ_rt.rs")), encoding = "ascii")
		subprocess.run([fourway.RUSTC, "--edition", "2021", "-A", "warnings",
		                str(source), "-o", str(tmp_path / "wr")], check = True)
		said = subprocess.run([str(tmp_path / "wr")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == wanted


def _refusal(text: str) -> str:
	with pytest.raises(SituError) as caught:
		solve(parse(Source("t.situ", "target buffer;\nbit_order msb_first;\n"
		                   + text)))
	return str(caught.value)


@pytest.mark.parametrize(("text", "said"), [
	("varint_type c { encoding = cbor; max_bits = 64; }\n"
	 "struct s { c v; }", "are not stated here"),
	("varint_type c { encoding = cbor; max_bits = 64; }\n"
	 "struct s { u2 a; c v; }", "are not stated here"),
	("varint_type h { encoding = hpack; prefix = 7; max_bits = 32; }\n"
	 "struct s { u2 a; h v; }", "are not stated here"),
	("varint_type h { encoding = hpack; prefix = 7; max_bits = 32; }\n"
	 "struct s { u8 n; u8 d[n]; h v; }", "are not stated here"),
	("varint_type h { encoding = hpack; max_bits = 32; }\nstruct s { }",
	 "needs `prefix`"),
	("varint_type h { encoding = leb128; prefix = 3; max_bits = 32; }\n"
	 "struct s { }", "needs `prefix`"),
	("varint_type h { encoding = hpack; prefix = 9; max_bits = 32; }\n"
	 "struct s { }", "from 1 to 8"),
	("varint_type h { encoding = hpack; prefix = 7; max_bits = 32;"
	 " max_bytes = 3; }\nstruct s { }", "declares `max_bytes`"),
	("varint_type t { encoding = bertag; max_bits = 32; }\nstruct s { }",
	 "bits in a `bertag`"),
])
def test_a_number_inside_a_byte_is_stated_or_refused(text: str,
		said: str) -> None:
	assert said in _refusal(text)


def _builders(name: str, tmp_path: Path) -> ModuleType:
	subprocess.run([sys.executable, "-m", "situc", "build",
	                str(ROOT / "example" / name / f"{name}.situ"),
	                "--target", "python", "--layer", "edit",
	                "--out", str(tmp_path)],
	               cwd = ROOT, check = True, capture_output = True)
	sys.path[:0] = [str(RUNTIME / "python"), str(tmp_path)]
	spec = importlib.util.spec_from_file_location(
		f"{name}_build_{tmp_path.name}", tmp_path / f"{name}_build.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def test_a_builder_writes_the_rfc_s_bytes(tmp_path: Path) -> None:
	"""The flag and the number go in one byte, and it is spent once."""
	module = _builders("hpack", tmp_path)
	out = bytearray(64)
	used = module.build_string(out, 0, b"custom-key")
	assert out[:used] == bytes.fromhex("0a") + b"custom-key"
	out = bytearray(300)
	used = module.build_string(out, 1, bytes(200))
	assert out[:2] == bytes.fromhex("ff49") and used == 202
	out = bytearray(8)
	assert out[:module.build_table_entry(out, 1, 2)] == bytes.fromhex("82")
	# 1337 in five bits is C.1.2's, under this pattern's top bits.
	out = bytearray(8)
	assert out[:module.build_size_update(out, 1, 1337)] \
		== bytes.fromhex("3f9a0a")
