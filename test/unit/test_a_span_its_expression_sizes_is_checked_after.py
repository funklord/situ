"""A builder writes a byte run its expression sizes, and checks it after
(26.1016).

RTP's `padding[padded_flag * last]` multiplies two fields, the second
being the packet's own last byte, so no field can be solved for the
caller's bytes -- and none needs to be: the caller states the flag and the
bytes, and once the message is whole the reader's `_len` says how long they
have to be. A disagreement is refused. ID3's synchsafe frame size and
QUIC's `length - (number_length + 1)` are the same shape and were refused
the same way. C only, as the other three do not yet build such a run.

The work also found the counted value array refusing a null pointer at a
count of zero: RTP's CSRC list, which is empty in nearly every packet.

Held to RFC 3550 section 5.1, ID3v2.4.0 structure section 4, and RFC 9000
section 17.2.2.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, ROOT,
                                                          _built, _run)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_an_rtp_packet_with_and_without_padding(tmp_path: Path) -> None:
	"""V=2, PT 96, sequence 1, timestamp 160, SSRC 0x12345678, no CSRC and
	no extension, payload `abcd`; then the same padded by three bytes, the
	last of which counts the padding including itself."""
	_run(tmp_path, _built(tmp_path, "rtp"), "rtp", """
	static const uint8_t plain[16] = {
		0x80, 0x60, 0, 1, 0, 0, 0, 0xa0, 0x12, 0x34, 0x56, 0x78,
		'a', 'b', 'c', 'd' };
	static const uint8_t pad3[3] = { 0, 0, 3 }, pad2[3] = { 0, 0, 2 },
	                     pad5[3] = { 0, 0, 5 };
	static const uint8_t payload[4] = { 'a', 'b', 'c', 'd' };
	uint8_t  buf[64];
	uint32_t wrote = 0;

	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        NULL, 0u, &wrote) != SITU_OK
	    || wrote != 16u || memcmp(buf, plain, 16u) != 0) {
		return 1;
	}
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad3, 3u, &wrote) != SITU_OK
	    || wrote != 19u || buf[0] != 0xa0
	    || memcmp(buf + 1, plain + 1, 15u) != 0
	    || memcmp(buf + 16, pad3, 3u) != 0) {
		return 2;
	}
	/* A last byte saying two over three bytes of padding. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad2, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 3;
	}
	/* And saying five: the reader's length stops at the bytes there are,
	 * so this one is caught as the message running short. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 1u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad5, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 6;
	}
	/* Padding with P clear: the reader would read none. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 0u, NULL, 0u, payload, 4u,
	                        pad3, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 4;
	}
	/* A count of one with no array is still refused. */
	if (situ_rtp_build_none(buf, sizeof buf, 2u, 0u, 0u, 96u, 1u, 160u,
	                        0x12345678u, NULL, 1u, NULL, 0u, payload, 4u,
	                        NULL, 0u, &wrote) != SITU_ERR_BOUNDS) {
		return 5;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_an_id3_text_frame(tmp_path: Path) -> None:
	"""TIT2, three bytes of content -- encoding 3 (UTF-8) and `Hi` -- with
	a synchsafe size of 3 and no flags; a size of 4 over them refused."""
	_run(tmp_path, _built(tmp_path, "id3"), "id3", """
	static const uint8_t content[3] = { 3, 'H', 'i' };
	static const uint8_t want[13] = {
		'T', 'I', 'T', '2', 0, 0, 0, 3, 0, 0, 3, 'H', 'i' };
	uint8_t  buf[32];
	uint32_t wrote = 0;

	if (situ_id3_frame_build(buf, sizeof buf, 'T', 'I', 'T', '2',
	                         0u, 0u, 0u, 3u, 0u, 0u, 0u, 0u, 0u, 0u, 0u, 0u,
	                         content, 3u, &wrote) != SITU_OK
	    || wrote != 13u || memcmp(buf, want, 13u) != 0) {
		return 1;
	}
	if (situ_id3_frame_build(buf, sizeof buf, 'T', 'I', 'T', '2',
	                         0u, 0u, 0u, 4u, 0u, 0u, 0u, 0u, 0u, 0u, 0u, 0u,
	                         content, 3u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_quic_initial(tmp_path: Path) -> None:
	"""Version 1, an eight-byte destination ID, no source ID or token, a
	one-byte packet number and sixteen bytes of payload: Length is 17, one
	varint byte. Length 18 over the same bytes refused."""
	_run(tmp_path, _built(tmp_path, "quic"), "quic", """
	static const uint8_t dcid[8] = { 1, 2, 3, 4, 5, 6, 7, 8 };
	static const uint8_t number[1] = { 0x2a };
	uint8_t  payload[16];
	uint8_t  want[34];
	uint8_t  buf[64];
	uint32_t wrote = 0;

	memset(payload, 0x55, sizeof payload);
	want[0] = 0xc0;
	want[1] = 0; want[2] = 0; want[3] = 0; want[4] = 1;
	want[5] = 8;
	memcpy(want + 6, dcid, 8u);
	want[14] = 0; want[15] = 0; want[16] = 17; want[17] = 0x2a;
	memcpy(want + 18, payload, 16u);
	if (situ_initial_build(buf, sizeof buf, 1u, 1u, 0u, 0u, 1u, dcid, 8u,
	                       NULL, 0u, NULL, 0u, 17u, number, 1u,
	                       payload, 16u, &wrote) != SITU_OK
	    || wrote != 34u || memcmp(buf, want, 34u) != 0) {
		return 1;
	}
	if (situ_initial_build(buf, sizeof buf, 1u, 1u, 0u, 0u, 1u, dcid, 8u,
	                       NULL, 0u, NULL, 0u, 18u, number, 1u,
	                       payload, 16u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")



@pytest.mark.parametrize("target", ["python", "cpp", "rust"])
def test_the_other_three_refuse_it(tmp_path: Path, target: str) -> None:
	"""They build from C's plan and would write the span unchecked: Python
	returned a packet for a last byte of 2 over three bytes of padding, and
	for padding with P clear. Refused until each checks the length."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "rtp" / "rtp.situ"), "--target", target,
		 "--layer", "edit", "--out", str(tmp_path)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	assert "no builder for `rtp.none`: `padding`" in done.stderr, done.stderr
