"""A struct that can contain itself takes arguments (0068 step 5, 26.904).

    struct payload [depth = 4] {
        parameter u8 kind [stream];
        u8 next; ...; u16 length [min = 4];
        variant body switch (kind) { ... }
        variant rest switch (next) {
            case 0: nothing none;
            default: payload(kind = next) more;
        }
    }

IKEv2 in miniature: each payload's body is named by the PREVIOUS payload's
`next`, so the argument changes at every level and decides the layout. All of
it is the depth-carrying path, `extent_at`, the nesting probe and the run walks
in each backend, which a struct outside a cycle never reaches.

Every message below is worked out by hand from the layout. Agreement alone
would pass for six readers making the same mistake, so the verdict and a field
behind the recursion are asserted as well.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from test_dissector import LUA, compare
from test_walker_c import (COMPILER, VERDICT, _build_driver, _inline_image,
                           c_answers, c_offsets, c_verdict, python_answers,
                           python_offsets, python_verdict, shape_named_text)

ROOT     = Path(__file__).resolve().parents[2]
HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-O1", "-fsanitize=address,undefined",
            "-fno-sanitize-recover=all"]

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: `pair` is two bytes whatever `length` says, so where `rest` sits follows the
#: arm the handed `kind` chose: a reader with the parent's value reads `next`
#: from the wrong byte. Its `[max]` is reachable only through that arm.
SCHEMA = HEAD + """
struct nothing { }
struct sa    { parameter u16 n [stream]; u8 proposals[n]; }
struct nonce { parameter u16 n [stream]; u8 data[n]; }
struct pair  { u8 a; u8 b [max = 9]; }

struct payload [depth = 4] {
	parameter u8 kind [stream];
	u8 next; bit critical; reserved u7; u16 length [min = 4];
	variant body switch (kind) {
		case 33: sa(n = length - 4) as_sa;
		case 40: pair as_pair;
		default: nonce(n = length - 4) other;
	}
	variant rest switch (next) {
		case 0: nothing none;
		default: payload(kind = next) more;
	}
}
struct ike { u8 first; u8 version; payload(kind = first) p; }

struct node [depth = 3] {
	parameter u8 b [stream];
	u8 data[b];
	u8 n;
	node(b = n) kids[n];
	u8 after [max = 9];
}
struct forest { u8 b; node(b = b) root; u8 tail; }

struct ping [depth = 4] {
	parameter u8 w [stream];
	u8 d[w];
	u8 more;
	variant v switch (more) {
		case 0: nothing none;
		default: pong(z = more) p;
	}
}
struct pong [depth = 4] {
	parameter u8 z [stream];
	u8 d[z];
	u8 n;
	ping(w = n) kids[n];
}
struct pp { u8 w; ping(w = w) p; u8 tail; }

struct wnode [depth = 3] {
	parameter u8 b [stream];
	u8 data[b];
	u8 more;
	u8 n;
	variant sub switch (n) {
		case 0: nothing none;
		default: wlist(b = n) list;
	}
}
struct wlist [depth = 3] {
	parameter u8 b [stream];
	u8 d[b];
	u8 w;
	wnode(b = w) items[] while (more != 0) max 4;
}
struct wtop { u8 b; wlist(b = b) all; u8 tail; }

struct dnode [depth = 3] {
	parameter u8 b [stream];
	u8 data[b];
	u8 n;
	dnode(b = n) kids[] until ";";
}
struct dtop { u8 b; dnode(b = b) r; u8 tail; }

struct capped [depth = 2] {
	parameter u8 cap [stream];
	u8 n [max = cap];
	capped(cap = cap) kids[n];
}
struct ctop { u8 cap; capped(cap = cap) r; }

struct wplain [depth = 3] {
	u8 d;
	u8 more;
	u8 n;
	wplain kids[] while (more != 0) max 4;
}
struct dplain [depth = 3] { u8 d; u8 n; dplain kids[] until ";"; }

