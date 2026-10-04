"""`validate` does not read the interior of a `sealed` region (26.564).

The generated gate type says what the rule is, in its own doc comment:

    Every accessor for the interior takes this type, and the only thing
    that produces one is situ_<region>_open(), which will not hand one
    out until <tag> has verified. Parsing attacker-controlled plaintext
    before authenticating it is therefore not discouraged here; it does
    not compile.

C's `validate` read `situ_base(view) + 3u` directly and did exactly that, by
the one route the design says is impossible -- `validate` holds a plain view
and is documented as *the first thing a parser runs and the last thing an
attacker controls*, so every byte it reads is unauthenticated.

Measured on a `held` whose sealed six bytes carry `80 81 82 83`, which is
what an encrypted field looks like from outside the gate:

    C        SITU_ERR_CONSTRAINT -- not valid UTF-8
    Python   accepted
    C++      accepted
    Rust     accepted

So C was refusing a legitimate message on the strength of ciphertext, and the
four disagreed about a real input. Only the span attributes leaked: a
`[max]` inside a region was already declined by the dotted-path dispatch at
the foot of `_member_checks`, which is why this surfaced as `[encoding]` and
`[nul_terminated]` and not as a bound.

**An `authenticated` region is the control and is deliberately untouched.**
Its bytes are plaintext with a tag over them, its accessors take a view
rather than a gate, and its members' constraints are checked by all four.
`example/usb` is that shape. What separates the two is `_gate_type`, which is
also what `unverified_ok` opts a member out of -- so a member the author
declared readable before verification keeps its checks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol

import pytest

from situc import ast
from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

ROOT    = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime" / "c"
HOST_CC = shutil.which("gcc") or shutil.which("cc")
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror")

CODEC = """codec aead {
	granularity = byte;
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}

impl aead extern "x";
"""

#: The interior carries both span attributes and a value bound, so the test
#: speaks about the branch that leaked and the one that never did.
INTERIOR = """struct held {
	u8  kind;
	%s {
		u16  n [max = 500];
		u8   label[4] [nul_terminated, encoding = utf8];
	}
	tag u8[16];
}
"""

PREAMBLE = "target buffer;\nendian big;\n\n" + CODEC + "\n"
SEALED        = PREAMBLE + INTERIOR % "sealed body(aead)"
AUTHENTICATED = PREAMBLE + INTERIOR % "authenticated body"


class Emitted(Protocol):
	def files(self) -> dict[str, str]: ...


class Backend(Protocol):
	def __call__(self, schema: ast.Schema, resolved: ResolvedSchema,
	             basename: str) -> Emitted: ...


BACKENDS: dict[str, Backend] = {
	"c":      generate_c,
	"cpp":    generate_cpp,
	"python": generate_python,
	"rust":   generate_rust,
}

#: The CALL, not the name. Python imports `utf8_valid` from its runtime at
#: the top of every module, so a mark matching the bare name counts one
#: before any check is emitted -- the same instrument error 26.563 made with
#: check ids, one file later.
CALLS = {
	"c":      "situ_utf8_valid(",
	"cpp":    "situ_utf8_valid(",
	"python": "if not utf8_valid(",
	"rust":   "situ_rt::utf8_valid(",
}


def _emitted(backend: str, text: str) -> str:
	schema   = parse_text(text, path="held.situ")
	resolved = resolve(schema, solve(schema))
	built    = BACKENDS[backend](schema, resolved, "held")
	return "".join(one for _, one in sorted(built.files().items()))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_no_backend_checks_the_interior_of_a_sealed_region(
		backend: str) -> None:
	"""Because no backend may read it before the tag has verified."""
	assert CALLS[backend] not in _emitted(backend, SEALED), (
		f"the {backend} backend validates the interior of a `sealed` region, "
		"whose bytes nothing has authenticated")


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_every_backend_checks_the_interior_of_an_authenticated_region(
		backend: str) -> None:
	"""The control, and the reason this is about the gate and not the region.

	An `authenticated` region is plaintext with a tag over it: its accessors
	take a view, so `validate` may read it. Without this the test above
	passes for a backend that stopped checking `[encoding]` anywhere.
	"""
	assert CALLS[backend] in _emitted(backend, AUTHENTICATED), (
		f"the {backend} backend does not check `[encoding]` inside an "
		"`authenticated` region, whose bytes it may read")


@pytest.mark.skipif(HOST_CC is None, reason="no C compiler")
def test_c_accepts_a_sealed_body_that_is_not_text(tmp_path: Path) -> None:
	"""The reported shape, asked of the bytes.

	`80 81 82 83` in the sealed field is not valid UTF-8 and is exactly what
	a ciphertext looks like. C refused it and the other three accepted; this
	is the verdict agreeing now.
	"""
	schema   = parse_text(SEALED, path="held.situ")
	resolved = resolve(schema, solve(schema))
	built    = generate_c(schema, resolved, "held")
	(tmp_path / "held.h").write_text(built.header, encoding="ascii")
	(tmp_path / "held.c").write_text(built.source, encoding="ascii")
	(tmp_path / "probe.c").write_text("""
#include "held.h"

int main(void)
{
	/* kind, six sealed bytes, a sixteen-byte tag. */
	uint8_t     raw[1 + 6 + 16] = { 0 };
	situ_msg_t  msg;
	situ_view_t view;

	raw[3] = 0x80; raw[4] = 0x81; raw[5] = 0x82; raw[6] = 0x83;

	situ_msg_init(&msg, raw, sizeof raw);
	if (situ_held_view(&msg, 0, &view) != SITU_OK) return 1;
	if (situ_held_validate(view) != SITU_OK)       return 2;
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "held.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	ran = subprocess.run([str(tmp_path / "probe")])
	assert ran.returncode == 0, (
		f"the probe failed at check {ran.returncode}: `validate` judges bytes "
		"nothing has authenticated")
