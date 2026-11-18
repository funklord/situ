"""A variant with a length of its own is that long whatever its arms hold
(26.1096).

openmlx4's MFA: each section's payload is `variant payload[hdr.size]`,
raw bytes or an xz stream, inside the archive's authenticated body and
followed by the CRC32 over it. xz's expansion is unbounded, so the arm had
no length, and the variant was asked of its arms -- which made the section
unmeasurable, the archive's CRC unplaceable, and the check absent from all
four backends. `verify` called the CRC "not checked: no codec computes it"
and passed a flipped byte.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import main

SCHEMA = """target buffer;
endian big;

codec crc32 {
	kernel = polynomial(width = 32, poly = 0x04C11DB7, init = 0xFFFFFFFF,
	                    xorout = 0xFFFFFFFF, reflect);
}
impl crc32 derived;

codec xz {
	expansion = unbounded;
	granularity = stream;
	not seekable;
	invertible;
}
impl xz extern "lzma";

struct xz_payload { coded stream(xz) { u8 data[remaining]; } }

struct section {
	u8 kind [max = 1];
	u8 len;
	variant payload[len] switch (kind) {
		case 0: u8 raw[remaining];
		case 1: xz_payload packed;
		default: error;
	}
}

struct archive {
	authenticated body {
		section one;
	}
	checksum u8 crc[4] covers(body) is crc32;
}
"""


def _schema(tmp_path: Path) -> Path:
	path = tmp_path / "mfa_like.situ"
	path.write_text(SCHEMA, encoding="ascii")
	return path


def test_a_flipped_crc_after_a_coded_arm_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	body = bytes([0, 2, 0xAA, 0xBB])
	crc  = zlib.crc32(body).to_bytes(4, "big")
	bad  = crc[:3] + bytes([crc[3] ^ 1])
	vectors = tmp_path / "v.vectors"
	vectors.write_text(f"archive good {(body + crc).hex(' ')}\n"
	                   f"archive bad {(body + bad).hex(' ')}\n",
	                   encoding="ascii")

	assert main(["verify", str(_schema(tmp_path)), str(vectors)]) == 1
	out = capsys.readouterr().out
	assert "1 of 2 vectors do not conform" in out, out
	assert "ChecksumError: archive.crc" in out, out
	assert "not checked" not in out, out


@pytest.mark.parametrize("target", ["c", "cpp", "rust", "python"])
def test_every_backend_checks_the_crc(tmp_path: Path, target: str) -> None:
	out = tmp_path / target
	out.mkdir()
	assert main(["build", "--target", target, "--layer", "edit",
	             "--out", str(out), str(_schema(tmp_path))]) == 0
	text = "".join(one.read_text(encoding="utf-8")
	               for one in out.iterdir() if one.is_file())
	assert "crc_check" in text
	assert "cannot resolve where the tag sits" not in text
