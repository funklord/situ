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
from typing import Any

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


# All four, with one toy seal written in each language: the same bytes or
# the same refusal, for a packet, a DTLS record under its epoch, and the
# nonce inside a header. The toy is no cipher; it touches every input the
# seal is handed, so a language handing different bytes says otherwise.

def _toy(key_id: int, nonce: bytes, aad: bytes, text: bytes,
		tag_len: int) -> tuple[bytes, bytes]:
	out = bytes(byte ^ (nonce[i % len(nonce)] if nonce else 0)
	            ^ (key_id & 0xFF) ^ (i & 0xFF) for i, byte in enumerate(text))
	total = (sum(aad) + sum(out)) & 0xFFFFFFFF
	return out, bytes((total + 7 * j) & 0xFF for j in range(tag_len))


TOY_C = """
static int toy(void *ctx, const situ_sealing_t *s)
{
	uint32_t total = 0;
	if (ctx != NULL) return -1;
	for (uint32_t i = 0; i < s->text_len; i++) {
		s->text[i] ^= (uint8_t)((s->nonce_len ? s->nonce[i % s->nonce_len] : 0)
		                        ^ (uint8_t)s->key_id ^ (uint8_t)i);
		total += s->text[i];
	}
	for (uint32_t i = 0; i < s->aad_len; i++) total += s->aad[i];
	for (uint32_t j = 0; j < s->tag_len; j++) s->tag[j] = (uint8_t)(total + 7u * j);
	return 0;
}
"""

FOUR: dict[str, dict[str, Any]] = {
	"packet": dict(
		args=lambda: [7, struct.pack(">BBHI", 1, 2, len(BODY), 41), NONCE,
		              struct.pack(">HI", 5, 6) + bytes(range(16)) + BODY]),
	"dtls": dict(
		args=lambda: [23, 0xFEFD, 3, bytes([0, 0, 0, 0, 0, 9]),
		              len(BODY) + 24, NONCE[:8], BODY]),
	"headed": dict(
		args=lambda: [bytes([9]) + struct.pack(">H", len(BODY)) + NONCE,
		              BODY]),
	# A MAC over authenticated bytes, no region: the length is computed,
	# so a shorter payload is another message rather than a refusal.
	"signed_record": dict(args=lambda: [9, BODY], codes=["0", "0", "4"]),
}

SIGNED = """target buffer;
endian big;
struct signed_record {
	authenticated body {
		u8   kind;
		u16  length;
		u8   payload[length];
	}
	tag u8  mac[32] covers(body);
}
"""

WRITTEN = {"headed": HEADED, "signed_record": SIGNED}


def _c_list(data: bytes) -> str:
	return "{" + ", ".join(str(one) for one in data) + "}"


