"""What time a member holds: `[time = UNIT, epoch = EPOCH]` or
`[duration = UNIT]` (26.1165).

A unit is a base unit multiplied or divided by whole numbers -- `ns`,
`s / 1024`, `s / (1 << 32)`, `ns * 100` -- kept as a reduced fraction of
a second (or of a month, for the two calendar units), so two spellings
of one unit are one unit to everything that compares them. The leftmost
term is always the base unit, which is what lets a factor be any
whole-number expression without the unit's name being read as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from math import gcd

from situc import ast
from situc.diagnostics import SituError, error
from situc.expr import Env, evaluate

#: Every base unit, as (numerator, denominator) of its scale's own unit: a
#: second for the eleven fixed ones, a month for the calendar two.
FIXED_UNITS: dict[str, tuple[int, int]] = {
	"ps": (1, 10 ** 12), "ns": (1, 10 ** 9), "us": (1, 10 ** 6),
	"ms": (1, 1000), "s": (1, 1), "min": (60, 1), "h": (3600, 1),
	"d": (86400, 1), "week": (604800, 1),
}
CALENDAR_UNITS: dict[str, tuple[int, int]] = {"month": (1, 1), "year": (12, 1)}
BASE_UNITS = {**FIXED_UNITS, **CALENDAR_UNITS}

#: The named epochs, each with the time scale it counts in. The scale is
#: what decides whether two instants a day apart are 86400 seconds apart,
#: and a date alone cannot say it, so a name carries one.
EPOCHS: dict[str, str] = {
	"unix":   "POSIX: UTC from 1970-01-01T00:00:00Z, leap seconds not counted",
	"ntp":    "UTC from 1900-01-01T00:00:00Z, leap seconds not counted",
	"gps":    "GPS from 1980-01-06T00:00:00Z, continuous",
	"tai":    "TAI from 1958-01-01T00:00:00, continuous",
	"caller": "the caller's own clock, with no civil meaning",
}
#: The epochs whose scale has no months: a continuous count, or none.
NO_CALENDAR = frozenset({"gps", "tai", "caller"})

TIME_ATTRIBUTES = frozenset({"time", "duration", "epoch"})

_DATE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}):(\d{2})Z)?\Z")


@dataclass(frozen=True)
class TimeSpec:
	#: "time" for an instant, "duration" for an interval.
	kind: str
	#: The unit as a reduced fraction of a second, or of a month where
	#: `calendar` is set.
	numerator: int
	denominator: int
	calendar: bool
	#: A name from EPOCHS, or a date normalised to `YYYY-MM-DDTHH:MM:SSZ`;
	#: None for a duration.
	epoch: str | None
	#: The factors read where the value is (26.1165 step 4), in order:
	#: `*hdr.timecode_scale`, `/rate`. A unit the message or the caller
	#: chooses, named rather than folded, so a reader can always tell
	#: where it comes from.
	scale: tuple[str, ...] = ()

	def unit(self) -> str:
		"""The unit as `wire` writes it: a base unit's name where the
		fraction is one, else `N/Ds` (`N/Dmonth`)."""
		return self._fixed() + "".join(self.scale)

	def _fixed(self) -> str:
		table = CALENDAR_UNITS if self.calendar else FIXED_UNITS
		for name, value in table.items():
			if value == (self.numerator, self.denominator):
				return name
		scale = "month" if self.calendar else "s"
		if self.denominator == 1:
			return f"{self.numerator}{scale}"
		return f"{self.numerator}/{self.denominator}{scale}"

	def render(self) -> str:
		if self.kind == "duration":
			return f"duration={self.unit()}"
		return f"time={self.unit()}@{self.epoch}"


def _factor(expr: ast.Expr, env: Env,
            names: frozenset[str] | None) -> int | str:
	"""A whole number the schema states, or the source of one read where
	the value is: a member of the struct, a `parameter` the caller hands
	in, or a value assembled from them (26.1165 step 4). `names` is the
	struct's own; None accepts any path, for a caller after the check."""
	from situc.invariant import paths_in
	from situc.unparse import expr_to_source
	paths = paths_in(expr)
	try:
		value = evaluate(expr, env)
	except SituError as exc:
		if paths and (names is None
		              or all(path.split(".")[0] in names for path in paths)):
			return expr_to_source(expr).replace(" ", "")
		exc.diagnostic.notes.append(
			"a unit's factor is a whole number the schema states, or one "
			"read from a member of this struct or a `parameter` the caller "
			"hands in")
		raise
	if value <= 0:
		raise error("a unit's factor is a positive whole number", expr.span,
		            label = f"this is {value}")
	return value


