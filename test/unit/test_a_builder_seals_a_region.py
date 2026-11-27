"""A builder seals a region through the caller's AEAD (26.1192).

Every struct with a `sealed` region was refused a builder ("sealing is rung
3's"), so a sender wrote an encrypted packet by hand while its receiver had
a generated reader -- fuzznet's `fzn_seal_build` is that hand-written half.
The builder now takes the plaintext and a seal: the caller's primitive and
key behind a context situ never reads, as `_open` takes the caller's
verdict. situ says which bytes are which -- the nonce the region names, the
tag's coverage before the region as additional data, the region, the tag's
slot -- and the seal encrypts in place and writes the tag.

The seal here is OpenSSL's AES-128-GCM, and what it wrote is opened by
Python's `cryptography`, a second implementation, with the additional data
and nonce taken from the bytes as the reader places them: so the division
is checked against a real cipher rather than against this file's idea of
one. DTLS's record names its key by `epoch`, which the seal is handed.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
HOST_CC = shutil.which("gcc") or shutil.which("cc")

try:
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:			# pragma: no cover - the host decides
	AESGCM = None			# type: ignore[assignment,misc]

KEY   = bytes(range(16))
NONCE = bytes(range(0x40, 0x4C))
BODY  = b"attack at dawn"

C_SEAL = """#include <stdio.h>
#include <string.h>
#include <openssl/evp.h>
#include "%s_build.h"
static const uint8_t key[16] = {%s};
static int refuse;
static uint64_t seen_key;
static int seen_has;
static uint32_t seen_aad;
static int seal(void *ctx, const situ_sealing_t *s)
{
	EVP_CIPHER_CTX *c = EVP_CIPHER_CTX_new();
	int n = 0, ok;

	(void)ctx;
	seen_key = s->key_id;
	seen_has = s->has_key_id;
	seen_aad = s->aad_len;
	if (refuse || c == NULL) {
		EVP_CIPHER_CTX_free(c);
		return -1;
	}
	ok = EVP_EncryptInit_ex(c, EVP_aes_128_gcm(), NULL, NULL, NULL)
	     && EVP_CIPHER_CTX_ctrl(c, EVP_CTRL_GCM_SET_IVLEN,
	                            (int)s->nonce_len, NULL)
	     && EVP_EncryptInit_ex(c, NULL, NULL, key, s->nonce)
	     && (s->aad_len == 0
	         || EVP_EncryptUpdate(c, NULL, &n, s->aad, (int)s->aad_len))
	     && EVP_EncryptUpdate(c, s->text, &n, s->text, (int)s->text_len)
	     && EVP_EncryptFinal_ex(c, s->text + n, &n)
	     && EVP_CIPHER_CTX_ctrl(c, EVP_CTRL_GCM_GET_TAG, (int)s->tag_len,
	                            s->tag);
	EVP_CIPHER_CTX_free(c);
	return ok ? 0 : -1;
}
static uint8_t out[256];
static void show(situ_err_t e, uint32_t n)
{
	printf("%%d %%llu %%d %%u", (int)e, (unsigned long long)seen_key,
	       seen_has, seen_aad);
	for (uint32_t i = 0; e == SITU_OK && i < n; i++) printf(" %%02x", out[i]);
	puts("");
}
"""

PACKET_MAIN = """
static const uint8_t hdr[] = {%s};
static const uint8_t nonce[12] = {%s};
static const uint8_t plain[] = {%s};
int main(void)
{
	uint32_t n = 0;
	situ_err_t e = situ_packet_build(out, sizeof out, 7, hdr, sizeof hdr,
	                                 nonce, plain, sizeof plain, seal, NULL,
	                                 &n);
	show(e, n);
	/* One byte short of what `hdr.length` says. */
	e = situ_packet_build(out, sizeof out, 7, hdr, sizeof hdr, nonce, plain,
	                      sizeof plain - 1, seal, NULL, &n);
	show(e, 0);
	for (uint32_t i = 0; i < sizeof out; i++) {
		if (memcmp(out + i, "attack", 6) == 0) puts("plaintext left");
	}
	refuse = 1;
	show(situ_packet_build(out, sizeof out, 7, hdr, sizeof hdr, nonce,
	                       plain, sizeof plain, seal, NULL, &n), 0);
	for (uint32_t i = 0; i < sizeof out; i++) {
		if (memcmp(out + i, "attack", 6) == 0) puts("plaintext left");
	}
	show(situ_packet_build(out, sizeof out, 7, hdr, sizeof hdr, nonce,
	                       plain, sizeof plain, NULL, NULL, &n), 0);
	return 0;
}
"""

DTLS_MAIN = """
static const uint8_t sequence[6] = {0, 0, 0, 0, 0, 9};
static const uint8_t nonce[8] = {%s};
static const uint8_t plain[] = {%s};
int main(void)
{
	uint32_t n = 0;
	situ_err_t e = situ_record_build(out, sizeof out, 23, 0xFEFD, 3,
	                                 sequence, (uint16_t)(sizeof plain + 24),
	                                 nonce, plain, sizeof plain, seal, NULL,
	                                 &n);
	show(e, n);
	return 0;
}
"""


def _bytes(data: bytes) -> str:
	return ", ".join(str(one) for one in data)


def _run(stem: str, main: str, tmp_path: Path,
		schema: Path | None = None) -> list[list[str]]:
	schema = schema or ROOT / "example" / stem / f"{stem}.situ"
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "c", "--layer", "edit", str(schema), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True, timeout=300)
	assert done.returncode == 0, done.stderr[-2000:]
	assert "no builder for" not in done.stderr, done.stderr
	(tmp_path / "main.c").write_text(C_SEAL % (stem, _bytes(KEY)) + main)
	assert HOST_CC is not None
	made = subprocess.run([HOST_CC, "-std=c11", "-O1", "-Wall", "-Wextra",
	                       "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c",
	                       f"{stem}.c", str(RUNTIME / "c" / "situ.c"),
	                       "-lcrypto", "-o", "t"],
	                      cwd=tmp_path, capture_output=True, text=True,
	                      timeout=300)
	if made.returncode != 0 and "openssl/evp.h" in made.stderr:
		pytest.skip("no OpenSSL headers")
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run(["./t"], cwd=tmp_path, capture_output=True,
	                     text=True, timeout=60)
	assert ran.returncode == 0, ran.stderr
	return [line.split() for line in ran.stdout.splitlines()]


@pytest.mark.skipif(HOST_CC is None or AESGCM is None,
                    reason="needs a C compiler and `cryptography`")
def test_a_packet_is_sealed_and_opens(tmp_path: Path) -> None:
	hdr = struct.pack(">BBHI", 1, 2, len(BODY), 41)
	plain = struct.pack(">HI", 5, 6) + bytes(range(16)) + BODY
	lines = _run("packet", PACKET_MAIN % (_bytes(hdr), _bytes(NONCE),
	                                      _bytes(plain)), tmp_path)
	ok, constraint, tag = "0", "2", "4"
	assert [line[0] for line in lines] == [ok, constraint, tag, tag], lines

	built = bytes(int(one, 16) for one in lines[0][4:])
	# hop, three reserved, the header and nonce under the tag, the region.
	assert built[:4] == b"\x07\x00\x00\x00"
	assert built[4:24] == hdr + NONCE
	sealed, sealed_tag = built[24:-16], built[-16:]
	assert len(sealed) == len(plain) and sealed != plain
	assert lines[0][3] == "20"		# the additional data: hdr, nonce
	opened = AESGCM(KEY).decrypt(NONCE, sealed + sealed_tag, built[4:24])
	assert opened == plain
	# A flipped header byte fails the tag: it is under it.
	flipped = bytearray(built[4:24])
	flipped[3] ^= 1
	with pytest.raises(Exception):
		AESGCM(KEY).decrypt(NONCE, sealed + sealed_tag, bytes(flipped))


@pytest.mark.skipif(HOST_CC is None or AESGCM is None,
                    reason="needs a C compiler and `cryptography`")
def test_a_dtls_record_is_sealed_under_its_epoch(tmp_path: Path) -> None:
	explicit = NONCE[:8]
	lines = _run("dtls", DTLS_MAIN % (_bytes(explicit), _bytes(BODY)),
	             tmp_path)
	assert lines[0][:4] == ["0", "3", "1", "0"], lines
	built = bytes(int(one, 16) for one in lines[0][4:])
	assert built[:13] == bytes([23, 0xFE, 0xFD, 0, 3, 0, 0, 0, 0, 0, 9]) + \
		struct.pack(">H", len(BODY) + 24)
	assert built[13:21] == explicit
	opened = AESGCM(KEY).decrypt(explicit, built[21:], b"")
	assert opened == BODY


HEADED = """target buffer;
endian big;
codec gcm {
	tag_bytes   = 16;
	nonce_bytes = 12;
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl gcm extern "app_gcm";
struct head {
	u8   kind;
	u16  length;
	u8   nonce[12];
}
struct headed {
	authenticated {
		head  h;
	}
	sealed(gcm, nonce = h.nonce) {
		u8  body[h.length];
	}
	tag u8[16];
}
"""

HEADED_MAIN = """
static const uint8_t head[] = {%s};
static const uint8_t plain[] = {%s};
int main(void)
{
	uint32_t n = 0;
	situ_err_t e = situ_headed_build(out, sizeof out, head, sizeof head,
	                                 plain, sizeof plain, seal, NULL, &n);
	show(e, n);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None or AESGCM is None,
                    reason="needs a C compiler and `cryptography`")
def test_a_nonce_inside_the_header_is_found(tmp_path: Path) -> None:
	"""fuzznet's frame: `nonce = head.nonce`, inside the authenticated
	header the caller builds. The seal is handed those bytes, reached
	through the nested member's view."""
	schema = tmp_path / "headed.situ"
	schema.write_text(HEADED, encoding="ascii")
	head = bytes([9]) + struct.pack(">H", len(BODY)) + NONCE
	lines = _run("headed", HEADED_MAIN % (_bytes(head), _bytes(BODY)),
	             tmp_path, schema)
	assert lines[0][:4] == ["0", "0", "0", str(len(head))], lines
	built = bytes(int(one, 16) for one in lines[0][4:])
	assert built[:len(head)] == head
	opened = AESGCM(KEY).decrypt(NONCE, built[len(head):], head)
	assert opened == BODY