def _drivers(stem: str, args: list[int | bytes]) -> dict[str, str]:
	"""One good case, one a byte short, one the seal refuses, per language."""
	plain = args[-1]
	assert isinstance(plain, bytes)
	short = [*args[:-1], plain[:-1]]
	struct_name = {"packet": "packet", "dtls": "record"}.get(stem, stem)

	def c_args(given: list[int | bytes], cpp: bool) -> str:
		out = []
		for i, one in enumerate(given):
			if isinstance(one, int):
				out.append(str(one))
			elif i == len(given) - 1 or (stem == "packet" and i == 1) \
					or stem in WRITTEN:
				out.append(f"a{i}, {len(one)}u")
			else:
				out.append(f"a{i}")
		return ", ".join(out)

	decls = "\n".join(f"static const uint8_t a{i}[] = {_c_list(one)};"
	                  for i, one in enumerate(args) if isinstance(one, bytes))
	show = """
static void show(int e, const uint8_t *out, uint32_t n)
{
	printf("%d", e);
	for (uint32_t i = 0; e == 0 && i < n; i++) printf(" %02x", out[i]);
	puts("");
}
"""
	c = (f'#include <stdio.h>\n#include "{stem}_build.h"\n{TOY_C}\n{decls}\n'
	     f"{show}\nstatic uint8_t out[256];\nint main(void)\n{{\n"
	     "\tuint32_t n = 0;\n\tint e;\n"
	     f"\te = situ_{struct_name}_build(out, sizeof out, "
	     f"{c_args(args, False)}, toy, NULL, &n);\n\tshow(e, out, n);\n"
	     f"\te = situ_{struct_name}_build(out, sizeof out, "
	     f"{c_args(args, False).replace(f'{len(plain)}u', f'{len(plain) - 1}u')}"
	     ", toy, NULL, &n);\n\tshow(e, out, n);\n"
	     f"\te = situ_{struct_name}_build(out, sizeof out, "
	     f"{c_args(args, False)}, toy, out, &n);\n\tshow(e, out, 0);\n"
	     "\treturn 0;\n}\n")
	cpp = (f'#include <cstdio>\n#include "{stem}_build.hpp"\n'
	       + TOY_C.replace("NULL", "nullptr") + f"\n{decls}\n{show}\n"
	       "static uint8_t out[256];\nint main()\n{\n"
	       "\tstd::uint32_t n = 0;\n"
	       f"\tint e = static_cast<int>(situ_build::{struct_name}(out, sizeof out, "
	       f"{c_args(args, True)}, toy, nullptr, n));\n\tshow(e, out, n);\n"
	       f"\te = static_cast<int>(situ_build::{struct_name}(out, sizeof out, "
	       f"{c_args(args, True).replace(f'{len(plain)}u', f'{len(plain) - 1}u')}"
	       ", toy, nullptr, n));\n\tshow(e, out, n);\n"
	       f"\te = static_cast<int>(situ_build::{struct_name}(out, sizeof out, "
	       f"{c_args(args, True)}, toy, out, n));\n\tshow(e, out, 0);\n"
	       "\treturn 0;\n}\n")

	def rust_args(given: list[int | bytes]) -> str:
		out = []
		for i, one in enumerate(given):
			if isinstance(one, int):
				out.append(str(one))
			elif i == len(given) - 1 or (stem == "packet" and i == 1) \
					or stem in WRITTEN:
				out.append(f"&{_c_list(one).replace('{', '[').replace('}', ']')}[..]")
			else:
				out.append(f"&{_c_list(one).replace('{', '[').replace('}', ']')}")
		return ", ".join(out)

	rust = (f"mod situ_rt;\n#[allow(dead_code)] mod {stem};\n"
	        f"#[allow(dead_code)] mod {stem}_build;\n"
	        "fn toy(s: &mut situ_rt::Sealing<'_>) -> bool {\n"
	        "\tlet mut total: u32 = 0;\n"
	        "\tlet k = s.key_id.unwrap_or(0) as u8;\n"
	        "\tfor i in 0..s.text.len() {\n"
	        "\t\tlet n = if s.nonce.is_empty() { 0 } else "
	        "{ s.nonce[i % s.nonce.len()] };\n"
	        "\t\ts.text[i] ^= n ^ k ^ (i as u8);\n"
	        "\t\ttotal = total.wrapping_add(u32::from(s.text[i]));\n\t}\n"
	        "\tfor b in s.aad { total = total.wrapping_add(u32::from(*b)); }\n"
	        "\tfor j in 0..s.tag.len() { s.tag[j] = "
	        "total.wrapping_add(7 * j as u32) as u8; }\n\ttrue\n}\n"
	        "fn show(r: Result<usize, situ_rt::Error>, out: &[u8]) {\n"
	        "\tmatch r {\n\t\tOk(n) => { print!(\"0\"); "
	        "for b in &out[..n] { print!(\" {:02x}\", b); } println!(); }\n"
	        "\t\tErr(situ_rt::Error::Constraint) => println!(\"2\"),\n"
	        "\t\tErr(situ_rt::Error::Tag) => println!(\"4\"),\n"
	        "\t\tErr(_) => println!(\"99\"),\n\t}\n}\n"
	        "fn main() {\n\tlet mut out = [0u8; 256];\n"
	        f"\tlet r = {stem}_build::build_{struct_name}(&mut out, "
	        f"{rust_args(args)}, &mut toy);\n\tshow(r, &out);\n"
	        f"\tlet r = {stem}_build::build_{struct_name}(&mut out, "
	        f"{rust_args(short)}, &mut toy);\n\tshow(r, &out);\n"
	        f"\tlet r = {stem}_build::build_{struct_name}(&mut out, "
	        f"{rust_args(args)}, &mut |_s| false);\n\tshow(r, &out);\n"
	        "}\n")
	return {"c": c, "cpp": cpp, "rust": rust}


