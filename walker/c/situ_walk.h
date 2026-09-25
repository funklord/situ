/* An embedded walker: read a packed layout image over live bytes.
 *
 * This is the walker decision 0026 was argued from -- a radio whose framing
 * must change without a firmware rebuild -- and decision 0035 records why it
 * is C. The Python walker in `walker/` is the fifth column of the
 * differential check and is not this; nothing here is a port of it.
 *
 * WHAT IT PROMISES.
 *
 *   * No allocation. Every function takes what it writes into. An embedded
 *     walker in a fixed arena is the caller 0031's caller buffers describe.
 *   * No recursion, and no unbounded loop. Section 10's language is total --
 *     no calls, no recursion, no iteration -- so a program's length is its
 *     own bound and the evaluator needs no step limit. A guard that cannot
 *     fire is worse than none, because it suggests the danger it does not
 *     address; that argument is 0026's and it is why this can be shipped to
 *     a device at all.
 *   * No libc beyond <stdint.h> and <stddef.h>.
 *
 * WHAT IT DOES NOT DO YET. Delimited members, varints, runs, variants,
 * regions and the `validate` probes. Those are the rest of the walk, and
 * 0035 sizes them. What is here is the spine: the image, the expression
 * evaluator, and a member placed and read.
 */
#ifndef SITU_WALK_H
#define SITU_WALK_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Matches `situ_err_t` in the runtime, so a caller mixing the two reads one
 * set of codes. The walker adds none of its own: a construct it cannot read
 * is `SITU_WALK_UNSUPPORTED`, which is a statement about this build rather
 * than about the bytes. */
typedef enum {
	SITU_WALK_OK          = 0,
	SITU_WALK_BOUNDS      = 1,
	SITU_WALK_CONSTRAINT  = 2,
	/* A discriminant naming no arm, where the variant's default is `error`
	 * (14.5). It is a message this build cannot READ rather than one that
	 * breaks a rule, which is the schema's distinction and not this
	 * walker's -- `situ_err_t` has carried the code all along and the four
	 * backends return it. This walker had no spelling for it while it
	 * declined every variant, and adding the check without adding the code
	 * would have folded a VERSION answer into CONSTRAINT at the one moment
	 * the two walkers finally had something to compare. */
	SITU_WALK_VERSION     = 3,
	SITU_WALK_MALFORMED   = 8,   /* the image is not one */
	SITU_WALK_UNSUPPORTED = 9,   /* a construct this build does not render */
	/* A `parameter` the schema declares and the caller did not supply
	 * (decision 0050). Its own code because it is a statement about the
	 * CALL -- not about the bytes, which nothing has read, and not about
	 * this build, which renders the construct perfectly well. Folding it
	 * into either would report a verdict on a message nobody looked at,
	 * which is the shape `situ verify` moved its own refusal for.
	 *
	 * Never a default. situ does not know the caller's block size, and a
	 * walk under a guessed one reads the wrong bytes confidently. */
	SITU_WALK_ARGUMENT    = 10
} situ_walk_err;

/* `none`, as `std/image.situ` spells it. */
#define SITU_WALK_NONE 0xffffffffu

