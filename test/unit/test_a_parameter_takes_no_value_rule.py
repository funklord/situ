"""A value rule on a `parameter` is refused (26.805).

    struct t { parameter u8 b [max = 10]; u8 x; }

Accepted, and the four backends gave three answers: C called
`situ_t_b_get`, which nobody emits, and C++ called `this->b()`, so neither
compiled, while Python and Rust emitted no check at all. A rule the
schema states and the generated code does not hold is what 14.5 calls
worse than stating nothing, so it is refused until `validate` checks
arguments. `[stream]` is still a parameter's own word.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.parser import parse_text

PREAMBLE = "target buffer;\nendian big;\n"


@pytest.mark.parametrize("rule", ["max = 10", "min = 1", "must_eq = 4"])
def test_a_value_rule_is_refused(rule: str) -> None:
	with pytest.raises(SituError) as refused:
		parse_text(PREAMBLE + f"struct t {{ parameter u8 b [{rule}]; u8 x; }}")
	assert "cannot constrain `parameter b` yet" in str(refused.value)


def test_stream_is_still_a_parameter_s_own_word() -> None:
	parse_text(PREAMBLE + "struct t { parameter u8 b [stream]; u8 x[b]; }")
