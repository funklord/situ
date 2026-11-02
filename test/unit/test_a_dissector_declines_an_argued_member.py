"""The dissector declines a member its parent hands arguments (26.803).

    struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
    struct vol   { u16 block; entry(block = block) ent; u8 after; }

The Lua dissector reads a parameter from the struct's own preference,
which is right when `entry` is the top of a capture. Under `vol` it is
wrong: `vol.block` says what the argument is, and the dissector read
`entry.prefs.block` and placed `after` on that. Until the arguments are
passed, the member is declined, which stops the walk rather than placing
anything wrongly, and no extent of `vol` is claimed.
"""

from __future__ import annotations

from situc.diagnostics import Source
from situc.dissector import generate
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag; u8 data[block]; }
struct vol { u16 block; entry(block = block) ent; u8 after; }
struct arms { u16 block; u8 k;
              variant body switch (k) { case 1: entry(block = block) one;
                                        default: u8 raw; }
              u8 after; }
"""


def _lua() -> str:
	schema = parse(Source("unit.situ", SCHEMA))
	return generate(schema, resolve(schema, solve(schema)), "unit")


def _dissector(lua: str, name: str) -> str:
	start = lua.index(f"function {name}.dissector(")
	return lua[start:lua.index("\nend\n", start)]


def test_the_member_is_declined_not_misread() -> None:
	lua  = _lua()
	body = _dissector(lua, "vol")
	assert "vol.ent: its parent hands it arguments" in body
	assert "entry.prefs.block" not in body
	assert "Dissector.get(\"entry\")" not in body
	# ...and nothing claims to know how long a `vol` is.
	assert "local function vol_extent" not in lua


def test_an_arm_is_declined_as_well() -> None:
	"""...and the variant stops the cursor, so `after` is not placed where
	the variant began. Its decline was a bare comment, which left `at`
	where it was and showed `after` there."""
	lua  = _lua()
	body = _dissector(lua, "arms")
	assert "entry.prefs.block" not in body
	assert "arms.after: not shown" in body
	assert "subtree:add(arms.fields.after" not in body
	assert "local function arms_extent" not in lua