typedef struct {
	const uint8_t *image;
	uint32_t       image_len;

	const uint8_t *structs;
	uint32_t       struct_count;
	uint32_t       struct_stride;

	const uint8_t *placements;
	uint32_t       placement_count;
	uint32_t       placement_stride;

	const uint8_t *code;
	uint32_t       code_len;

	const uint8_t *varints;
	uint32_t       varint_count;
	uint32_t       varint_stride;

	const uint8_t *delimiters;
	uint32_t       delimiter_count;
	uint32_t       delimiter_stride;
	/* `skip`: one row per byte of a member's lead set, sorted by
	 * placement and consecutive under it, the way delimiters are. */
	const uint8_t *skips;
	uint32_t       skip_count;
	uint32_t       skip_stride;

	/* One row per *arm*, so a variant has several and they are contiguous:
	 * the table is sorted by placement like every other. */
	const uint8_t *arms;
	uint32_t       arm_count;
	uint32_t       arm_stride;

	/* One row per check, several per member, contiguous and in the order
	 * they are asked -- which is part of the schema's meaning rather than
	 * an implementation detail, the first failure being the answer. */
	const uint8_t *constraints;
	uint32_t       constraint_count;
	uint32_t       constraint_stride;

	/* One row per enum member: the values an enum admits, for the
	 * membership check a `default = error` enum makes. */
	const uint8_t *enum_values;
	uint32_t       enum_value_count;
	uint32_t       enum_value_stride;

	/* One row per endian marker: the `little` sentinel its field is read
	 * big-endian and compared against (decision 0035). */
	const uint8_t *markers;
	uint32_t       marker_count;
	uint32_t       marker_stride;

	/* One row per member inside an `authenticated` or `sealed` region, and
	 * one for each region member itself: the region it sits in and that
	 * region's flags (bit 0 sealed, bit 1 `[allow_unverified_read]`). A
	 * region names itself, so a gate's interior is every member whose owner
	 * is the gate (decision 0035). */
	const uint8_t *regions;
	uint32_t       region_count;
	uint32_t       region_stride;

	/* One row per struct that carries a `[version]` field: the placement
	 * index of that field, which a `[since]` member is gated on. Keyed by
	 * shape (decision 0035). */
	const uint8_t *versions;
	uint32_t       version_count;
	uint32_t       version_stride;

	/* One row per struct that names itself: the depth the format allows and
	 * the depth the schema asks a reader to spend (0054). Keyed by shape.
	 *
	 * What this replaces is a number chosen for a corpus. `WALK_DEPTH_MAX`
	 * bounded the walk and was compared against nothing the schema
	 * declared, so a schema saying 32 met a walker allowing 8 and the walk
	 * stopped early in silence. It is a ceiling now rather than the answer:
	 * the schema's number is used where it is lower, and where it is higher
	 * the walker refuses by name instead of quietly measuring short. */
	/* An `indexed` region's offset table geometry (section 9.3): one row
	 * per such member, holding the entry width in bits, the bytecode for
	 * the entry count, and what an offset is measured from (0024).
	 *
	 * The section existed and this build did not read it, so `validate`
	 * could not ask whether `count * entry_bytes` fits the frame -- the
	 * one check every backend makes about such a table -- and the packer
	 * marked any struct holding one unvalidatable. */
	/* A `tlv` region's grammar (section 9.5): the selector's bytecode and
	 * the tag varint's decoder parameters, and beside it one row per value
	 * rule saying how far an item's value reaches.
	 *
	 * Neither section was read by this build, and until the rules existed
	 * there was nothing in the first worth reading: the record said how to
	 * find an item's TAG and nothing about its VALUE, so no walker could
	 * count the items in one.
	 *
	 * `tlv_count_` has the trailing underscore because `situ_walk_count` is
	 * already a function: a member of that name shadows nothing in C, but
	 * the two reading alike in a diff is how the wrong one gets used. */
	/* The byte runs a `[must_eq]` on a run pins, and a token set's arms
	 * (0052, 0055): one row per alternative, consecutive under the
	 * placement, in declaration order.
	 *
	 * This build had none, so `check_pinned_run` was a kind it did not
	 * render -- which made every struct holding a token set unanswerable
	 * to it while the Python walk answered. Honest, and it meant the two
	 * could not be compared about `smtp`, `http` or `edges` at all. */
	const uint8_t *pinned_runs;
	uint32_t       pinned_run_count;
	uint32_t       pinned_run_stride;

	const uint8_t *tlvs;
	uint32_t       tlv_count_;
	uint32_t       tlv_stride;

	const uint8_t *tlv_rules;
	uint32_t       tlv_rule_count;
	uint32_t       tlv_rule_stride;

	const uint8_t *indexes;
	uint32_t       index_count;
	uint32_t       index_stride;

	const uint8_t *depths;
	uint32_t       depth_count;
	uint32_t       depth_stride;

	/* What the schema says about a message beyond its layout (0051): one
	 * row per `when`, keyed by the shape whose members its predicate
	 * reads. Only the `refuse` rows change a verdict -- `warn` and `note`
	 * say something about a message that conforms -- and this build reads
	 * them for exactly that, because a walker that skipped a refusal would
	 * call a message legal that the four backends refuse. */
	const uint8_t *messages;
	uint32_t       message_count;
	uint32_t       message_stride;

	/* The arguments this walk was acquired with (decision 0050), in the
	 * order the schema declares its `parameter` members. `arg_count` is
	 * what says how many there are; NULL and zero both mean none, and a
	 * parameter with no argument behind it is refused rather than read as
	 * zero.
	 *
	 * Here, and not on a view, because this walker has no view: every entry
	 * point takes `(image, message, len, shape)` and the image binding is
	 * the one of those that belongs to the CALLER and lives as long as the
	 * walk. Python's walker carries the same list on the `View` its
	 * `acquire` returns; the two differ in where the caller keeps them and
	 * in nothing a walk can observe.
	 *
	 * That makes them per-binding rather than per-message, which is the
	 * right lifetime for the only kind of parameter that can move a
	 * member: `[stream]` says the fact is negotiated once and then fixed.
	 * A caller with a second stream calls `situ_walk_acquire` again.
	 *
	 * `situ_walk_open` zeroes the whole struct, so a binding nobody
	 * supplied arguments to reads as "none supplied" -- which is refused at
	 * the point of use rather than read as zero. */
	const int64_t *args;
	uint32_t       arg_count;
	/* The struct they were supplied FOR. Python's walker gets this for
	 * free -- its arguments live on the View under the placement indices of
	 * the struct that View is over, so another struct's parameter is simply
	 * not in the map. Here the arguments are positional and a second
	 * parameterised struct would take the first one's value silently, which
	 * is the wrong-number-confidently failure this construct was refused
	 * for. Read only where `args` is non-NULL, so zero is not a claim. */
	uint32_t       arg_shape;
} situ_walk_image;

