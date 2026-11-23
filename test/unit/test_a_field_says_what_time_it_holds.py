"""`[time = UNIT, epoch = EPOCH]` and `[duration = UNIT]` (26.1165).

The holder's direction, relayed by fuzznet (inbox 20261123T1050Z): an
unambiguous description of time on a field, every unit -- sub-second,
non-decimal ticks, calendar -- and situ not narrowing the author's choice.
Step 1 is the attribute, its refusals and the wire signature, so that two
consumers reading `expires_at` differently fail `wire --check` by name.
"""

from __future__ import annotations

import pytest

from situc import wire
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve
from situc.unparse import unparse
from situc.wellformed import check

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\nconst tick = 100;\n"


def _members(body: str) -> dict[str, str]:
	"""Each member's wire line, by name, for one struct `f`."""
	schema = parse_text(HEAD + "struct f {\n" + body + "}\n", path="f.situ")
	check(schema)
	text = wire.render(schema, resolve(schema, solve(schema)), "f.situ")
	lines: dict[str, str] = {}
	for line in text.splitlines():
		words = line.split()
		if len(words) > 3 and words[0].startswith("@"):
			lines[words[3]] = line
	return lines


def _refused(body: str) -> str:
	with pytest.raises(SituError) as caught:
		check(parse_text(HEAD + "struct f {\n" + body + "}\n", path="f.situ"))
	return str(caught.value)


@pytest.mark.parametrize(("member", "fact"), [
	("u64 t [time = ms, epoch = unix];", "time=ms@unix"),
	("u32 t [duration = us];", "duration=us"),
	("i64 t [duration = ps];", "duration=ps"),
	# NTP's 32.32 fraction, Windows' FILETIME and MPEG's system clock: no
	# word of their own, and none needed.
	("u64 t [time = s / (1 << 32), epoch = ntp];", "time=1/4294967296s@ntp"),
	('u64 t [time = ns * tick, epoch = "1601-01-01"];',
	 "time=1/10000000s@1601-01-01T00:00:00Z"),
	("u64 t [time = s / 90000, epoch = caller];", "time=1/90000s@caller"),
	("u32 t [time = d, epoch = gps];", "time=d@gps"),
	("u16 t [duration = min * 15];", "duration=900s"),
	("u8 t [duration = month];", "duration=month"),
	("u8 t [duration = month * 12];", "duration=year"),
	('u16 t [time = year, epoch = "2000-01-01T00:00:00Z"];',
	 "time=year@2000-01-01T00:00:00Z"),
	("decimal u32 t[10] [time = s, epoch = unix];", "time=s@unix"),
	("u4 t [duration = h];", "duration=h"),
])
def test_the_signature_states_it(member: str, fact: str) -> None:
	assert fact in _members(member)["t"].split()


def test_two_spellings_of_one_unit_are_one_unit() -> None:
	"""`s / 1000` is `ms`: the signature compares meanings, so rewording a
	unit is no contract change and a change of unit always is one."""
	assert "duration=ms" in _members("u32 t [duration = s / 1000];")["t"]
	assert "duration=us" in _members("u32 t [duration = ms / 1000];")["t"]


def _signature(member: str) -> str:
	schema = parse_text(HEAD + "struct f {\n" + member + "\n}\n",
	                    path="f.situ")
	check(schema)
	return wire.render(schema, resolve(schema, solve(schema)), "f.situ")


@pytest.mark.parametrize("other", [
	"u64 t [time = us, epoch = unix];",
	"u64 t [time = ms, epoch = caller];",
	"u64 t [duration = ms];",
	"u64 t;",
])
def test_a_consumer_reading_it_otherwise_fails_the_check(other: str) -> None:
	"""fuzznet's case: `expires_at` read in another unit, from another
	epoch, as a duration, or with nothing said, is a breaking change."""
	before  = _signature("u64 t [time = ms, epoch = unix];")
	verdict = wire.compare(before, _signature(other))
	assert verdict.findings, other


def test_it_round_trips() -> None:
	text = HEAD + ('struct f {\n\tu64 t [time = ns * 100, '
	               'epoch = "1601-01-01"];\n}\n')
	once = unparse(parse_text(text, path="f.situ"))
	assert "time = ns * 100" in once
	assert once == unparse(parse_text(once, path="f.situ"))


@pytest.mark.parametrize(("member", "said"), [
	("u64 t [time = fortnight, epoch = unix];", "`fortnight` is not a unit"),
	("u64 t [time = ms, epoch = y2k];", "`epoch` is one of"),
	('u64 t [time = ms, epoch = "2001-02-30"];', "`epoch` is one of"),
	('u64 t [time = ms, epoch = "1/1/2001"];', "`epoch` is one of"),
	("u64 t [time = ms];", "a `time` states its `epoch`"),
	("u64 t [epoch = unix];", "an `epoch` belongs to a `time`"),
	("u64 t [duration = ms, epoch = unix];", "a `duration` has no epoch"),
	("u64 t [time = ms, duration = ms, epoch = unix];", "not both"),
	("u64 t [duration = s / 0];", "positive whole number"),
	("u64 t [duration = 1000 / s];", "a base unit, multiplied or divided"),
	("u64 t [duration = s + 1];", "a base unit, multiplied or divided"),
	("u64 t [time = month, epoch = gps];", "no calendar"),
	("u64 t [time = year, epoch = caller];", "no calendar"),
])
def test_what_cannot_hold_is_refused(member: str, said: str) -> None:
	assert said in _refused(member)