def _python_says(directory: Path, stem: str,
		args: list[int | bytes]) -> str:
	sys.path[:0] = [str(directory), str(RUNTIME / "python")]
	try:
		import importlib
		built = importlib.import_module(f"{stem}_build")
		runtime = importlib.import_module("situ_runtime")
	finally:
		del sys.path[:2]

	def toy(s: Any) -> None:
		text = bytes(s.text)
		sealed, tag = _toy(s.key_id or 0, s.nonce or b"",
		                   s.aad, text, len(s.tag))
		s.text[:] = sealed
		s.tag[:] = tag

	def refuse(s: Any) -> None:
		raise RuntimeError("refused")

	codes = {runtime.ConstraintError: 2, runtime.TagError: 4}
	lines = []
	name = {"packet": "packet", "dtls": "record"}.get(stem, stem)
	plain = args[-1]
	assert isinstance(plain, bytes)
	for given, seal in ((args, toy), ([*args[:-1], plain[:-1]], toy),
	                    (args, refuse)):
		out = bytearray(256)
		try:
			n = getattr(built, f"build_{name}")(out, *given, seal)
			lines.append("0" + "".join(f" {one:02x}" for one in out[:n]))
		except runtime.SituError as refused:
			lines.append(str(codes.get(type(refused), 99)))
	return "\n".join(lines) + "\n"


@pytest.mark.parametrize("stem", sorted(FOUR))
def test_four_builders_seal_alike(stem: str, tmp_path: Path) -> None:
	sys.path.insert(0, str(ROOT / "test" / "unit"))
	import fourway
	if not fourway.COMPLETE:
		pytest.skip("needs gcc, g++ and rustc")
	schema = ROOT / "example" / stem / f"{stem}.situ"
	if stem in WRITTEN:
		schema = tmp_path / f"{stem}.situ"
		schema.write_text(WRITTEN[stem], encoding="ascii")
	args = FOUR[stem]["args"]()
	said: dict[str, str] = {}
	drivers = _drivers(stem, args)
	for target in ("c", "cpp", "rust", "python"):
		out = tmp_path / target
		out.mkdir()
		done = subprocess.run([sys.executable, "-m", "situc", "build",
		                       "--target", target, "--layer", "edit",
		                       str(schema), "--out", str(out)], cwd=ROOT,
		                      capture_output=True, text=True, timeout=300)
		assert done.returncode == 0, done.stderr[-2000:]
		assert "no builder for" not in done.stderr, done.stderr
	said["python"] = _python_says(tmp_path / "python", stem, args)
	c, cpp, rust = (tmp_path / one for one in ("c", "cpp", "rust"))
	(c / "main.c").write_text(drivers["c"])
	(cpp / "main.cpp").write_text(drivers["cpp"])
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", rust)
	rt = rust / "situ_rt.rs"
	rt.write_text(rt.read_text().replace("#![no_std]\n", ""))
	(rust / "main.rs").write_text(drivers["rust"])
	for argv, where in (
			([fourway.HOST_CC or "cc", "-std=c11", "-Wall", "-Wextra",
			  "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c", f"{stem}.c",
			  str(RUNTIME / "c" / "situ.c"), "-o", "probe"], c),
			([fourway.HOST_CXX or "c++", "-std=c++17", "-Wall", "-Wextra",
			  "-Werror", f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.",
			  "main.cpp", str(RUNTIME / "c" / "situ.c"), "-o", "probe"], cpp),
			([fourway.RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
			  "-A", "dead_code", "main.rs", "-o", "probe"], rust)):
		made = subprocess.run(argv, cwd=where, capture_output=True,
		                      text=True, timeout=300)
		assert made.returncode == 0, made.stderr[-3000:]
		ran = subprocess.run(["./probe"], cwd=where, capture_output=True,
		                     text=True, timeout=60)
		assert ran.returncode == 0, ran.stderr
		said[where.name] = ran.stdout
	assert len(set(said.values())) == 1, said
	lines = said["python"].splitlines()
	assert [line.split()[0] for line in lines] == \
		FOUR[stem].get("codes", ["0", "2", "4"]), lines
	# And the toy as this file computes it, from the bytes the reader
	# places: so four agreeing is not four wrong alike.
	built = bytes(int(one, 16) for one in lines[0].split()[1:])
	if stem == "packet":
		aad, nonce, key, at = built[4:24], NONCE, 0, 24
	elif stem == "headed":
		aad, nonce, key, at = built[:15], NONCE, 0, 15
	elif stem == "signed_record":
		assert built[:3 + len(BODY)] == bytes([9, 0, len(BODY)]) + BODY
		assert built[3 + len(BODY):] == _toy(0, b"", built[:-32], b"", 32)[1]
		return
	else:
		aad, nonce, key, at = b"", NONCE[:8], 3, 21
	plain = args[-1]
	assert isinstance(plain, bytes)
	assert built[at:] == b"".join(_toy(key, nonce, aad, plain, 16))