/* One member, as the image describes it. */
typedef struct {
	uint8_t  kind;
	uint8_t  endian;
	/* Byte 2 of the placement row, which this walker skipped over for its
	 * whole life: it read `flags` from byte 3 correctly and never looked at
	 * the one before it. A bit-packed field cannot be assembled without it
	 * (26.264). */
	uint8_t  bit_order;
	uint8_t  flags;
	uint32_t offset_bits;
	uint32_t size_bits;
	/* The upper bound, and a *pin* rather than a reachable bound where
	 * SITU_WALK_PINNED is set. Read only for that case: a length is clamped
	 * to a pin and never to an ordinary maximum, because the compiled
	 * backends clamp to what is left in the view instead. */
	uint32_t size_max_bits;
	uint32_t element_bits;
	uint32_t array_count;
	uint32_t size_code;
	uint32_t repeat_code;
	uint32_t type_struct;
	uint32_t located_code;
	/* A text number's base, 2 to 16, and zero for a member that is not one.
	 * `radix_digits` is how many digits the schema declared, which is the
	 * fixed-width form's width; the delimited form's comes from the scan. */
	uint8_t  radix;
	/* `image_placement.text_flags`, which is the placement's second flag
	 * byte rather than a text number's alone -- `flags` is full, and
	 * SITU_WALK_PARAMETER lives here for that reason.
	 *
	 * Bit 2 is `[case_insensitive]`, which for a token set is a property of
	 * the SET rather than of the member (0055) -- so a pinned-run check
	 * that ignored it refuses `helo` where every other reader takes it. */
	uint8_t  text_flags;
	uint16_t radix_digits;
	/* `max` on a `while` run: the ceiling the schema put on how many
	 * elements one may hold, and zero where it stated none. */
	uint16_t repeat_cap;
	/* `pad_to(n)` alignment in bytes, 0 where the member is not padding
	 * (decision 0043). */
	uint16_t pad_to;
} situ_walk_placement;

/* Bits of `situ_walk_placement.flags` a caller needs.
 *
 * `SITU_WALK_SIGNED` is not a detail. A value comes back in a `uint64_t`
 * with the sign extended through it, so `-2` and `18446744073709551614` are
 * the same answer and the caller decides which it is looking at -- and a
 * caller with no way to ask decides wrongly. The differential printed a
 * signed element unsigned and reported a disagreement that was not there,
 * which is what a missing accessor looks like from outside. */
