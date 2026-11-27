"""HTTP/2's frames, as tshark reads them (26.1182).

`example/http2`'s vectors are curl's client side of a connection and two
frames written from RFC 9113 around curl's field block, one padded DATA and
one padded, prioritized HEADERS. tshark's HTTP/2 dissector reads them inside
a TCP stream, which text2pcap builds from the bytes, and situ's frames have
to say what it says: each frame's type, length, pad length and weight, and
the data of each DATA frame.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT   = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "example" / "http2" / "http2.situ"
HAVE   = shutil.which("tshark") is not None and shutil.which("text2pcap") is not None


def _vector(name: str) -> bytes:
	for line in (ROOT / "example" / "http2" / "http2.vectors").read_text().splitlines():
		words = line.split()
		if len(words) > 2 and words[1] == name:
			return bytes.fromhex("".join(words[2:]))
	raise KeyError(name)


def _stream() -> bytes:
	"""curl's connection, then the two RFC frames on stream 3."""
	return _vector("curl") + _vector("padded_headers") + _vector("padded_data")


def _tshark(data: bytes, tmp: Path) -> tuple[list[tuple[int, int]], list[str],
		list[str], list[str]]:
	"""Each frame's type and length; every pad length, weight and DATA's data."""
	dump = tmp / "c.od"
	dump.write_text("".join(f"{at:06x} " + " ".join(f"{b:02x}" for b in data[at:at + 16])
	                        + "\n" for at in range(0, len(data), 16)))
	subprocess.run(["text2pcap", "-q", "-T", "40000,80", str(dump), str(tmp / "c.pcap")],
	               check=True, timeout=60)
	said = subprocess.run(["tshark", "-r", str(tmp / "c.pcap"), "-d", "tcp.port==80,http2",
	                       "-T", "fields", "-E", "separator=;", "-E", "occurrence=a",
	                       "-E", "aggregator=,", "-e", "http2.type", "-e", "http2.length",
	                       "-e", "http2.pad_length", "-e", "http2.headers.weight",
	                       "-e", "http2.data.data"],
	                      capture_output=True, text=True, check=True, timeout=120).stdout
	kinds, lengths, pads, weights, datas = (part.split(",") if part else []
	                                        for part in said.strip().split(";"))
	return list(zip(map(int, kinds), map(int, lengths))), pads, weights, datas


def _situ(tmp: Path) -> object:
	out = tmp / "py"
	subprocess.run([sys.executable, "-m", "situc", "build", "--target", "python", str(SCHEMA),
	                "--out", str(out)], cwd=ROOT, check=True, capture_output=True, timeout=300)
	sys.path.insert(0, str(ROOT / "runtime" / "python"))
	spec = importlib.util.spec_from_file_location("http2_oracle", out / "http2.py")
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.mark.skipif(not HAVE, reason="no tshark and text2pcap")
def test_situ_reads_the_frames_tshark_reads(tmp_path: Path) -> None:
	data = _stream()
	frames, pads, weights, datas = _tshark(data, tmp_path)
	module = _situ(tmp_path)
	from situ_runtime import Message  # type: ignore[import-not-found]

	whole = module.connection.at(Message(bytearray(data)), 0, len(data))   # type: ignore[attr-defined]
	whole.validate()
	ours, our_pads, our_weights, our_datas = [], [], [], []
	for index in range(whole.frames_count):
		one = whole.frames(index)
		ours.append((one.type, one.length))
		# tshark names a pad length for every DATA and HEADERS, 0 where the
		# frame is not padded.
		if one.type == 0 and one.padded:
			body = one.payload_data.body_with_padding
			our_pads.append(str(body.pad_length))
			our_datas.append(bytes(body.data).hex())
		elif one.type == 0:
			our_pads.append("0")
			our_datas.append(bytes(one.payload_data.body_bytes).hex())
		elif one.type == 1 and one.padded:
			padded = one.payload_headers.body_with_padding
			our_pads.append(str(padded.pad_length))
			if one.priority:
				our_weights.append(str(padded.headers.body_prioritized.fields.weight))
		elif one.type == 1:
			our_pads.append("0")
	# curl's six frames and the two written here: a sweep that read none
	# would agree with anything.
	assert len(frames) == 8 and len(pads) == 4 and weights == ["15"]
	assert ours == frames
	assert our_pads == pads and our_weights == weights
	assert our_datas == [d.replace(":", "") for d in datas]