def test_a_factor_from_the_message_is_not_yet_read() -> None:
	"""Step 4 reads it. Until then it is refused, not taken as a constant
	of whatever the name happens to fold to."""
	assert "constant" in _refused("u32 scale;\n\tu64 t [duration = ns * scale];")


@pytest.mark.parametrize("member", [
	"u8 t[4] [duration = s];",
	"bit t [duration = s];",
])
def test_it_sits_on_a_number(member: str) -> None:
	assert "means nothing here" in _refused(member)


def test_a_struct_is_no_number() -> None:
	text = HEAD + "struct g { u8 a; }\nstruct f { g t [duration = s]; }\n"
	with pytest.raises(SituError) as caught:
		check(parse_text(text, path="f.situ"))
	assert "means nothing here" in str(caught.value)


# -- step 3: the kernels follow the field ------------------------------------

KERNELS = """target buffer;
endian big;
struct h { u64 nonce; u64 exp TIME; u8 index; u8 chunks; }
struct f { h head; u8 body[remaining]; }
replay n(m: f) [nonce = m.head.nonce, expires = m.head.exp,
                ahead = 1000UNIT];
relation same(first: f, later: f) {
	must later.head.nonce == first.head.nonce;
}
reassemble pieces(p: f) by same [index = p.head.index,
	count = p.head.chunks, piece = p.body, messages = 2, bytes = 64,
	timeout = 3000, expires = p.head.expROOM];
"""


def _kernels(time: str, unit: str = "", room: str = "") -> str:
	return (KERNELS.replace("TIME", time).replace("UNIT", unit)
	        .replace("ROOM", room))


def _c_of(text: str) -> str:
	from situc.codegen.c import reassemble, replay
	schema = parse_text(text, path="k.situ")
	check(schema)
	resolved = resolve(schema, solve(schema))
	files = {**replay.generate(schema, resolved, "k"),
	         **reassemble.generate(schema, resolved, "k")}
	assert len(files) == 2, files.keys()
	return "\n".join(files.values())


def test_a_kernel_takes_its_clock_from_the_field() -> None:
	"""fuzznet's `expires_at`: the field says what both peers read, so
	neither kernel restates the unit, and the generated tables say it."""
	source = _c_of(_kernels("[time = ms, epoch = unix]"))
	assert "in ms" in source


def test_a_tick_reaches_the_generated_comment() -> None:
	source = _c_of(_kernels("[time = s / 90000, epoch = caller]"))
	assert "1/90000s" in source


def test_a_kernel_unit_that_agrees_is_accepted() -> None:
	check(parse_text(_kernels("[time = ms, epoch = unix]", ", unit = ms",
	                          ", unit = ms"), path="k.situ"))


@pytest.mark.parametrize(("time", "said"), [
	("[time = ms, epoch = unix]", "is not the unit"),
	("[duration = ms]", "an expiry is an instant"),
	("[time = month, epoch = unix]", "a clock counts a fixed unit"),
])
def test_a_kernel_disagreeing_with_its_field_is_refused(time: str,
                                                       said: str) -> None:
	with pytest.raises(SituError) as caught:
		check(parse_text(_kernels(time, ", unit = s"), path="k.situ"))
	assert said in str(caught.value)


def test_with_nothing_on_the_field_the_kernel_still_says_it() -> None:
	"""The control: the unit is stated somewhere, or the schema is
	refused, as before 26.1165."""
	with pytest.raises(SituError) as caught:
		check(parse_text(_kernels(""), path="k.situ"))
	assert "does not state `unit`" in str(caught.value)
	with pytest.raises(SituError) as caught:
		check(parse_text(_kernels("", ", unit = ms"), path="k.situ"))
	assert "states no `unit`" in str(caught.value)


# -- step 2: a named value says what time it holds ---------------------------

NTP = HEAD + """struct ts {
	u32 seconds;
	u32 fraction;
	let u64 at [time = s / (1 << 32), epoch = ntp] = seconds << 32 | fraction;
}
"""


def test_a_time_split_across_fields_is_one_value() -> None:
	"""NTP's two halves are one instant (example/ntp says situ named them
	apart because nothing could say they were one)."""
	schema = parse_text(NTP, path="ntp.situ")
	check(schema)
	text = wire.render(schema, resolve(schema, solve(schema)), "ntp.situ")
	assert "  let at time=1/4294967296s@ntp" in text.splitlines()


def test_a_named_value_round_trips_its_time() -> None:
	once = unparse(parse_text(NTP, path="ntp.situ"))
	assert "let u64 at [time = s / (1 << 32), epoch = ntp] =" in once
	assert once == unparse(parse_text(once, path="ntp.situ"))


def test_a_named_value_says_nothing_else() -> None:
	text = NTP.replace("epoch = ntp]", "epoch = ntp, max = 3]")
	with pytest.raises(SituError) as caught:
		check(parse_text(text, path="ntp.situ"))
	assert "means nothing on a named value" in str(caught.value)


def test_a_named_value_is_held_to_the_same_rules() -> None:
	text = NTP.replace(", epoch = ntp]", "]")
	with pytest.raises(SituError) as caught:
		check(parse_text(text, path="ntp.situ"))
	assert "a `time` states its `epoch`" in str(caught.value)


def test_a_value_without_a_time_adds_no_line() -> None:
	"""26.1056's values, in every signature already committed, are left
	as they were."""
	text = NTP.replace(" [time = s / (1 << 32), epoch = ntp]", "")
	schema = parse_text(text, path="ntp.situ")
	check(schema)
	rendered = wire.render(schema, resolve(schema, solve(schema)), "ntp.situ")
	assert "let at" not in rendered