#define SITU_WALK_OFFSET_KNOWN 0x01u
/* The member's size is a constant the schema fixed, rather than one the
 * message declares. It travels with `OFFSET_KNOWN` as a PAIR: a member whose
 * size is fixed and whose offset is not was never bounds-checked by the
 * acquisition, so the walk checks it -- and a member that declares its own
 * length is the `fits_frame` case instead, never fixed-size, so the two do
 * not overlap. This walker knew only the offset half and refused both at the
 * first guard, which agreed on the verdict and named the wrong check. */
#define SITU_WALK_SIZE_FIXED   0x04u
#define SITU_WALK_SIGNED       0x10u
/* A `tag` or `checksum` member (decision 0035). A caller asks `present=`
 * of it rather than for a value -- `situ_walk_bytes` answers, since its
 * question is exactly whether the tag's span is inside the frame. The
 * caller runs the algorithm; the walker only says the bytes are there. */
#define SITU_WALK_IS_TAG       0x40u
/* `[size = N]` pinned this member's footprint (decision 0039). */
#define SITU_WALK_PINNED       0x80u

/* A bit of `situ_walk_placement.text_flags`, not of `flags`.
 *
 * A `parameter` (decision 0050): an argument the caller supplies, which the
 * message does not carry. The member occupies NOTHING, so the member after
 * it begins where it began -- and its row still says `offset_bits` and
 * `size_bits`, those being where that next member starts and how wide the
 * argument is. A walker that missed the flag would therefore read the next
 * member's first byte and hand it back as the argument: a wrong value that
 * reads exactly like a right one, which is why every backend refused the
 * construct until a view could carry one. */
#define SITU_WALK_PARAMETER    0x40u

/* Bind an image. Every table it names is bounds-checked against the whole
 * before anything reads one, because the image is the least trusted input
 * this component has. */
situ_walk_err situ_walk_open(situ_walk_image *out,
                                 const uint8_t *image, uint32_t len);

/* Supply the arguments `shape`'s `parameter` members take (decision 0050).
 *
 * The C answer to Python's `acquire`, and it does that function's argument
 * half: the bounds check has no counterpart here, every entry point making
 * its own. `args` are positional, in the order the schema declares them --
 * the placement table's order, which is the one thing both walkers have,
 * names living in the image's optional tail that a device omits.
 *
 * SITU_WALK_ARGUMENT where `count` is not exactly what `shape` takes. Too
 * few is the case that matters and too many is refused for the same reason
 * read backwards: an argument nothing declares is one the caller believes
 * is being used.
 *
 * The array is BORROWED and must outlive every walk made through this
 * binding; nothing here allocates.
 *
 * Optional for a schema with no parameters, which is every schema written
 * before 0050: `situ_walk_open` leaves the binding with none, and a walk
 * that never meets one never asks. */
situ_walk_err situ_walk_acquire(situ_walk_image *image, uint32_t shape,
                                const int64_t *args, uint32_t count);

/* How many members a struct has, and where they start. */
situ_walk_err situ_walk_members(const situ_walk_image *image, uint32_t shape,
                                    uint32_t *first, uint32_t *count);

/* One placement, decoded out of the table. */
situ_walk_err situ_walk_placement_at(const situ_walk_image *image,
                                         uint32_t index,
                                         situ_walk_placement *out);

/* One arm of a variant, decoded out of the table.
 *
 * `chosen` is SITU_WALK_NONE for `default: error`, which names no member:
 * a discriminant reaching it makes the message one this build cannot READ
 * rather than one that breaks a rule, which is 14.5's distinction and the
 * reason that case has a verdict of its own.
 *
 * `selects` is the discriminant's own placement and is the same for every
 * arm of one variant. It is carried per row because that is how the image
 * stores it, and repeating it here is cheaper than a second accessor. */
typedef struct {
	uint32_t chosen;
	int64_t  when;
	uint32_t selects;
	uint8_t  flags;
} situ_walk_arm;

/* Bits of `situ_walk_arm.flags`. A row carrying either names no case value
 * to match: DEFAULT answers whatever matched nothing, and ERROR answers
 * nothing at all. */
#define SITU_WALK_ARM_DEFAULT 0x01u
#define SITU_WALK_ARM_ERROR   0x02u