def _unit(expr: ast.Expr, env: Env, names: frozenset[str] | None,
          scale: list[str]) -> tuple[int, int, bool]:
	if isinstance(expr, ast.NameRef):
		base = BASE_UNITS.get(expr.name)
		if base is None:
			raise error(f"`{expr.name}` is not a unit", expr.span,
			            label = "here",
			            notes = ["a unit is one of "
			                     f"{', '.join(BASE_UNITS)}, multiplied or "
			                     "divided by whole numbers: `s / 90000`, "
			                     "`ns * 100`"])
		return base[0], base[1], expr.name in CALENDAR_UNITS
	if isinstance(expr, ast.Binary) and expr.op in ("*", "/"):
		numerator, denominator, calendar = _unit(expr.left, env, names,
		                                         scale)
		factor = _factor(expr.right, env, names)
		if isinstance(factor, str):
			wrapped = factor if factor.replace(".", "").replace("_", "") \
				.isalnum() else f"({factor})"
			scale.append(f"{expr.op}{wrapped}")
			return numerator, denominator, calendar
		if scale:
			raise error("a unit's constant factors come before the ones read "
			            "from the message", expr.right.span, label = "here",
			            notes = ["`ns * 100 * hdr.scale`, not "
			                     "`ns * hdr.scale * 100`: one spelling "
			                     "per unit, so a signature compares them"])
		if expr.op == "*":
			numerator *= factor
		else:
			denominator *= factor
		common = gcd(numerator, denominator)
		return numerator // common, denominator // common, calendar
	raise error("a unit is a base unit, multiplied or divided by whole "
	            "numbers", expr.span, label = "here",
	            notes = ["the base unit comes first: `s / 1024`, not "
	                     "`1024 / s`"])


def _epoch(attr: ast.Attr) -> str:
	value = attr.value
	if isinstance(value, ast.NameRef) and value.name in EPOCHS:
		return value.name
	if isinstance(value, ast.StringLiteral):
		match = _DATE.match(value.value)
		if match is not None:
			year, month, day, hour, minute, second = (
				int(one) for one in match.groups(default="0"))
			try:
				when: datetime | None = datetime(year, month, day, hour,
				                                 minute, second,
				                                 tzinfo=timezone.utc)
			except ValueError:
				when = None
			if when is not None:
				return when.strftime("%Y-%m-%dT%H:%M:%SZ")
	raise error("`epoch` is one of "
	            f"{', '.join(EPOCHS)}, or a date", attr.span, label = "here",
	            notes = ['a date is "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SSZ", '
	                     "counted on the POSIX scale",
	                     "`caller` is a clock only the caller keeps, such as "
	                     "a monotonic count"])