HMAC_MAIN = """
#include <openssl/hmac.h>
static int mac(void *ctx, const situ_sealing_t *s)
{
	unsigned int n = 0;
	(void)ctx;
	if (s->text_len != 0 || s->nonce != NULL || s->tag_len != 32) return -1;
	return HMAC(EVP_sha256(), key, sizeof key, s->aad, s->aad_len, s->tag,
	            &n) != NULL && n == 32 ? 0 : -1;
}
static const uint8_t payload[] = {%s};
int main(void)
{
	uint32_t n = 0;
	situ_err_t e = situ_signed_record_build(out, sizeof out, 9, payload,
	                                        sizeof payload, mac, NULL, &n);
	show(e, n);
	(void)seal;
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="needs a C compiler")
def test_a_mac_is_written_by_the_callers_hmac(tmp_path: Path) -> None:
	"""A tag over authenticated bytes and no sealed region: a MAC or a
	signature, which names no codec, so it had no builder (26.1193). The
	caller's seal is handed the coverage as additional data and nothing to
	encrypt; here it is OpenSSL's HMAC-SHA256, checked by Python's."""
	import hashlib
	import hmac
	schema = tmp_path / "signed_record.situ"
	schema.write_text(SIGNED, encoding="ascii")
	lines = _run("signed_record", HMAC_MAIN % _bytes(BODY), tmp_path, schema)
	assert lines[0][0] == "0", lines
	built = bytes(int(one, 16) for one in lines[0][4:])
	body = bytes([9, 0, len(BODY)]) + BODY
	assert built[:len(body)] == body
	assert built[len(body):] == hmac.new(KEY, body, hashlib.sha256).digest()


UNSEAL_MAIN = """
static int open_gcm(void *ctx, const situ_sealing_t *s)
{
	EVP_CIPHER_CTX *c = EVP_CIPHER_CTX_new();
	int n = 0, ok;
	(void)ctx;
	if (c == NULL) return -1;
	ok = EVP_DecryptInit_ex(c, EVP_aes_128_gcm(), NULL, NULL, NULL)
	     && EVP_CIPHER_CTX_ctrl(c, EVP_CTRL_GCM_SET_IVLEN,
	                            (int)s->nonce_len, NULL)
	     && EVP_DecryptInit_ex(c, NULL, NULL, key, s->nonce)
	     && (s->aad_len == 0
	         || EVP_DecryptUpdate(c, NULL, &n, s->aad, (int)s->aad_len))
	     && EVP_DecryptUpdate(c, s->text, &n, s->text, (int)s->text_len)
	     && EVP_CIPHER_CTX_ctrl(c, EVP_CTRL_GCM_SET_TAG, (int)s->tag_len,
	                            s->tag)
	     && EVP_DecryptFinal_ex(c, s->text + n, &n) > 0;
	EVP_CIPHER_CTX_free(c);
	return ok ? 0 : -1;
}
static const uint8_t hdr[] = {%s};
static const uint8_t nonce[12] = {%s};
static const uint8_t plain[] = {%s};
int main(void)
{
	uint32_t n = 0;
	situ_msg_t msg;
	situ_view_t view;
	situ_packet_sealed_t gate;
	situ_err_t e = situ_packet_build(out, sizeof out, 7, hdr, sizeof hdr,
	                                 nonce, plain, sizeof plain, seal, NULL,
	                                 &n);
	uint8_t again[256];

	(void)show;
	memcpy(again, out, n);
	situ_msg_init(&msg, out, n);
	e = situ_packet_view(&msg, 0, n, &view);
	e = e ? e : situ_packet_sealed_unseal(view, open_gcm, NULL, &gate);
	printf("%%d %%u %%d\\n", (int)e,
	       e ? 0u : (unsigned)situ_packet_sealed_inner_seq_get(gate),
	       e ? 0 : memcmp(situ_packet_sealed_body_ptr(gate), "attack", 6));
	/* The header under the tag, flipped: refused, and nothing decrypted
	 * is left behind. */
	again[5] ^= 1;
	situ_msg_init(&msg, again, n);
	e = situ_packet_view(&msg, 0, n, &view);
	e = e ? e : situ_packet_sealed_unseal(view, open_gcm, NULL, &gate);
	printf("%%d", (int)e);
	for (uint32_t i = 0; i < n; i++) {
		if (memcmp(again + i, "attack", 6) == 0) printf(" plaintext");
	}
	puts("");
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None or AESGCM is None,
                    reason="needs a C compiler and `cryptography`")