/* How many arms a variant has, SITU_WALK_UNSUPPORTED where the member is
 * not one.
 *
 * The walk has read this table since it learned variants and kept it to
 * itself, so a caller could compare a variant's VERDICT against another
 * reader and not its contents -- the arm is where the bytes are, and it is
 * reached by no public entry point. `situ_walk_bytes`, `situ_walk_count`
 * and `situ_walk_element` all take an arm's placement happily; what was
 * missing was any way to learn the placement. */
situ_walk_err situ_walk_arms(const situ_walk_image *image, uint32_t index,
                                 uint32_t *count);

/* One arm, decoded, `which` counting from zero in declaration order. */
situ_walk_err situ_walk_arm_at(const situ_walk_image *image, uint32_t index,
                                   uint32_t which, situ_walk_arm *out);

/* Decode one varint at `at`, answering the bytes it consumed and the value.
 *
 * Two encodings, differing in which end the groups come from: `leb128` puts
 * the low group first, `be128` the high one -- ASN.1's identifier octets,
 * MIDI's delta times, SQLite's record varints. `terminal_bits` of eight is
 * the case worth naming: the last permitted byte has no spare bit for a
 * continuation flag, so it is read whole and ends the value whatever its
 * high bit says. That is SQLite's ninth byte, and it is why nine bytes hold
 * sixty-four bits where seven-bit groups would need ten.
 *
 * SITU_WALK_BOUNDS where the buffer ends mid-value, which is what the getter
 * does in every backend. */
situ_walk_err situ_walk_varint(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t index, uint32_t at,
                                   uint32_t *consumed, uint64_t *value);

/* Where a delimited member's content stops, and whether the delimiter was
 * there. `at` is the member's own byte offset.
 *
 * The two answers are separate on purpose, and the C runtime says why: a
 * member whose delimiter is absent is *truncated*, not empty, and it reaches
 * as far as the cap or the buffer allowed -- so the member after it starts
 * at that point rather than being unplaceable. The member's span is the
 * content plus the delimiter where there is one, which is `situ_walk_size_
 * bits`; the content alone is what a backend's `_ptr` and `_len` hand back.
 *
 * Naive matching, as the generated code does it: a delimiter is one or two
 * bytes in every format this targets, and a reader has to be able to check
 * it against the specification they are implementing. */
situ_walk_err situ_walk_scan(const situ_walk_image *image,
                                 const uint8_t *message, uint32_t len,
                                 uint32_t index, uint32_t at,
                                 uint32_t *content, int *terminated,
                                 uint32_t *took);

/* How wide a member is, in bits. A constant where the image knows one; a
 * `size_code` program otherwise, which is what `size = Bounded` costs.
 * SITU_WALK_UNSUPPORTED for a width this build cannot compute -- a `while`
 * run or a variant arm. */
situ_walk_err situ_walk_size_bits(const situ_walk_image *image,
                                      const uint8_t *message, uint32_t len,
                                      uint32_t shape, uint32_t index,
                                      uint32_t *out);

/* Where a member starts, in bits from the message base.
 *
 * A constant where the image knows one, and otherwise the members before it
 * summed -- which is the answer `offset = Dynamic` names, and the reason a
 * walk costs what the capability map says it costs. */
situ_walk_err situ_walk_offset_bits(const situ_walk_image *image,
                                        const uint8_t *message, uint32_t len,
                                        uint32_t shape, uint32_t index,
                                        uint32_t *out);

/* A member's value, from a message. Fixed offsets and widths up to 64 bits;
 * anything else answers SITU_WALK_UNSUPPORTED rather than a number, because
 * a wrong length is indistinguishable from a right one once it leaves. */
situ_walk_err situ_walk_read(const situ_walk_image *image,
                                 const uint8_t *message, uint32_t len,
                                 uint32_t shape, uint32_t index,
                                 uint64_t *out);

/* How many elements a run holds: a declared count, the `size_code` program
 * the message answers, or -- for a `while` run -- however many elements are
 * there before one fails the predicate.
 *
 * SITU_WALK_UNSUPPORTED for a member that is not a run, and for a text
 * number, whose bracket is digits rather than elements. */
