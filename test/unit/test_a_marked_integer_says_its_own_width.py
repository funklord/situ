"""BER's length, Bitcoin's CompactSize and MySQL's lenenc (26.921).

A first byte below a threshold is the value, and above it a marker for how many
bytes hold it. Every expected number here is from outside this tree: X.690's
rule, a certificate `openssl asn1parse` reads, and the genesis coinbase, whose
hash is the published transaction id.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fourway
from every_schema import ROOT
from situc.diagnostics import SituError, Source
from situc.parser import parse
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

RUNTIME = ROOT / "runtime"

SCHEMA = """target buffer;
endian big;
varint_type der  { encoding = der; max_bits = 64; }
varint_type derm { encoding = der; max_bits = 64; minimal; }
varint_type cs   { encoding = compactsize; max_bits = 64; }
varint_type csm  { encoding = compactsize; max_bits = 64; minimal; }
varint_type le   { encoding = lenenc; max_bits = 64; }
varint_type small { encoding = der; max_bits = 8; }
struct a_der { der v; u8 last; }
struct a_derm { derm v; u8 last; }
struct a_cs { cs v; u8 last; }
struct a_csm { csm v; u8 last; }
struct a_le { le v; u8 last; }
struct a_small { small v; u8 last; }
struct framed { u8 tag; der n; u8 body[n]; u8 last; }
"""

#: struct -> encoding -> (bytes used, value).
SAMPLES = {
	"a_der": {
		"05":                 (1, 5),
		"7f":                 (1, 127),
		"8180":               (2, 128),
		"820100":             (3, 256),
		"8105":               (2, 5),		# padded: BER's, not DER's
		"880102030405060708": (9, 0x0102030405060708),
	},
	"a_cs": {
		"fc":                 (1, 252),
		"fdfd00":             (3, 253),
		"fd0001":             (3, 256),
		"fe00000100":         (5, 65536),
		"ff0000000001000000": (9, 1 << 32),
	},
	"a_le": {
		"fa":                 (1, 250),
		"fcfb00":             (3, 251),
		"fd000001":           (4, 65536),
		"fe0000000001000000": (9, 1 << 32),
	},
}

#: What no reading of the form admits, whatever follows: a verdict of short.
NUMBERLESS = {
	"a_der": ["8000", "ff00", "8900"],	# indefinite, reserved, past eight
	"a_le":  ["fb00", "ff00"],		# NULL, and an error packet
}

#: What `minimal` refuses, read as the getter's zero.
PADDED = {"a_derm": ["8105", "820080"], "a_csm": ["fd0500", "fe05000000"]}

CERTIFICATE = bytes.fromhex("".join(
	line.split(None, 2)[2] for line in
	(ROOT / "example" / "x509" / "x509.vectors").read_text(
		encoding = "ascii").splitlines()
	if line.startswith("certificate ")))

GENESIS = bytes.fromhex("".join(
	line.split(None, 2)[2] for line in
	(ROOT / "example" / "bitcoin" / "bitcoin.vectors").read_text(
		encoding = "ascii").splitlines()
	if line.startswith("transaction ")))


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
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding = "ascii")
	return fourway.build(tmp_path, schema)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_what_each_form_says(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	for struct, samples in SAMPLES.items():
		for sample, (size, value) in samples.items():
			message = bytes.fromhex(sample) + b"\x07"
			assert _all(command, message, struct, "v", tmp_path) \
				== f"len={size} value={value}", (struct, sample)
			assert _all(command, message, struct, "last", tmp_path) == "7"
	for struct, numberless in NUMBERLESS.items():
		for sample in numberless:
			assert _all(command, bytes.fromhex(sample), struct, "validate",
			            tmp_path) == "1", (struct, sample)
	for struct, padded in PADDED.items():
		for sample in padded:
			message = bytes.fromhex(sample) + b"\x07"
			assert _all(command, message, struct, "v", tmp_path).endswith(
				"value=0"), (struct, sample)


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_type_of_eight_bits_stops_at_one_byte_of_value(tmp_path: Path) -> None:
	command = _built(tmp_path, SCHEMA)
	assert _all(command, bytes.fromhex("818007"), "a_small", "v", tmp_path) \
		== "len=2 value=128"
	assert _all(command, bytes.fromhex("82010007"), "a_small", "validate",
	            tmp_path) == "1"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_certificate_measures_as_openssl_says(tmp_path: Path) -> None:
	stated = 370
	if shutil.which("openssl") is not None:
		der = tmp_path / "c.der"
		der.write_bytes(CERTIFICATE)
		lines = subprocess.run(["openssl", "asn1parse", "-inform", "DER",
		                        "-in", str(der)], capture_output = True,
		                       text = True, check = True).stdout.splitlines()
		assert "hl=4" in lines[0]
		stated = int(lines[0].split("l=")[2].split()[0])
	text = (ROOT / "example" / "x509" / "x509.situ").read_text(encoding = "ascii")
	command = _built(tmp_path, text)
	assert _all(command, CERTIFICATE, "certificate", "validate", tmp_path) == "0"
	assert _all(command, CERTIFICATE[:-1], "certificate", "validate",
	            tmp_path) == "1"
	assert _all(command, CERTIFICATE, "certificate", "body", tmp_path) \
		.startswith(f"len={stated} ")
	assert _all(command, CERTIFICATE[4:], "items", "parts", tmp_path) \
		== "count=3"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_the_genesis_coinbase_is_what_every_node_holds(tmp_path: Path) -> None:
	once = hashlib.sha256(GENESIS).digest()
	assert hashlib.sha256(once).digest()[::-1].hex() == \
		"4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b"
	text = (ROOT / "example" / "bitcoin" / "bitcoin.situ").read_text(
		encoding = "ascii")
	command = _built(tmp_path, text)
	assert _all(command, GENESIS, "transaction", "validate", tmp_path) == "0"
	assert _all(command, GENESIS[:-1], "transaction", "validate", tmp_path) \
		== "1"
	assert _all(command, GENESIS, "transaction", "input_count", tmp_path) \
		== "len=1 value=1"


def test_both_walkers_measure_what_the_backends_do(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	asked: dict[tuple[str, str], str] = {}
	for struct, samples in SAMPLES.items():
		for sample in samples:
			asked[struct, sample + "07"] = "0"
			if len(sample) > 2:
				asked[struct, sample[:-2]] = "1"
	for struct, numberless in NUMBERLESS.items():
		for sample in numberless:
			asked[struct, sample] = "1"
	asked["framed", "30" "8103" "aabbcc" "07"] = "0"	# padded, and not `minimal`
	asked["framed", "30" "03" "aabbcc" "07"] = "0"
	asked["framed", "30" "8103" "aabbcc"] = "1"
	for (struct, sample), verdict in asked.items():
		shape = shape_named_text(SCHEMA, struct)
		data  = bytes.fromhex(sample)
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			(struct, sample)
		assert python_verdict(blob, data, shape) == verdict, (struct, sample)


BOUNDARIES = [0, 127, 128, 250, 251, 252, 253, 255, 256, 65535, 65536,
              (1 << 24) - 1, 1 << 24, (1 << 32) - 1, 1 << 32, (1 << 64) - 1]

FORMS = {
	"der":         ((0x80, 0x81, (1, 2, 3, 4, 5, 6, 7, 8)), "big"),
	"compactsize": ((0xFD, 0xFD, (2, 4, 8)), "little"),
	"lenenc":      ((0xFB, 0xFC, (2, 3, 8)), "little"),
}


def _wanted(form: str) -> list[str]:
	"""By hand from each form's rule."""
	(direct, first, widths), order = FORMS[form]
	lines = []
	for value in BOUNDARIES:
		if value < direct:
			lines.append(f"{value:02x}")
			continue
		at, size = next((at, n) for at, n in enumerate(widths)
		                if value < 1 << (8 * n))
		held = value.to_bytes(size, order)  # type: ignore[arg-type]
		lines.append(f"{first + at:02x}{held.hex()}")
	return lines