def test_a_packet_is_unsealed_by_the_callers_open(tmp_path: Path) -> None:
	"""The receiving half (26.1194): the caller's open is handed the same
	split the seal was -- nonce, additional data, ciphertext, tag -- from
	the reader's own answers, and the gate comes out only if it verified.
	A flipped header byte is refused, and the region it decrypted into
	is zeroed rather than left as unauthenticated plaintext."""
	hdr = struct.pack(">BBHI", 1, 2, len(BODY), 41)
	plain = struct.pack(">HI", 5, 6) + bytes(range(16)) + BODY
	lines = _run("packet", UNSEAL_MAIN % (_bytes(hdr), _bytes(NONCE),
	                                      _bytes(plain)), tmp_path)
	assert lines == [["0", "6", "0"], ["4"]], lines


# Unsealed alike: the message the toy sealed, opened by a toy open written in
# each language -- which decrypts BEFORE it checks the tag, as OpenSSL's
# EVP does, so a refusal leaves plaintext unless the unseal zeroes it --
# intact and with a covered byte flipped. Each prints its buffer after.

TOY_OPEN_C = """
static int toy_open(void *ctx, const situ_sealing_t *s)
{
	uint32_t total = 0;
	int ok = 1;
	(void)ctx;
	for (uint32_t i = 0; i < s->aad_len; i++) total += s->aad[i];
	for (uint32_t i = 0; i < s->text_len; i++) {
		total += s->text[i];
		s->text[i] ^= (uint8_t)((s->nonce_len ? s->nonce[i % s->nonce_len] : 0)
		                        ^ (uint8_t)s->key_id ^ (uint8_t)i);
	}
	for (uint32_t j = 0; j < s->tag_len; j++)
		if (s->tag[j] != (uint8_t)(total + 7u * j)) ok = 0;
	return ok ? 0 : -1;
}
"""

VIEWED = {"packet": "packet", "dtls": "record", "headed": "headed"}


def _sealed_bytes(stem: str, args: list[int | bytes]) -> bytes:
	"""The message as the toy sealed it, computed here."""
	plain = args[-1]
	assert isinstance(plain, bytes)
	if stem == "packet":
		head = bytes([7, 0, 0, 0]) + args[1] + NONCE	# type: ignore[operator]
		body, tag = _toy(0, NONCE, head[4:], plain, 16)
	elif stem == "headed":
		head = args[0]
		assert isinstance(head, bytes)
		body, tag = _toy(0, NONCE, head, plain, 16)
	else:
		head = bytes([23, 0xFE, 0xFD, 0, 3]) + bytes([0, 0, 0, 0, 0, 9]) \
			+ struct.pack(">H", len(plain) + 24) + NONCE[:8]
		body, tag = _toy(3, NONCE[:8], b"", plain, 16)
	return head + body + tag


