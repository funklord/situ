"""A registry declares a keyspace of typed cells (0070, step 2).

fuzznet keeps its settings as a table of C structs -- a key, a type spelled
as text, bounds, a default, a scope, a safe direction, what a client shows
-- because situ had nothing to say about keyed cells, and a client that
re-modelled the table lost what did not fit (fuzznet sec 1099). This is the
declaration: parsed, checked, dumped and round-tripped. What each reader
generates from it is the next step.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from situc.diagnostics import Source				# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.unparse import unparse				# noqa: E402

BASE = """target buffer;
endian big;

policy scope   { estate, host }
policy display { restart, sensitive, advanced, verb, advertised }

struct count {
	decimal u64  value[remaining]  [minimal];
}

struct text {
	u8  bytes[remaining]  [encoding = ascii];
	u8  pad[0];
}
"""

GOOD = BASE + """
registry node_settings {
	"revocation/k"  : count [min = 1, max = 255, scope = estate,
	                         safe = higher, unit = "root signatures"]
	                  "the estate's k";
	"net/metric"    : count [max = 255, default = "100", scope = host,
	                         display = advanced | advertised];
	"stream/grant/" : count [max = 3, scope = estate, display = verb];
	"host/name"     : text  [min_size = 1, max_size = 32, scope = host];
}
"""


def test_a_registry_parses_into_its_entries() -> None:
	schema = parse(Source("settings.situ", GOOD))
	[registry] = schema.registries()
	assert registry.name == "node_settings"
	assert [(entry.key, entry.type_name, entry.family)
	        for entry in registry.entries] == [
		("revocation/k", "count", False),
		("net/metric", "count", False),
		("stream/grant/", "count", True),
		("host/name", "text", False),
	]
	assert registry.entries[0].summary == "the estate's k"
	assert [decl.name for decl in schema.policies()] == ["scope", "display"]


def test_a_registry_round_trips() -> None:
	once  = unparse(parse(Source("settings.situ", GOOD)))
	twice = unparse(parse(Source("settings.situ", once)))
	assert once == twice
	assert '"stream/grant/" : count [max = 3' in once


@pytest.mark.parametrize("body, why", [
	('"a/b" : count; "a/b" : count;', "declared twice"),
	('"a/" : count; "a/x" : count;', "inside the family `a/`"),
	('"a/x" : count; "a/" : count;', "inside the family `a/`"),
	('"a//b" : count;', "is not a key"),
	('"" : count;', "is not a key"),
	('"a b" : count;', "is not a key"),
	('"a" : count [scope = planet];', "takes words of `policy scope`"),
	('"a" : count [colour = 3];', "is not a registry attribute"),
	('"a" : nope;', "is not a struct"),
	('"a" : text [max = 3];', "bounds a value `text` has not got"),
	('"a" : count [safe = up];', "`lower` or `higher`"),
	('"a" : count [default = 60];', "as it would be stored"),
	('"a" : count [window = -1];', "is a number"),
])
def test_a_registry_refuses_what_no_reader_could_act_on(
		body: str, why: str) -> None:
	with pytest.raises(Exception, match=why):
		parse(Source("settings.situ", BASE + "registry r { " + body + " }\n"))


def test_a_policy_owns_its_words() -> None:
	with pytest.raises(Exception, match="names a word twice"):
		parse(Source("p.situ", BASE.replace("{ estate, host }",
		                                    "{ estate, estate }")))
	with pytest.raises(Exception, match="a word situ owns"):
		parse(Source("p.situ", BASE + "policy unit { a }\n"))
	with pytest.raises(Exception, match="declared more than once"):
		parse(Source("p.situ", BASE + "registry count { }\n"))
