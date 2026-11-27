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
}


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
					or stem == "headed":
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
	     ", toy, NULL, &n);\n\tshow(e, out, 0);\n"
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
	       ", toy, nullptr, n));\n\tshow(e, out, 0);\n"
	       f"\te = static_cast<int>(situ_build::{struct_name}(out, sizeof out, "
	       f"{c_args(args, True)}, toy, out, n));\n\tshow(e, out, 0);\n"
	       "\treturn 0;\n}\n")

	def rust_args(given: list[int | bytes]) -> str:
		out = []
		for i, one in enumerate(given):
			if isinstance(one, int):
				out.append(str(one))
			elif i == len(given) - 1 or (stem == "packet" and i == 1) \
					or stem == "headed":
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

	def toy(s: object) -> None:
		text = bytes(s.text)				# type: ignore[attr-defined]
		sealed, tag = _toy(s.key_id or 0, s.nonce or b"",	# type: ignore[attr-defined]
		                   s.aad, text, len(s.tag))		# type: ignore[attr-defined]
		s.text[:] = sealed				# type: ignore[attr-defined]
		s.tag[:] = tag					# type: ignore[attr-defined]

	def refuse(s: object) -> None:
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
	if stem == "headed":
		schema = tmp_path / "headed.situ"
		schema.write_text(HEADED, encoding="ascii")
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
	assert [line.split()[0] for line in lines] == ["0", "2", "4"], lines
	# And the toy as this file computes it, from the bytes the reader
	# places: so four agreeing is not four wrong alike.
	built = bytes(int(one, 16) for one in lines[0].split()[1:])
	if stem == "packet":
		aad, nonce, key, at = built[4:24], NONCE, 0, 24
	elif stem == "headed":
		aad, nonce, key, at = built[:15], NONCE, 0, 15
	else:
		aad, nonce, key, at = b"", NONCE[:8], 3, 21
	plain = args[-1]
	assert isinstance(plain, bytes)
	assert built[at:] == b"".join(_toy(key, nonce, aad, plain, 16))