def _unseal_drivers(stem: str, message: bytes) -> dict[str, str]:
	name = VIEWED[stem]
	flipped = bytearray(message)
	flipped[len(message) - 17] ^= 1		# the region's last byte
	lists = (f"static uint8_t one[] = {_c_list(message)};\n"
	         f"static uint8_t two[] = {_c_list(bytes(flipped))};\n")
	show = """
static void dump(int e, const uint8_t *b, uint32_t n)
{
	printf("%d", e);
	for (uint32_t i = 0; i < n; i++) printf(" %02x", b[i]);
	puts("");
}
"""
	c = (f'#include <stdio.h>\n#include "{stem}_build.h"\n{TOY_OPEN_C}\n'
	     f"{lists}{show}\nstatic int go(uint8_t *b, uint32_t n)\n{{\n"
	     "\tsitu_msg_t m;\n\tsitu_view_t v;\n"
	     f"\tsitu_{name}_sealed_t g;\n"
	     "\tsitu_msg_init(&m, b, n);\n"
	     f"\tsitu_err_t e = situ_{name}_view(&m, 0, n, &v);\n"
	     f"\tif (e == SITU_OK) e = situ_{name}_sealed_unseal(v, toy_open, "
	     "NULL, &g);\n\tdump((int)e, b, n);\n\treturn 0;\n}\n"
	     "int main(void)\n{\n\tgo(one, sizeof one);\n"
	     "\tgo(two, sizeof two);\n\treturn 0;\n}\n")
	cpp = (f'#include <cstdio>\n#include "{stem}_build.hpp"\n'
	       + TOY_OPEN_C.replace("NULL", "nullptr") + f"\n{lists}{show}\n"
	       "int main()\n{\n"
	       f"\tint e = static_cast<int>(situ_build::{name}_sealed_unseal("
	       "one, sizeof one, toy_open, nullptr, [](auto) {}));\n"
	       "\tdump(e, one, sizeof one);\n"
	       f"\te = static_cast<int>(situ_build::{name}_sealed_unseal("
	       "two, sizeof two, toy_open, nullptr, [](auto) {}));\n"
	       "\tdump(e, two, sizeof two);\n\treturn 0;\n}\n")
	rust = (f"mod situ_rt;\n#[allow(dead_code)] mod {stem};\n"
	        f"#[allow(dead_code)] mod {stem}_build;\n"
	        "fn toy_open(s: &mut situ_rt::Sealing<'_>) -> bool {\n"
	        "\tlet mut total: u32 = 0;\n"
	        "\tlet k = s.key_id.unwrap_or(0) as u8;\n"
	        "\tfor b in s.aad { total = total.wrapping_add(u32::from(*b)); }\n"
	        "\tfor i in 0..s.text.len() {\n"
	        "\t\ttotal = total.wrapping_add(u32::from(s.text[i]));\n"
	        "\t\tlet n = if s.nonce.is_empty() { 0 } else "
	        "{ s.nonce[i % s.nonce.len()] };\n"
	        "\t\ts.text[i] ^= n ^ k ^ (i as u8);\n\t}\n"
	        "\t(0..s.tag.len()).all(|j| s.tag[j] == "
	        "total.wrapping_add(7 * j as u32) as u8)\n}\n"
	        "fn dump(e: i32, b: &[u8]) {\n\tprint!(\"{}\", e);\n"
	        "\tfor x in b { print!(\" {:02x}\", x); }\n\tprintln!();\n}\n"
	        "fn code(r: Result<(), situ_rt::Error>) -> i32 {\n"
	        "\tmatch r { Ok(()) => 0, Err(situ_rt::Error::Tag) => 4, "
	        "Err(situ_rt::Error::Bounds) => 1, Err(_) => 99 }\n}\n"
	        "fn main() {\n"
	        f"\tlet mut one = {_c_list(message).replace('{', '[').replace('}', ']')};\n"
	        f"\tlet mut two = {_c_list(bytes(flipped)).replace('{', '[').replace('}', ']')};\n"
	        f"\tlet r = {stem}_build::unseal_{name}_sealed(&mut one, "
	        "&mut toy_open, |_g| ());\n\tdump(code(r), &one);\n"
	        f"\tlet r = {stem}_build::unseal_{name}_sealed(&mut two, "
	        "&mut toy_open, |_g| ());\n\tdump(code(r), &two);\n}\n")
	return {"c": c, "cpp": cpp, "rust": rust}