C_WRITER = """#include "situ.h"
#include <stdio.h>
int main(void)
{
	static const uint64_t values[] = { VALUES };
	for (unsigned i = 0; i < sizeof values / sizeof *values; i++) {
		uint8_t out[9];
		uint32_t n = situ_varint_FORM_put(out, 9u, values[i]);
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
		let mut out = [0u8; 9];
		let n = situ_rt::varint_FORM_put(&mut out, value);
		let text: String = out[..n].iter().map(|b| format!("{:02x}", b)).collect();
		println!("{}", text);
	}
}
"""


@pytest.mark.parametrize("form", list(FORMS))
def test_all_three_runtimes_write_each_form(form: str, tmp_path: Path) -> None:
	sys.path.insert(0, str(RUNTIME / "python"))
	import situ_runtime  # type: ignore[import-not-found]
	put = getattr(situ_runtime, f"varint_{form}_put")
	get = getattr(situ_runtime, f"varint_{form}_get")
	measure = getattr(situ_runtime, f"varint_{form}_len")
	lines = []
	for value in BOUNDARIES:
		out = bytearray(9)
		n   = put(out, 0, value)
		assert n == measure(value)
		assert get(out, 0, 9) == (value, n)
		lines.append(out[:n].hex())
	assert lines == _wanted(form)

	values = ", ".join(str(value) for value in BOUNDARIES)
	if fourway.HOST_CC is not None:
		source = tmp_path / "w.c"
		source.write_text(C_WRITER.replace("FORM", form).replace(
			"VALUES", ", ".join(f"{value}ull" for value in BOUNDARIES)),
			encoding = "ascii")
		subprocess.run([fourway.HOST_CC, f"-I{RUNTIME / 'c'}", str(source),
		                "-o", str(tmp_path / "wc")], check = True)
		said = subprocess.run([str(tmp_path / "wc")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == _wanted(form)
	if fourway.RUSTC is not None:
		source = tmp_path / "w.rs"
		source.write_text(RUST_WRITER.replace("FORM", form).replace(
			"VALUES", ", ".join(f"{value}u64" for value in BOUNDARIES)).replace(
			"RT", str(RUNTIME / "rust" / "situ_rt.rs")), encoding = "ascii")
		subprocess.run([fourway.RUSTC, "--edition", "2021", "-A", "warnings",
		                str(source), "-o", str(tmp_path / "wr")], check = True)
		said = subprocess.run([str(tmp_path / "wr")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == _wanted(form)


@pytest.mark.parametrize("encoding", ["der", "compactsize", "lenenc"])
def test_max_bytes_is_the_marker_s_to_say(encoding: str) -> None:
	text = f"varint_type q {{ encoding = {encoding}; max_bits = 32; max_bytes = 5; }}\n"
	with pytest.raises(SituError) as caught:
		parse(Source("unit.situ", text))
	assert "`max_bits` decides it" in repr(caught.value.diagnostic) \
		or "max_bytes" in str(caught.value)
