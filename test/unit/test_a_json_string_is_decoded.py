"""The `json_string` stuffing code, held against Python's `json` (26.892).

situ frames a string and did not decode it, and netcfgd's hottest member is a
JSON string holding a whole configuration file. `json` is the oracle: it was
written by nobody here. Where it is lenient the grammar decides, a lone
surrogate being the case: RFC 8259 allows the text and no reader can do
anything with it, and this code refuses it rather than emit broken UTF-8.
"""

from __future__ import annotations

import ctypes
import importlib
import json
import random
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
codec json_string {
	kernel = stuffing(worst_case = 6, per = 1, code = json_string);
}
impl json_string derived;
struct s { u8 n; coded body (json_string) { u8 text[n]; } }
"""

Codec = Callable[[bytes], bytes]


def texts() -> list[str]:
	picked = random.Random(892)
	alphabet = ('ab"\\/\b\f\n\r\t\x00\x01\x1f\x7f'
	            "éࠀ￿\U0001f600\U0010ffff ")
	found = ["", "plain", 'say "hi"', "back\\slash", "\x00", "\U0001f600"]
	found += ["".join(picked.choices(alphabet, k=picked.randrange(1, 30)))
	          for _ in range(300)]
	return found


#: What the grammar refuses, each of them a mistake a reader makes.
MALFORMED = [
	b'\\', b'\\q', b'\\u', b'\\u12', b'\\u12G4', b'"', b'\x01', b'\n',
	b'\\uD83D', b'\\uD83Dx', b'\\uD83D\\n', b'\\uD83D\\u0041', b'\\uDE00',
	b'\\uD83D\\uD83D', b'ok\\', b'ok\\uD83D',
]


def derived(out: Path, target: str, name: str) -> Path:
	"""`gen-derived` for one language, and the file it wrote."""
	source = out / "js.situ"
	source.write_text(SCHEMA, encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "gen-derived", str(source),
	                "--target", target, "--out", str(out)],
	               check=True, capture_output=True, cwd=ROOT)
	return out / name


@pytest.fixture(scope="module")
def python(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
	out = tmp_path_factory.mktemp("json")
	derived(out, "python", "js_derived.py")
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	try:
		return importlib.import_module("js_derived")
	finally:
		del sys.path[:2]
		sys.modules.pop("js_derived", None)


def py_encode(built: ModuleType, data: bytes) -> bytes:
	out = bytearray(len(data) * 6)
	return bytes(out[:built.json_string_encode(data, len(data), out)])


def py_decode(built: ModuleType, data: bytes) -> bytes:
	out = bytearray(len(data))
	return bytes(out[:built.json_string_decode(data, len(data), out)])


@pytest.fixture(scope="module")
def c_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
	out = tmp_path_factory.mktemp("jsonc")
	source = derived(out, "c", "js_derived.c")
	subprocess.run([sys.executable, "-m", "situc", "build", str(out / "js.situ"),
	                "--target", "c", "--out", str(out)],
	               check=True, capture_output=True, cwd=ROOT)
	library = out / "js.so"
	subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
	                "-I", str(ROOT / "runtime" / "c"), "-I", str(out),
	                "-o", str(library), str(source)],
	               check=True, capture_output=True)
	return ctypes.CDLL(str(library))


def c_call(library: ctypes.CDLL, name: str, data: bytes, room: int) -> bytes:
	function = getattr(library, name)
	function.restype = ctypes.c_uint32
	out = ctypes.create_string_buffer(max(room, 1))
	wrote = function(data, ctypes.c_uint32(len(data)), out)
	return out.raw[:wrote]


@pytest.mark.parametrize("text", texts()[:40])
def test_python_encodes_as_json_does(python: ModuleType, text: str) -> None:
	wire = json.dumps(text, ensure_ascii=False)[1:-1].encode()
	assert py_encode(python, text.encode()) == wire


def test_every_text_round_trips_in_python(python: ModuleType) -> None:
	for text in texts():
		raw = text.encode()
		assert py_decode(python, py_encode(python, raw)) == raw


def test_python_decodes_what_json_writes_with_ascii_escapes(
		python: ModuleType) -> None:
	"""`ensure_ascii` is every `\\uXXXX` and every surrogate pair."""
	for text in texts():
		wire = json.dumps(text, ensure_ascii=True)[1:-1].encode()
		assert py_decode(python, wire) == text.encode(), wire


def test_c_agrees_with_python(python: ModuleType, c_library: ctypes.CDLL) -> None:
	for text in texts():
		raw  = text.encode()
		wire = json.dumps(text, ensure_ascii=True)[1:-1].encode()
		assert c_call(c_library, "situ_json_string_encode", raw,
		              len(raw) * 6) == py_encode(python, raw)
		assert c_call(c_library, "situ_json_string_decode", wire,
		              len(wire)) == raw


@pytest.mark.parametrize("wire", MALFORMED)
def test_a_malformed_string_answers_zero(python: ModuleType,
		c_library: ctypes.CDLL, wire: bytes) -> None:
	"""Zero from non-empty input is the refusal: every valid escape yields."""
	assert py_decode(python, wire) == b""
	assert c_call(c_library, "situ_json_string_decode", wire, len(wire)) == b""


def test_decoding_never_grows(python: ModuleType) -> None:
	"""The wrapper hands the decoder a buffer as long as the wire form."""
	for text in texts():
		wire = json.dumps(text, ensure_ascii=True)[1:-1].encode()
		assert len(py_decode(python, wire)) <= len(wire)


@pytest.mark.skipif(shutil.which("rustc") is None, reason="needs rustc")
def test_rust_agrees_with_python(tmp_path: Path, python: ModuleType) -> None:
	"""A line of hex in, its decoding and its encoding out."""
	source = derived(tmp_path, "rust", "js_derived.rs")
	source.write_text(source.read_text(encoding="ascii") + RUST_MAIN,
	                  encoding="ascii")
	binary = tmp_path / "js"
	subprocess.run(["rustc", "-A", "warnings", "-o", str(binary), str(source)],
	               check=True, capture_output=True)
	raws   = [text.encode() for text in texts()]
	wires  = [json.dumps(text)[1:-1].encode() for text in texts()]
	inputs = [*wires, *MALFORMED]
	feed   = "".join(f"{one.hex()}\n" for one in inputs)
	said   = subprocess.run([str(binary)], input=feed, text=True, check=True,
	                        capture_output=True).stdout.splitlines()
	assert len(said) == len(inputs)
	for wire, line in zip(inputs, said):
		decoded, encoded = (line.split(" ") + [""])[:2]
		assert bytes.fromhex(decoded) == py_decode(python, wire), wire
		assert bytes.fromhex(encoded) == py_encode(python, wire), wire
	assert raws


RUST_MAIN = """
fn main() {
	use std::io::BufRead;
	for line in std::io::stdin().lock().lines() {
		let hex = line.unwrap();
		let data: Vec<u8> = (0..hex.len() / 2)
			.map(|at| u8::from_str_radix(&hex[at * 2..at * 2 + 2], 16).unwrap())
			.collect();
		let mut back = vec![0u8; data.len()];
		let mut wide = vec![0u8; data.len() * 6];
		let got = json_string_decode(&data, data.len(), &mut back);
		let put = json_string_encode(&data, data.len(), &mut wide);
		let show = |bytes: &[u8]| bytes.iter()
			.map(|one| format!("{one:02x}")).collect::<String>();
		println!("{} {}", show(&back[..got]), show(&wide[..put]));
	}
}
"""