def _python_unseals(directory: Path, stem: str, message: bytes) -> str:
	sys.path[:0] = [str(directory), str(RUNTIME / "python")]
	try:
		import importlib
		built = importlib.import_module(f"{stem}_build")
		reader = importlib.import_module(stem)
		runtime = importlib.import_module("situ_runtime")
	finally:
		del sys.path[:2]
	name = VIEWED[stem]

	def toy_open(s: Any) -> None:
		total = (sum(s.aad) + sum(bytes(s.text))) & 0xFFFFFFFF
		nonce = s.nonce or b""
		key = (s.key_id or 0) & 0xFF
		for i in range(len(s.text)):
			s.text[i] ^= (nonce[i % len(nonce)] if nonce else 0) ^ key \
				^ (i & 0xFF)
		if bytes(s.tag) != bytes((total + 7 * j) & 0xFF
		                         for j in range(len(s.tag))):
			raise ValueError("tag")

	flipped = bytearray(message)
	flipped[len(message) - 17] ^= 1
	lines = []
	for given in (bytearray(message), flipped):
		view = getattr(reader, name).at(runtime.Message(memoryview(given)), 0,
		                                len(given))
		try:
			getattr(built, f"unseal_{name}_sealed")(view, toy_open)
			code = 0
		except runtime.TagError:
			code = 4
		lines.append(str(code) + "".join(f" {one:02x}" for one in given))
	return "\n".join(lines) + "\n"


@pytest.mark.parametrize("stem", ["dtls", "headed", "packet"])
def test_four_readers_unseal_alike(stem: str, tmp_path: Path) -> None:
	sys.path.insert(0, str(ROOT / "test" / "unit"))
	import fourway
	if not fourway.COMPLETE:
		pytest.skip("needs gcc, g++ and rustc")
	schema = ROOT / "example" / stem / f"{stem}.situ"
	if stem in WRITTEN:
		schema = tmp_path / f"{stem}.situ"
		schema.write_text(WRITTEN[stem], encoding="ascii")
	message = _sealed_bytes(stem, FOUR[stem]["args"]())
	said: dict[str, str] = {}
	for target in ("c", "cpp", "rust", "python"):
		out = tmp_path / target
		out.mkdir()
		done = subprocess.run([sys.executable, "-m", "situc", "build",
		                       "--target", target, "--layer", "edit",
		                       str(schema), "--out", str(out)], cwd=ROOT,
		                      capture_output=True, text=True, timeout=300)
		assert done.returncode == 0, done.stderr[-2000:]
	said["python"] = _python_unseals(tmp_path / "python", stem, message)
	drivers = _unseal_drivers(stem, message)
	c, cpp, rust = (tmp_path / one for one in ("c", "cpp", "rust"))
	(c / "main.c").write_text(drivers["c"])
	(cpp / "main.cpp").write_text(drivers["cpp"])
	shutil.copy(RUNTIME / "rust" / "situ_rt.rs", rust)
	rt = rust / "situ_rt.rs"
	rt.write_text(rt.read_text().replace("#![no_std]\n", ""))
	(rust / "main.rs").write_text(drivers["rust"])
	for argv, where in (
			([fourway.HOST_CC or "cc", "-std=c11", "-Wall", "-Wextra",
			  "-Werror", f"-I{RUNTIME / 'c'}", "-I.", "main.c", f"{stem}.c",
			  str(RUNTIME / "c" / "situ.c"), "-o", "probe"], c),
			([fourway.HOST_CXX or "c++", "-std=c++17", "-Wall", "-Wextra",
			  "-Werror", f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}", "-I.",
			  "main.cpp", str(RUNTIME / "c" / "situ.c"), "-o", "probe"], cpp),
			([fourway.RUSTC or "rustc", "--edition", "2021", "-D", "warnings",
			  "-A", "dead_code", "main.rs", "-o", "probe"], rust)):
		made = subprocess.run(argv, cwd=where, capture_output=True,
		                      text=True, timeout=300)
		assert made.returncode == 0, made.stderr[-3000:]
		ran = subprocess.run(["./probe"], cwd=where, capture_output=True,
		                     text=True, timeout=60)
		assert ran.returncode == 0, ran.stderr
		said[where.name] = ran.stdout
	assert len(set(said.values())) == 1, said
	first, second = said["python"].splitlines()
	opened = bytes(int(one, 16) for one in first.split()[1:])
	plain = FOUR[stem]["args"]()[-1]
	assert isinstance(plain, bytes)
	assert first.split()[0] == "0" and opened[-16 - len(plain):-16] == plain
	refused = bytes(int(one, 16) for one in second.split()[1:])
	assert second.split()[0] == "4"
	assert refused[-16 - len(plain):-16] == bytes(len(plain))