/* How many items a `tlv` region holds (section 9.5), and how many entries
 * an `indexed` region's offset table holds (9.3).
 *
 * Separate from `situ_walk_count`, which answers for a COUNTED run -- a
 * declared count or a `size_code` program -- and whose refusals `walk.py`
 * mirrors exactly. These two are different questions with different
 * answers, and folding them in would have moved that function's population
 * without moving the Python one.
 *
 * A `tlv` count is a walk: nothing in the region records one, so each
 * item's tag is read, the selector decoded out of it, and the rule that
 * selector names says how far the value reaches. An `indexed` count is an
 * evaluation, the number being a program the image carries.
 *
 * SITU_WALK_UNSUPPORTED where the image does not describe the region --
 * an image written before those sections carried a selector or a count. */
situ_walk_err situ_walk_tlv_count(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  uint32_t *out);

situ_walk_err situ_walk_index_count(const situ_walk_image *image,
                                    const uint8_t *message, uint32_t len,
                                    uint32_t shape, uint32_t index,
                                    uint32_t *out);

situ_walk_err situ_walk_count(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  uint32_t *out);

/* One element of a run, by index rather than by pointer.
 *
 * A run has no single value and `situ_walk_read` refuses one; this is how a
 * caller asks for the values it does have. The elements are the bytes, so
 * this is the same read at a different offset -- which is deliberately one
 * function rather than two, a backend and its own run accessor having once
 * disagreed about exactly that. */
situ_walk_err situ_walk_element(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t shape, uint32_t index,
                                   uint32_t at, uint64_t *out);

/* A member's bytes: where they start in `message`, and how many.
 *
 * For the runs and arrays that have no scalar value, and for a delimited
 * member, whose span carries its delimiter because that is what places the
 * member after it. Points into the caller's buffer and copies nothing: an
 * embedded walker in a fixed arena has nowhere to copy to. */
situ_walk_err situ_walk_bytes(const situ_walk_image *image,
                                  const uint8_t *message, uint32_t len,
                                  uint32_t shape, uint32_t index,
                                  const uint8_t **out, uint32_t *count);

/* An endian marker's verdict: whether its field, read big-endian, equals the
 * `little` sentinel the schema gave it -- 1 for little, 0 otherwise.
 *
 * The marker is what decides the message's byte order, so it is read in the
 * one order that does not depend on the answer -- big-endian, whatever it
 * turns out to say. SITU_WALK_UNSUPPORTED for a member that is not a marker,
 * SITU_WALK_BOUNDS where the frame does not reach it. */
situ_walk_err situ_walk_marker(const situ_walk_image *image,
                                   const uint8_t *message, uint32_t len,
                                   uint32_t shape, uint32_t index,
                                   uint32_t *little);

/* A sealed region's gate, and whether this build can open it (decision
 * 0035, 14.3). `*opened` and `*refused` are both 1 for a gate this build
 * answers: situ guards the bytes and admits a verification that would pass,
 * refuses one that would fail -- the caller runs the cipher, so the answer
 * does not depend on the bytes and is comparable without running anybody's.
 *
 * SITU_WALK_UNSUPPORTED for a member that is not a sealed region, or one the
 * schema waived with `[allow_unverified_read]` -- then there is no gate to
 * open, and the differential skips it rather than name an `_open` no backend
 * emits. */
situ_walk_err situ_walk_gate(const situ_walk_image *image, uint32_t index,
                                 uint32_t *opened, uint32_t *refused);

/* The `ordinal`-th plain scalar inside sealed region `gate`, by placement
 * index, so a caller can read the interior a tag exists to protect through
 * the gate `situ_walk_gate` opened. Read it with `situ_walk_read`.
 *
 * Only the scalars: a `[secret]` member has no debug accessor by design
 * (14.6) and a run inside a gate is spelled several ways not yet compared,
 * so both are skipped -- the same subset `report._gated` renders.
 *
 * SITU_WALK_UNSUPPORTED where `gate` is not a sealed region; SITU_WALK_BOUNDS
 * where `ordinal` is past the last interior scalar, which is how a caller
 * loops until it runs out. */
situ_walk_err situ_walk_gated(const situ_walk_image *image, uint32_t gate,
                                  uint32_t ordinal, uint32_t *out_index);