// Any `[holds]` sends both walkers' `validate` through a depth walk of its own,
// which has to hand on arguments as well.
struct held_one { u8 x; }
struct holder { u8 n; u8 body[n] [holds = held_one]; }
"""

#: struct -> message -> what all six say: 0 whole, 1 short, 2 a rule broken.
CASES = {
	"ike": {
		# 33 chooses `sa`, whose `next` hands 40 to the payload after it.
		"2101" "28000006aabb" "000000040102":                 "0",
		# The argument differs at all three levels.
		"2101" "0500000511" "280000062233" "000000040102":    "0",
		"0501" "00000005cc":                                  "0",
		# `b` is broken only if `pair` is what the handed 40 selected.
		"2101" "28000006aabb" "00000004010a":                 "2",
		# The frame cuts each level.
		"2101" "28000006aa":                                  "1",
		"2101" "28000006aabb" "0000":                         "1",
		# `[min]` broken, so `length - 4` is -3 and held to zero.
		"2101" "00000001":                                    "2",
		"2101" "28000002" "000000040102":                     "2",
	},
	"forest": {
		"01" "aa0005" "07":                                   "0",
		# Each `n` is what its children are handed.
		"01" "aa02" "bbcc0001" "ddee01" "ff0002" "03" "04" "07": "0",
		"01" "aa02" "bbcc0001" "ddee01" "ff000a" "03" "04" "07": "2",
		"01" "aa02" "bbcc00":                                 "1",
	},
	"pp": {
		"01" "aa00" "07":                                     "0",
		"01" "aa02" "bbcc01" "dd00" "07":                     "0",
		"01" "aa02" "bb":                                     "1",
	},
	"wtop": {
		# `more` sits behind `data[b]` with the ELEMENT's `b`, which is `w`:
		# 2 here, where the list's own `b` is 1 and would read `cc` for it.
		"01" "aa02" "bbcc0000" "07":                          "0",
		"01" "aa02" "bbcc0100" "ddee0000" "07":               "0",
	},
	"dtop": {
		"01" "aa003b" "07":                                   "0",
		"01" "aa02" "bbcc003b" "ddee01" "ff003b" "3b" "3b" "07": "0",
	},
	"ctop": {
		"0200":                                               "0",
		"02" "02" "00" "00":                                  "0",
		"01" "02" "00" "00":                                  "2",
	},
}

#: What a field behind the recursion reads, so that six readers cannot agree
#: on the wrong byte: (struct, message, field, value).
FIELDS = [
	("forest", "01" "aa02" "bbcc0001" "ddee01" "ff0002" "03" "04" "07",
	 "tail", "7"),
	("pp",   "01" "aa02" "bbcc01" "dd00" "07", "tail", "7"),
	("wtop", "01" "aa02" "bbcc0100" "ddee0000" "07", "tail", "7"),
	("dtop", "01" "aa02" "bbcc003b" "ddee01" "ff003b" "3b" "3b" "07",
	 "tail", "7"),
]

#: Exactly `[depth]` deep, under a parent outside the cycle. A `struct` taking
#: an argument is only ever reached that way, and the level this reaches is the
#: one `forest { node root; }` used to cut. What follows the last `payload` is not
#: a sixth: its `next` is 0, which `default:` is not. The field is the proof.
#: Both walkers count the parent as a level, so they stop here, say they cannot
#: say, and agree: a limit of theirs rather than a verdict on the bytes.
AT_DEPTH = {
	"ike":  ["0501" + "05000004" * 4 + "00000004",
	         "0501" + "05000004" * 4 + "00000004" "ffffffff"],
	"forest": ["01" "aa01" "bb01" "cc01" "dd00" "01" "01" "01" "01" "07"],
	"pp":   ["01" "aa01" "bb01" "cc01" "dd01" "ee00" "07"],
	"wtop": ["01" "aa01" "bb0001" "cc01" "dd0000" "07"],
}

#: Past `[depth]`, and each is otherwise whole. The four backends call that
#: malformed. Both walkers stop at the ceiling and say they cannot say, as they
#: do for a recursive struct with no argument at all (26.112, 26.901).
TOO_DEEP = {
	"ike":  "0501" + "05000004" * 5 + "00000004",
	"forest": "01" "aa01" "bb01" "cc01" "dd01" "ee00" "01" "01" "01" "01" "01"
	        "07",
	"pp":   "01" "aa01" "bb01" "cc01" "dd01" "ee01" "ff00" "07",
}


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _line(section: str, name: str) -> str:
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith(name + " "))


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) \
		-> tuple[Path, dict[str, list[str]]]:
	place  = tmp_path_factory.mktemp("recursive")
	schema = place / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	return place, fourway.build(place, schema)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_all_four_read_it_alike(struct: str,
		built: tuple[Path, dict[str, list[str]]]) -> None:
	place, command = built
	for message, verdict in CASES[struct].items():
		given = {backend: _section(fourway.answers(
		             argv, bytes.fromhex(message), place), struct)
		         for backend, argv in command.items()}
		assert len(set(given.values())) == 1, (message, given)
		assert _line(next(iter(given.values())), "validate") == verdict, \
			(message, given)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("struct,message,field,value", FIELDS)
def test_the_field_behind_it_is_the_right_byte(struct: str, message: str,
		field: str, value: str,
		built: tuple[Path, dict[str, list[str]]]) -> None:
	place, command = built
	for argv in command.values():
		section = _section(fourway.answers(
			argv, bytes.fromhex(message), place), struct)
		assert _line(section, field) == value, (argv, section)


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_both_walkers_read_it_alike(struct: str, tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	drive = _build_driver(tmp_path, blob, VERDICT)
	for message, verdict in CASES[struct].items():
		data = bytes.fromhex(message)
		assert python_verdict(blob, data, shape) == verdict, message
		assert c_verdict(tmp_path, blob, data, shape, drive) == verdict, \
			message


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("struct", sorted(CASES))
def test_both_walkers_place_every_member_alike(struct: str,
		tmp_path: Path) -> None:
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	for message, verdict in CASES[struct].items():
		if verdict == "1":
			continue
		data = bytes.fromhex(message)
		assert c_answers(tmp_path, blob, data, shape) \
			== python_answers(blob, data, shape), message
		assert c_offsets(tmp_path, blob, data, shape) \
			== python_offsets(blob, data, shape), message


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("struct", sorted(TOO_DEEP))
def test_a_chain_past_its_depth(struct: str, tmp_path: Path,
		built: tuple[Path, dict[str, list[str]]]) -> None:
	place, command = built
	data  = bytes.fromhex(TOO_DEEP[struct])
	given = {backend: _section(fourway.answers(argv, data, place), struct)
	         for backend, argv in command.items()}
	assert len(set(given.values())) == 1, given
	assert _line(next(iter(given.values())), "validate") == "2", given

	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	assert python_verdict(blob, data, shape) == "cannot-say"
	assert c_verdict(tmp_path, blob, data, shape) == "cannot-say"


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("struct", sorted(AT_DEPTH))
def test_a_chain_at_its_depth(struct: str, tmp_path: Path,
		built: tuple[Path, dict[str, list[str]]]) -> None:
	place, command = built
	blob  = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, struct)
	for message in AT_DEPTH[struct]:
		data  = bytes.fromhex(message)
		given = {backend: _section(fourway.answers(argv, data, place), struct)
		         for backend, argv in command.items()}
		assert len(set(given.values())) == 1, given
		section = next(iter(given.values()))
		assert _line(section, "validate") == "0", given
		if struct != "ike":
			assert _line(section, "tail") == "7", given
		assert c_verdict(tmp_path, blob, data, shape) \
			== python_verdict(blob, data, shape) == "cannot-say"


def _near(rng: random.Random) -> bytes:
	"""Bytes small enough to nest, with the schema's own values among them."""
	return bytes(rng.choice((0, 0, 1, 1, 2, 3, 5, 33, 40, 0x3b, 9, 10,
	                         rng.randrange(256)))
	             for _ in range(rng.randrange(40)))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_agree_on_whatever_comes(
		built: tuple[Path, dict[str, list[str]]]) -> None:
	"""Every struct, every line, drawn bytes: depth and cut frames in any mix."""
	place, command = built
	rng = random.Random(904)
	reached: set[str] = set()
	for _ in range(250):
		data  = _near(rng)
		given = {backend: fourway.answers(argv, data, place)
		         for backend, argv in command.items()}
		assert len(set(given.values())) == 1, (data.hex(), given)
		reached.update(line for line in next(iter(given.values())).splitlines()
		               if line.startswith("validate"))
	# A draw that stopped at every first refusal compares nothing.
	assert {"validate 0", "validate 1", "validate 2"} <= reached


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_both_walkers_agree_on_whatever_comes(tmp_path: Path) -> None:
	blob   = _inline_image(SCHEMA)
	drive  = _build_driver(tmp_path, blob, VERDICT)
	shapes = [shape_named_text(SCHEMA, struct) for struct in CASES]
	rng    = random.Random(26904)
	for _ in range(250):
		data = _near(rng)
		for shape in shapes:
			assert c_verdict(tmp_path, blob, data, shape, drive) \
				== python_verdict(blob, data, shape), (shape, data.hex())