def spec_of(attrs: tuple[ast.Attr, ...], env: Env,
            names: frozenset[str] | None = None) -> TimeSpec | None:
	"""The member's time, or None where it states none; refused where what
	it states cannot hold."""
	said = {attr.name: attr for attr in attrs if attr.name in TIME_ATTRIBUTES}
	if not said:
		return None
	if "time" in said and "duration" in said:
		raise error("a member is a `time` or a `duration`, not both",
		            said["duration"].span, label = "here",
		            notes = ["`time` counts from an epoch, `duration` from "
		                     "nothing"])
	if "duration" in said and "epoch" in said:
		raise error("a `duration` has no epoch", said["epoch"].span,
		            label = "here",
		            notes = ["an instant is `[time = UNIT, epoch = EPOCH]`"])
	if "epoch" in said and "time" not in said:
		raise error("an `epoch` belongs to a `time`", said["epoch"].span,
		            label = "here",
		            notes = ["`[time = ms, epoch = unix]`"])
	kind = "time" if "time" in said else "duration"
	if kind == "time" and "epoch" not in said:
		raise error("a `time` states its `epoch`", said["time"].span,
		            label = "`epoch` is missing",
		            notes = ["an instant without an origin is the ambiguity "
		                     "this exists to remove: `epoch = unix`, `ntp`, "
		                     "`gps`, `tai`, `caller`, or a date"])
	value = said[kind].value
	if value is None:
		raise error(f"`{kind}` needs a unit", said[kind].span, label = "here")
	scale: list[str] = []
	numerator, denominator, calendar = _unit(value, env, names, scale)
	epoch = _epoch(said["epoch"]) if kind == "time" else None
	if calendar and epoch in NO_CALENDAR:
		raise error(f"the `{epoch}` epoch has no calendar to count months in",
		            said[kind].span, label = "a calendar unit",
		            notes = [f"`{epoch}` is {EPOCHS[epoch or '']}"])
	return TimeSpec(kind, numerator, denominator, calendar, epoch,
	                tuple(scale))


def field_time(schema: ast.Schema, type_name: str, path: str,
               env: Env) -> TimeSpec | None:
	"""The time a kernel's `expires` field states, found by its path
	(`m.head.exp`) from the struct the kernel reads; None where the field
	states none or the path does not lead to one."""
	from situc.wellformed import nested
	structs = {decl.name: decl for decl in schema.structs()}
	steps   = path.split(".")[1:]
	members = structs[type_name].members if type_name in structs else ()
	found: ast.Member | None = None
	for step in steps:
		pending = list(members)
		found   = None
		while pending:
			member = pending.pop(0)
			if getattr(member, "name", None) == step:
				found = member
				break
			pending.extend(nested(member))
		if found is None:
			return None
		type_ref = getattr(found, "type_ref", None)
		held     = structs.get(getattr(type_ref, "name", ""))
		members  = held.members if held is not None else ()
	if found is None:
		return None
	return spec_of(getattr(found, "attrs", ()), env)


def stated_unit(decl: ast.Replay | ast.Reassemble) -> TimeSpec | None:
	"""A kernel's own `unit = ms`, as a duration of that unit."""
	held = next((one for one in decl.attrs if one.name == "unit"), None)
	name = getattr(getattr(held, "value", None), "name", None)
	if name not in FIXED_UNITS:
		return None
	numerator, denominator = FIXED_UNITS[name]
	return TimeSpec("duration", numerator, denominator, False, None)


def clock_unit(schema: ast.Schema, decl: ast.Replay | ast.Reassemble) -> str:
	"""The unit a kernel's clock counts in, as its generated comments say
	it: the kernel's own `unit`, or its `expires` field's (26.1165)."""
	own = stated_unit(decl)
	if own is not None:
		return own.unit()
	expires = next((one for one in decl.attrs if one.name == "expires"), None)
	if expires is not None and expires.value is not None:
		from situc.relation import paths_in
		paths = paths_in(expires.value)
		spec  = field_time(schema, decl.param.type_name, paths[0],
		                   constant_env(schema)) if paths else None
		if spec is not None:
			return spec.unit()
	raise AssertionError(f"`{decl.name}` has no clock unit; wellformed "
	                     "refuses that")


def constant_env(schema: ast.Schema) -> Env:
	"""The schema's constants, folded in order, for a unit's factors."""
	consts: dict[str, int] = {}
	for decl in schema.consts():
		try:
			consts[decl.name] = evaluate(decl.value, Env(consts=consts))
		except SituError:
			continue
	return Env(consts=consts)
