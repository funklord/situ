# 0062: a name the generated class already binds

Status: accepted
Date: 2026-10-05
Phase: raised by 26.568, which renamed a fixture member rather than answer it

## Context

A Python class scope is one namespace. This backend puts its own names in
it beside every member's accessor -- `at` acquires a view, `validate`
checks the constraints, `required` answers the framing question -- so a
member named one of those is a second binding of the same name:

```situ
struct record { u8 at; u16 tail; }
```

```python
>>> record.at
<property object at 0x...>
>>> record.at(msg, 0, 9)
TypeError: 'property' object is not callable
```

The module imports. It cannot be used: `at` is the only way to get a
view, and nothing in the output says a word. Which of the two bindings
survives is emission order rather than anything the schema states, so the
same collision silently loses the member in one case and the acquisition
in another.

Measured over a plain fixed-size struct, the four backends are in four
different positions:

    C       situ_record_at_get beside situ_record_view      unaffected
    C++     validate, size_bytes                   g++ refuses the header
    Rust    validate, required                     rustc E0592
    Python  at, validate, required                    silent, and broken

C is unaffected because every member accessor carries a suffix. C++ and
Rust refuse at compile time, loudly, with the schema's own member named
in the diagnostic. Python is the one that produces something that looks
like it worked.

Decision 0025 answered the neighbouring question -- a member taking its
own class's name in C++ -- by moving the class and aliasing it. It
rejected refusal there on the grounds that it "would make `framed`,
`validate`, `extent` and `at` reserved words in one backend of four, and
outlaw `struct option { u8 option; }` for a reason that has nothing to do
with its bytes". That argument does not transfer: here three backends of
four already reject the schema, two of them by refusing to compile, and
Python's acceptance is the defect rather than the thing being protected.

## Decision

**The Python backend refuses a class that binds one name twice, and the
diagnostic points at the member.** The schema keeps every name C accepts;
what moves is a member whose name this backend cannot express.

**The guard asks the finished module, not a list of reserved names.**
Every class in the emitted text is parsed and its class-scope bindings
counted. A list would be a second copy of the emitter, and would go short
by one the next time a class learns a method -- which is how this fault
arrived: `nesting`, `nesting_at`, `messages`, `message_text` and
`resolve_offsets` are all names a class acquires for some shape, and no
list written before them could have carried them.

Section 25 forbids a pass that re-reads its own output. This is not one,
for the reason `_unshadow` records beside it: both are a second pass over
what this emitter has just written, with complete knowledge of the first
rather than a reader guessing at a file.

**`@x.setter` is recognised rather than counted.** It binds `x` a second
time on purpose and is the only shape in this backend's output that does
-- 1992 of them over the corpus, and zero duplicates once they are
excluded, which is what makes this a guard rather than a new refusal.

**The two other backends keep their compile-time refusals for now.** A
guard of this shape needs a parser for the language it reads, and situc
has one only for Python. The front-end alternative -- reserving the union
of every backend's structural names -- is rejected here: the union holds
`read`, `write`, `word`, `check` and `framed`, all plausible field names,
and every one of them generates cleanly for the struct shapes that do not
produce the method. A refusal whose false positives outlaw `u8 read;`
costs more than a g++ diagnostic that names the member.

## Alternatives considered

**Mangle the member's accessor.** `at_` beside `set_at`, which is what
`py_name` already does for a keyword. Rejected on 0025's own reasoning
read the other way: a keyword collision has no escape and mangling is the
only answer available, while this one does have an escape -- the member
has a name the author chose and can choose again. Mangling would also
split a member's accessors across two spellings for a reason a reader of
the module cannot see, and would make the same member's name differ
between Python and the three backends that keep it.

**Rename the structural method.** `_situ_at`, keeping the member's `at`.
This is 0025's answer -- rename what the generator owns, not what the
schema chose -- and it is the strongest alternative. Rejected because
0025 could hide its rename behind an alias and this cannot: `at` is what
every caller writes and what every generated test calls, and a view class
whose acquisition is sometimes `at` and sometimes `_situ_at` has no
reliable surface at all. The cost falls on every reader of the module
rather than on one line of the schema.

**Reserve the names in the front end.** One diagnostic for all four
backends, and one name per member everywhere. Rejected on the union
above: the set is shape-dependent, so a front-end list is either
over-broad enough to outlaw ordinary field names or incomplete enough to
let the C++ and Rust cases through anyway.

## Consequences

A schema with a member named `at`, `validate` or `required` is refused by
the Python backend, naming the member and the class. None exists in this
repository: the corpus generates unchanged in both flag combinations that
change what a class body holds, which the guard's own sweep asserts.

The guard is exact, so it also catches the collisions nobody has met --
a member against `nesting`, `messages`, `resolve_offsets` or another
member's `set_` form -- without anybody adding them to anything.