BESIDE = """target buffer;
endian big;
struct kid   { parameter u8 n [stream]; u8 d[n]; }
struct entry { parameter u16 block [stream]; u8 tag [max = 5]; u8 data[block]; }
struct gives { parameter u8 kind [stream]; u8 x;
               variant v switch (kind) { case 1: kid(n = x) a;
                                         default: kid(n = 1) b; }
               u8 after; }
struct wh    { parameter u16 block [stream];
               entry(block = block) w[] while (tag != 0) max 4; u8 after; }
struct de    { parameter u16 block [stream];
               entry(block = block) d[] until ";"; u8 after; }
struct late  { parameter u8 b [stream]; u8 d[b]; u8 n [max = 2]; }
struct under { u16 length [min = 4]; kid2(n = length - 4) a; u8 after; }
struct kid2  { parameter u16 n [stream]; u8 d[n]; u8 t; }
struct wide  { parameter u16 n [stream];
               variant v switch (n) { case 0: u8 a; default: u16 b; } }
struct moved { u16 length [min = 4]; wide(n = length - 4) w; u8 after; }
struct picky { u8 k; variant c switch (k) { case 1: u8 a; default: error; } }
struct holds_picky { u8 s; variant v switch (s) { case 1: picky p;
                                                  default: u8 z; } }
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_what_a_cycle_needed_of_a_struct_outside_one(tmp_path: Path) -> None:
	"""Four shapes with no cycle in them, none of which C could compile.

	A variant switched on a parameter at a static offset, a `while` and a
	delimited run handing on the parent's parameter, and a bound on a field
	behind an argument. All four were accepted and failed at `cc`.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(BESIDE, encoding="ascii")
	subprocess.run([str(ROOT / "bin" / "situc"), "build", "--target", "c",
	                "--out", str(tmp_path), str(schema)],
	               check=True, capture_output=True, text=True)
	assert HOST_CC is not None
	built = subprocess.run(
		[HOST_CC, "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'c'}", "-c",
		 str(tmp_path / "unit.c"), "-o", str(tmp_path / "unit.o")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_a_broken_min_hands_an_empty_child_outside_a_cycle(
		tmp_path: Path) -> None:
	"""The hand-off is held at every parent, not only a recursive one."""
	schema = tmp_path / "unit.situ"
	schema.write_text(BESIDE, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob    = _inline_image(BESIDE)
	shape   = shape_named_text(BESIDE, "under")
	data    = bytes.fromhex("0001" "aabb")
	given   = {backend: _section(fourway.answers(argv, data, tmp_path), "under")
	           for backend, argv in command.items()}
	assert len(set(given.values())) == 1, given
	assert _line(next(iter(given.values())), "validate") == "2", given
	assert python_verdict(blob, data, shape) == "2"
	assert c_verdict(tmp_path, blob, data, shape) == "2"


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_a_broken_min_moves_nothing_it_should_not(tmp_path: Path) -> None:
	"""A handed value chooses an arm, so it is not only a length.

	-3 and 0 pick different arms of `wide`, and the arms differ in width, so
	`after` is where the two would part: 0xbb for a value held at zero.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(BESIDE, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob    = _inline_image(BESIDE)
	shape   = shape_named_text(BESIDE, "moved")
	data    = bytes.fromhex("0001" "aa" "bb" "cc")
	for argv in command.values():
		section = _section(fourway.answers(argv, data, tmp_path), "moved")
		assert _line(section, "after") == str(0xbb), section
	assert str(0xbb) in python_answers(blob, data, shape)
	assert str(0xbb) in c_answers(tmp_path, blob, data, shape)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_refusal_inside_an_arm_is_not_another_arm(tmp_path: Path) -> None:
	"""`default: error` in the struct an arm holds is the message's verdict.

	Python's `except` around the arm caught it as the arm being absent.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(BESIDE, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	data    = bytes.fromhex("01" "07" "ee")
	for argv in command.values():
		section = _section(fourway.answers(argv, data, tmp_path), "holds_picky")
		assert _line(section, "validate") == "3", section


REFUSED = {
	"a [remaining] run of its own cycle":
		("struct t [depth = 3] { parameter u8 b [stream]; u8 n; "
		 "t(b = n) kids[remaining]; }", "a `[remaining]` run"),
	"a counted run in an arm of its own cycle":
		("struct t [depth = 3] { parameter u8 b [stream]; u8 n; "
		 "variant v switch (b) { case 0: u8 stop; "
		 "default: t(b = 0) kids[n]; } }", "a run in an arm"),
	"a while run in an arm of its own cycle":
		("struct t [depth = 3] { parameter u8 b [stream]; u8 n; "
		 "variant v switch (b) { case 0: u8 stop; "
		 "default: t(b = n) kids[] while (n != 0) max 3; } }",
		 "a run in an arm"),
	"an indexed region of its own cycle":
		("struct t [depth = 2] { parameter u8 b [stream]; u8 d[b]; u8 n; "
		 "indexed(offset_type = u8, count = n) { t(b = n) e; } }",
		 "an `indexed` region"),
}


@pytest.mark.parametrize("source,shape", REFUSED.values(), ids=list(REFUSED))
def test_what_stays_unbuilt_says_which_shape(source: str, shape: str) -> None:
	with pytest.raises(SituError) as refused:
		solve(parse_text(HEAD + source))
	assert shape in str(refused.value)
	assert "not built" in str(refused.value)


def test_a_run_of_a_cycle_outside_it_is_built() -> None:
	"""`top` is not in the cycle, so the run has an end and the arm a size."""
	solve(parse_text(HEAD + """
		struct t [depth = 3] { parameter u8 b [stream]; u8 n;
		                       t(b = n) kids[n]; }
		struct rest { t(b = 1) all[remaining]; }
		struct arm { u8 k; u8 n;
		             variant v switch (k) { case 1: t(b = 1) r[n];
		                                    default: u8 z; } }"""))


def test_a_member_of_its_own_cycle_still_names_its_arguments() -> None:
	"""Rule 6: nothing is inferred from matching names."""
	with pytest.raises(SituError):
		solve(parse_text(HEAD + """
			struct t [depth = 3] { parameter u8 b [stream]; u8 n;
			                       t kids[n]; }"""))


def _lua_text() -> str:
	schema = parse_text(SCHEMA)
	return generate(schema, resolve(schema, solve(schema)), "unit")


def test_the_dissector_declines_what_it_cannot_measure() -> None:
	"""It carries no depth, so a member of a cycle stops the walk and says so.

	`payload` recursing through an arm ran `gen-dissector` out of stack, with
	an argument or without one.
	"""
	lua = _lua_text()
	assert "ike.p: one `payload` has no extent this dissector can compute" \
		in lua
	assert "payload.rest: one arm has no length this dissector can compute" \
		in lua
	# Nothing names an extent no function defines.
	assert "payload_extent" not in lua and "node_extent" not in lua


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_agrees_with_the_walker_as_far_as_it_goes(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert not result.differ, result.differ
	assert result.compared > 0


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_tshark_runs_the_dissector_as_far_as_it_goes(tmp_path: Path) -> None:
	"""The real consumer: a stand-in cannot say what Wireshark refuses."""
	script = tmp_path / "unit.lua"
	script.write_text(_lua_text() + 'DissectorTable.get("wtap_encap")'
	                  ":add(wtap.USER0, ike)\n", encoding="ascii")
	dump = tmp_path / "ike.hex"
	dump.write_text("0000 21 01 00 00 00 05 cc\n", encoding="ascii")
	capture = tmp_path / "ike.pcap"
	subprocess.run(["text2pcap", "-q", "-l", "147", str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run(["tshark", "-r", str(capture),
	                        "-X", f"lua_script:{script}", "-V"],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua: Error" not in shown.stderr, shown.stderr
	assert "first: 33" in shown.stdout, shown.stdout
	assert "version: 1" in shown.stdout, shown.stdout


#: Small lengths matter most, and half the bytes small keeps a message past its
#: first refusal.
FILL = """
static unsigned long seed = 12345u;

static unsigned draw(void)
{
	seed = seed * 6364136223846793005ul + 1442695040888963407ul;
	return (unsigned)(seed >> 33);
}

static void fill(unsigned char *bytes, unsigned length)
{
	static const unsigned char named[] = {0, 0, 1, 1, 2, 3, 5, 33, 40, 59};

	for (unsigned i = 0u; i < length; i++) {
		const unsigned value = draw();

		bytes[i] = (value & 0x700u) ? named[value % 10u]
		                            : (unsigned char)value;
	}
}
"""

SHAPES = ("ike", "forest", "pp", "wtop", "dtop", "ctop")

C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
""" + FILL + """
int main(void)
{
	long accepted = 0;

	for (long i = 0; i < 40000; i++) {
		const unsigned length = draw() % 40u;
		unsigned char *bytes = malloc(length ? length : 1u);
		situ_msg_t     msg;
		situ_view_t    view;
		uint32_t       need;

		fill(bytes, length);
		situ_msg_init(&msg, bytes, length);
""" + "".join(f"""		if (situ_{name}_view(&msg, 0, length, &view) == SITU_OK
				&& situ_{name}_validate(view) == SITU_OK) {{
			accepted++;
		}}
""" for name in SHAPES) + """		(void)situ_forest_required(bytes, length, &need);
		(void)situ_ike_required(bytes, length, &need);
		(void)situ_pp_required(bytes, length, &need);
		free(bytes);
	}
	printf("%ld\\n", accepted);
	return 0;
}
"""

CXX_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
""" + FILL + """
int main()
{
	long accepted = 0;
	const auto ok = ::situ::rt::err::ok;

	for (long i = 0; i < 40000; i++) {
		const unsigned length = draw() % 40u;
		auto *bytes = static_cast<unsigned char *>(
			std::malloc(length ? length : 1u));

		fill(bytes, length);
		::situ::rt::message msg(bytes, length);
""" + "".join(f"""		{{
			::situ::{name} held;

			if (::situ::{name}::at(msg, 0u, length, held) == ok
					&& held.validate() == ok) {{
				accepted++;
			}}
		}}
""" for name in SHAPES) + """		std::free(bytes);
	}
	std::printf("%ld\\n", accepted);
	return 0;
}
"""


def _generate(tmp_path: Path, target: str) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	subprocess.run([str(ROOT / "bin" / "situc"), "build", "--target", target,
	                "--out", str(tmp_path), str(schema)],
	               check=True, capture_output=True, text=True)


def _run(binary: Path) -> None:
	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=240)
	assert ran.returncode == 0, ran.stderr[:2000]
	# Nothing validated would mean every message stopped at the first
	# refusal, and the walk this file is about was never taken.
	assert int(ran.stdout) > 0


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_reads_nothing_past_the_view(tmp_path: Path) -> None:
	assert HOST_CC is not None
	_generate(tmp_path, "c")
	(tmp_path / "main.c").write_text(C_PROBE, encoding="ascii")
	subprocess.run([HOST_CC, "-std=c11", *SANITIZE,
	                f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'c'}",
	                str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
	                str(ROOT / "runtime" / "c" / "situ.c"),
	                "-o", str(tmp_path / "probe")],
	               check=True, capture_output=True, text=True)
	_run(tmp_path / "probe")


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_reads_nothing_past_the_view(tmp_path: Path) -> None:
	assert HOST_CXX is not None
	_generate(tmp_path, "cpp")
	(tmp_path / "main.cpp").write_text(CXX_PROBE, encoding="ascii")
	subprocess.run([HOST_CXX, "-std=c++17", *SANITIZE,
	                f"-I{tmp_path}", f"-I{ROOT / 'runtime' / 'cpp'}",
	                f"-I{ROOT / 'runtime' / 'c'}",
	                "-x", "c++", str(tmp_path / "main.cpp"),
	                str(ROOT / "runtime" / "c" / "situ.c"),
	                "-o", str(tmp_path / "probe")],
	               check=True, capture_output=True, text=True)
	_run(tmp_path / "probe")
