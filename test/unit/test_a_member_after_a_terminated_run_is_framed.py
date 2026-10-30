"""A member after an `until` run of records is framed from past the
terminator (26.757).

`required` walks a terminated run element by element and, at the
terminator, added its bytes to the running total `at` and not to the
cursor `pos` -- the emitters' comment said the cursor "is never read after
this break". It is, wherever another variable member follows the run: that
member was framed from the terminator itself, one byte early, so a nested
struct after `form entries[] until ")"` read its atom's length one byte
off, and all four backends called the message complete short of its end.

Reported by hull (suggestion/hull.md section 21) as `situc verify`
finding 9 surplus bytes in vectors that had conformed: `verify` sizes a
vector with the Python backend's `required`. It bisects to 1e81500, which
let a nested struct frame itself through its own `required` -- the first
thing after the run to read the cursor at all. The differential could not
see it, all four agreeing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import fourway

ROOT = Path(__file__).resolve().parents[2]

#: hull's minimal case, with the parts of its canonical s-expressions the
#: case needs written in.
SCHEMA = """target buffer;
endian big;
struct form [depth = 64] {
	peek u8  kind;
	variant body switch (kind) {
		case '(':  list  as_list;
		default:   atom  as_atom;
	}
}
struct list [depth = 64] {
	u8    open;
	form  items[]  until ")";
}
struct atom {
	decimal u32  length  until ":" max 10 [minimal];
	u8           bytes[length];
}
struct tail {
	preamble u8[3] = "(1:";
	u8    k;
	atom  id;
	u8    close  [must_eq = ')'];
}
struct msg {
	u8    open [must_eq = '('];
	form  entries[]  until ")";
	tail  t;
}
"""

#: Messages hull measured, each whole: 4, 4 and 2 bytes were "surplus".
MESSAGES = [b"((1:a))(1:k5:alpha)", b"()(1:k5:alpha)",
            b"((1:a)(1:b))(1:k3:abc)", b"((1:a))(1:k1:*)"]


def test_all_four_frame_the_whole_message(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	for message in MESSAGES:
		for name, argv in command.items():
			section = fourway.answers(argv, message, tmp_path) \
				.split("-- msg\n", 1)[1].split("\n-- ", 1)[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("required()"))
			assert line.split()[-1] == f"*{len(message)}", (
				f"{message!r}: {name} answered {line.split()[-1]}")


def test_verify_accepts_the_vectors(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	vectors = tmp_path / "unit.vectors"
	vectors.write_text("".join(
		f"msg m{i} " + " ".join(f"{byte:02X}" for byte in message) + "\n"
		for i, message in enumerate(MESSAGES)), encoding="ascii")
	ran = subprocess.run([sys.executable, "-m", "situc", "verify",
	                      str(schema), str(vectors)],
	                     capture_output=True, text=True, cwd=ROOT)
	assert "surplus" not in ran.stdout + ran.stderr, ran.stdout + ran.stderr
	assert ran.returncode == 0, ran.stdout + ran.stderr