/* Is this message a well-formed instance of the struct?
 *
 * `*verdict` is SITU_WALK_OK, SITU_WALK_BOUNDS for a frame that does not
 * hold what the message claims, or SITU_WALK_CONSTRAINT for a rule it
 * breaks. Which of those comes first is part of the schema's meaning, not an
 * implementation detail: the first failure is the answer, so the order the
 * image lists the checks in is the order they are asked.
 *
 * The return value is a different question from the verdict. It is
 * SITU_WALK_UNSUPPORTED where this build cannot answer *at all* -- because
 * the image says it does not carry every check for this struct, or because
 * it carries a kind of check this build does not render. Whole or nothing:
 * `validate` is one answer about a whole struct, so a partial one reports OK
 * where the schema refuses, which is the one shape of wrong answer that
 * cannot be told from a right one.
 */
situ_walk_err situ_walk_validate(const situ_walk_image *image,
                                     const uint8_t *message, uint32_t len,
                                     uint32_t shape, situ_walk_err *verdict);

/* Which check refused, beside the verdict (0051).
 *
 * `report.failed_check` answers this in the Python walk and has since
 * 26.231; the generated C names the member from `check(view, &which)` since
 * 26.232. This walk answered neither, so the two walkers could agree on a
 * verdict while disagreeing about WHY -- which the corpus comparison could
 * not see, because it compared only the code.
 *
 * `placement` is SITU_WALK_NONE and `check` is SITU_WALK_NO_CHECK where the
 * struct validates, and where the refusal is the struct's own minimum: a
 * frame too short for the whole struct has no member that broke it, which is
 * the same answer the generated `check` gives with its no-check sentinel.
 *
 * Recorded on the way OUT rather than reconstructed by a second pass. The
 * order the checks are asked in is part of the schema's meaning -- the first
 * failure is the answer -- and a second walk would be a second
 * implementation of that order, free to disagree with the first. */
typedef struct {
	uint32_t placement;
	uint8_t  check;			/* an `image_check` kind */
} situ_walk_why;

#define SITU_WALK_NO_CHECK 0xffu

situ_walk_err situ_walk_failed_check(const situ_walk_image *image,
                                     const uint8_t *message, uint32_t len,
                                     uint32_t shape, situ_walk_err *verdict,
                                     situ_walk_why *why);

/* Evaluate a section 10 program. `field` reads a placement's value for the
 * expression, and is the only thing tying this to a message. */
/* `base` is the byte offset the referenced field's struct sits at in the
 * frame being walked, and is zero for a field of the struct itself. A
 * placement's own offset is within its own struct, so a field of a nested
 * struct needs the nesting offset too -- without it the read lands at the
 * right offset of the wrong struct (26.184). */
/* What an expression is asking ABOUT a placement.
 *
 * Section 10's builtins are four questions of one member, and the bytecode
 * has an opcode each. One callback answers all four because they differ only
 * in the question: the caller already holds the message, the shape and the
 * depth, and a second callback per question would be three more chances for
 * one of them to be wired to the wrong walk.
 *
 * `SIZE` and `OFFSET` are in BYTES, which is what `walker/vm.py`'s callers
 * pass -- `size_bits(view, i) // BITS_PER_BYTE`. Answering in bits here
 * would be a walker that evaluates every size expression eight times too
 * large and agrees with itself about it. */
typedef enum {
	SITU_WALK_ASK_VALUE  = 0,   /* the field's own value */
	SITU_WALK_ASK_SIZE   = 1,   /* how many bytes it occupies */
	SITU_WALK_ASK_OFFSET = 2,   /* where it starts, in bytes */
	SITU_WALK_ASK_COUNT  = 3    /* how many elements a run holds */
} situ_walk_ask;

typedef situ_walk_err (*situ_walk_load)(void *ctx, situ_walk_ask what,
                                            uint32_t index, int32_t base,
                                            int64_t *out);

situ_walk_err situ_walk_eval(const situ_walk_image *image, uint32_t at,
                                 situ_walk_load load, void *ctx,
                                 int64_t remaining, int64_t *out);

#ifdef __cplusplus
}
#endif

#endif /* SITU_WALK_H */
