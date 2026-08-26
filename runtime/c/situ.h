/* situ.h -- minimal runtime for generated situ accessors: views, bounds,
 * generation tracking.
 *
 * Nothing here allocates, recurses, or uses a VLA, and the only headers it
 * pulls in are <stdint.h> and <stddef.h>. Generated code depends on this file
 * and on nothing else.
 *
 * SITU_CHECKED enables bounds and generation checking. Checked and unchecked
 * builds are ABI-compatible: no structure layout below depends on the flag,
 * so a checked caller can link against an unchecked library and vice versa.
 */

#ifndef SITU_H
#define SITU_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SITU_VERSION_MAJOR	0
#define SITU_VERSION_MINOR	1

/* Failure classes. One code per class; generated code never sets errno and
 * never longjmps. */
typedef enum situ_err {
	SITU_OK		    = 0,
	SITU_ERR_BOUNDS	    = 1,  /* access outside the buffer or view	*/
	SITU_ERR_CONSTRAINT = 2,  /* must_eq, max, must_be_zero violated	*/
	SITU_ERR_VERSION    = 3,  /* unknown version or variant discriminant	*/
	SITU_ERR_TAG	    = 4,  /* authentication tag stale or unverified	*/
	SITU_ERR_STAGE	    = 5,  /* region's stage gate has not been passed	*/
	SITU_ERR_STALE	    = 6,  /* view outlived a layout-shifting mutation	*/
	/* Not an error in the way the others are: the bytes so far are a valid
	 * prefix and more are needed. A caller reading from a stream gets this
	 * on every partial read, which is why it is not SITU_ERR_BOUNDS -- that
	 * one means a read went outside the buffer, which is a bug or an attack.
	 * Conflating them makes a receiver treat normal progress as hostile. */
	SITU_ERR_TRUNCATED  = 7
} situ_err_t;

/* A message: the caller's buffer plus the generation counter that detects
 * views outliving a mutation that shifted layout (section 12.3), and the set
 * of authentication tags currently stale (section 14.2). The caller owns the
 * buffer; this structure never takes a copy. */
typedef struct situ_msg {
	uint8_t	 *base;
	uint32_t  size;
	uint32_t  generation;
	/* One bit per tag or checksum declared in the struct, set when a covered
	 * field is written and cleared when that tag is recomputed. Thirty-two is
	 * a generous ceiling: coverage must be disjoint or nested, so a schema
	 * with more tags than this has other problems. */
	uint32_t  dirty;
} situ_msg_t;

/* Mark tags stale. Generated setters for covered fields call this; the mask is
 * the OR of the tag bits declared in the generated header. */
static inline void situ_msg_mark_dirty(situ_msg_t *msg, uint32_t tags)
{
	msg->dirty |= tags;
}

static inline void situ_msg_clear_dirty(situ_msg_t *msg, uint32_t tags)
{
	msg->dirty &= ~tags;
}

/* Whether the buffer is fit to transmit. A stale tag means the bytes no longer
 * authenticate, so handing them out would produce a message the receiver
 * rejects -- or worse, one it accepts because the tag was never checked. */
static inline situ_err_t situ_msg_transmittable(const situ_msg_t *msg)
{
	return msg->dirty == 0u ? SITU_OK : SITU_ERR_TAG;
}

/* A view: a resolved frame base plus a bounds limit, passed by value. Field
 * access inside a view is a constant offset from base, with the bounds check
 * amortized at acquisition. The generation is the one the owning message had
 * when this view was taken. */
typedef struct situ_view {
	uint8_t	 *base;
	uint32_t  limit;
	uint32_t  generation;
} situ_view_t;

/* One run of bytes a scattered transform runs over (13.2b).
 *
 * `base` is mutable because the scattered form of the tier-1 ABI works in
 * place: it exists for a transform that covers spans with something
 * uncovered between them, and the only reason to reach for it is to avoid
 * gathering those spans into a temporary. A codec that wrote its answer
 * somewhere else would have copied them after all.
 *
 * That is why the scattered form is confined to length-preserving codecs,
 * which is what section 14.1a already requires of a `covers` clause: in
 * place is only meaningful where the answer is the same size as the
 * question. */
typedef struct situ_span {
	uint8_t	 *base;
	uint32_t  len;
} situ_span_t;

/* Bind a message to a caller-supplied buffer. Generation starts at 1 so that
 * a zero-initialised view is never mistaken for a live one. */
void situ_msg_init(situ_msg_t *msg, uint8_t *buf, uint32_t size);

/* Record that the layout may have shifted, invalidating every outstanding
 * view. Generated setters call this on any mutation that can move subsequent
 * members; it wraps, which is harmless because a stale view has to survive
 * exactly 2^32 mutations to alias. */
void situ_msg_touch(situ_msg_t *msg);

/* Acquire a view of [offset, offset+extent) within the message. This is the
 * one bounds check; the accesses within the view are unchecked constant
 * offsets. */
situ_err_t situ_view_at(const situ_msg_t *msg, uint32_t offset, uint32_t extent, situ_view_t *out);

/* Narrow a view to a sub-range, preserving its generation. */
situ_err_t situ_view_sub(situ_view_t view, uint32_t offset, uint32_t extent, situ_view_t *out);

/* True when a range lies inside the view. Kept out of the checked-only path
 * because dynamic sizes need it in release builds too. */
static inline int situ_in_bounds(situ_view_t view, uint32_t offset, uint32_t extent)
{
	return extent <= view.limit && offset <= view.limit - extent;
}

#ifdef SITU_CHECKED

/* Assert that a view still matches its message. Generated accessors call this
 * on entry; it compiles to nothing in a release build. */
static inline situ_err_t situ_view_check(const situ_msg_t *msg, situ_view_t view)
{
	if (view.base == NULL || view.generation != msg->generation) {
		return SITU_ERR_STALE;
	}
	return SITU_OK;
}

static inline situ_err_t situ_bounds_check(situ_view_t view, uint32_t off, uint32_t ext)
{
	return situ_in_bounds(view, off, ext) ? SITU_OK : SITU_ERR_BOUNDS;
}

#else

static inline situ_err_t situ_view_check(const situ_msg_t *msg, situ_view_t view)
{
	(void)msg;
	(void)view;
	return SITU_OK;
}

static inline situ_err_t situ_bounds_check(situ_view_t view, uint32_t off, uint32_t ext)
{
	(void)view;
	(void)off;
	(void)ext;
	return SITU_OK;
}

#endif /* SITU_CHECKED */

/* ------------------------------------------------------------------------
 * Host byte order
 *
 * Decided by the compiler building this translation unit, and deliberately not
 * by whatever machine ran situc. Those are different machines whenever anyone
 * cross-compiles, and a generator that baked its own order into the output
 * would produce code that reads the wrong bytes on the target while compiling
 * without a murmur.
 *
 * Where it cannot be determined, this refuses rather than assuming little
 * endian. A wrong guess here is undetectable until the bytes are on the wire.
 * ------------------------------------------------------------------------ */

#ifndef SITU_HOST_BIG
#  if defined(__BYTE_ORDER__) && defined(__ORDER_BIG_ENDIAN__)
#    define SITU_HOST_BIG (__BYTE_ORDER__ == __ORDER_BIG_ENDIAN__)
#  elif defined(_WIN32)
#    define SITU_HOST_BIG 0
#  else
#    error "situ cannot determine the host byte order; define SITU_HOST_BIG to 0 or 1"
#  endif
#endif

/* ------------------------------------------------------------------------
 * Byte-order access
 *
 * Generated accessors go through these rather than casting a pointer, which
 * would be both an alignment fault and a strict-aliasing violation on the
 * targets that matter. Every one of them compiles to a load plus a byte swap
 * on a machine that has one.
 *
 * The `ne` forms are host order. `SITU_HOST_BIG` is a compile-time constant, so
 * the branch folds away and the call costs exactly what the fixed-order one
 * does.
 * ------------------------------------------------------------------------ */

static inline uint16_t situ_get_be16(const uint8_t *p)
{
	return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static inline uint32_t situ_get_be32(const uint8_t *p)
{
	return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
	     | ((uint32_t)p[2] <<  8) | ((uint32_t)p[3]);
}

static inline uint64_t situ_get_be64(const uint8_t *p)
{
	return ((uint64_t)situ_get_be32(p) << 32) | (uint64_t)situ_get_be32(p + 4);
}

static inline uint16_t situ_get_le16(const uint8_t *p)
{
	return (uint16_t)(((uint16_t)p[1] << 8) | (uint16_t)p[0]);
}

static inline uint32_t situ_get_le32(const uint8_t *p)
{
	return ((uint32_t)p[3] << 24) | ((uint32_t)p[2] << 16)
	     | ((uint32_t)p[1] <<  8) | ((uint32_t)p[0]);
}

static inline uint64_t situ_get_le64(const uint8_t *p)
{
	return ((uint64_t)situ_get_le32(p + 4) << 32) | (uint64_t)situ_get_le32(p);
}

static inline void situ_put_be16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)(v >> 8);
	p[1] = (uint8_t)(v);
}

static inline void situ_put_be32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)(v >> 24);
	p[1] = (uint8_t)(v >> 16);
	p[2] = (uint8_t)(v >>  8);
	p[3] = (uint8_t)(v);
}

static inline void situ_put_be64(uint8_t *p, uint64_t v)
{
	situ_put_be32(p,     (uint32_t)(v >> 32));
	situ_put_be32(p + 4, (uint32_t)(v));
}

static inline void situ_put_le16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)(v);
	p[1] = (uint8_t)(v >> 8);
}

static inline void situ_put_le32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)(v);
	p[1] = (uint8_t)(v >>  8);
	p[2] = (uint8_t)(v >> 16);
	p[3] = (uint8_t)(v >> 24);
}

static inline void situ_put_le64(uint8_t *p, uint64_t v)
{
	situ_put_le32(p,     (uint32_t)(v));
	situ_put_le32(p + 4, (uint32_t)(v >> 32));
}

static inline uint16_t situ_get_ne16(const uint8_t *p)
{
	return SITU_HOST_BIG ? situ_get_be16(p) : situ_get_le16(p);
}

static inline uint32_t situ_get_ne32(const uint8_t *p)
{
	return SITU_HOST_BIG ? situ_get_be32(p) : situ_get_le32(p);
}

static inline uint64_t situ_get_ne64(const uint8_t *p)
{
	return SITU_HOST_BIG ? situ_get_be64(p) : situ_get_le64(p);
}

static inline void situ_put_ne16(uint8_t *p, uint16_t v)
{
	if (SITU_HOST_BIG) {
		situ_put_be16(p, v);
	} else {
		situ_put_le16(p, v);
	}
}

static inline void situ_put_ne32(uint8_t *p, uint32_t v)
{
	if (SITU_HOST_BIG) {
		situ_put_be32(p, v);
	} else {
		situ_put_le32(p, v);
	}
}

static inline void situ_put_ne64(uint8_t *p, uint64_t v)
{
	if (SITU_HOST_BIG) {
		situ_put_be64(p, v);
	} else {
		situ_put_le64(p, v);
	}
}

/* ------------------------------------------------------------------------
 * Bit-field access
 *
 * Offsets are bit-valued because the solver's are (project.md section 26.2).
 * `off` is measured in bits from the start of the view, and `width` runs 1 to
 * 64. Straddling fields are handled by the same code as contained ones, which
 * is why there is no separate path for them.
 *
 * The two bit orders differ in how a byte's bits are numbered, and therefore
 * in which byte holds a multi-byte field's most significant bits:
 *
 *   msb_first  fills from bit 7 of each byte downward, so the byte stream
 *              reads as one big-endian bit string.
 *   lsb_first  fills from bit 0 upward, so an earlier byte holds the *less*
 *              significant bits.
 * ------------------------------------------------------------------------ */

static inline uint64_t situ_bits_get_msb(const uint8_t *base, uint32_t off, uint32_t width)
{
	uint32_t first = off / 8u;
	uint32_t last  = (off + width - 1u) / 8u;
	uint32_t skip  = off - first * 8u;
	uint64_t acc   = 0;
	uint32_t i;

	for (i = first; i <= last; i++) {
		acc = (acc << 8) | (uint64_t)base[i];
	}

	/* The accumulator holds (last - first + 1) whole bytes; drop the bits
	 * below the field and mask off the ones above it. */
	acc >>= ((last - first + 1u) * 8u) - skip - width;
	return width == 64u ? acc : acc & (((uint64_t)1 << width) - 1u);
}

static inline void situ_bits_set_msb(uint8_t *base, uint32_t off, uint32_t width, uint64_t v)
{
	uint32_t first = off / 8u;
	uint32_t last  = (off + width - 1u) / 8u;
	uint32_t skip  = off - first * 8u;
	uint32_t span  = (last - first + 1u) * 8u;
	uint64_t mask  = width == 64u ? ~(uint64_t)0 : (((uint64_t)1 << width) - 1u);
	uint64_t acc   = 0;
	uint32_t i;

	for (i = first; i <= last; i++) {
		acc = (acc << 8) | (uint64_t)base[i];
	}

	acc &= ~(mask << (span - skip - width));
	acc |= (v & mask) << (span - skip - width);

	for (i = last + 1u; i > first; i--) {
		base[i - 1u] = (uint8_t)(acc & 0xFFu);
		acc >>= 8;
	}
}

static inline uint64_t situ_bits_get_lsb(const uint8_t *base, uint32_t off, uint32_t width)
{
	uint32_t first = off / 8u;
	uint32_t last  = (off + width - 1u) / 8u;
	uint32_t skip  = off - first * 8u;
	uint64_t acc   = 0;
	uint32_t i;

	/* Earlier bytes carry the less significant bits, so assemble downward. */
	for (i = last + 1u; i > first; i--) {
		acc = (acc << 8) | (uint64_t)base[i - 1u];
	}

	acc >>= skip;
	return width == 64u ? acc : acc & (((uint64_t)1 << width) - 1u);
}

static inline void situ_bits_set_lsb(uint8_t *base, uint32_t off, uint32_t width, uint64_t v)
{
	uint32_t first = off / 8u;
	uint32_t last  = (off + width - 1u) / 8u;
	uint32_t skip  = off - first * 8u;
	uint64_t mask  = width == 64u ? ~(uint64_t)0 : (((uint64_t)1 << width) - 1u);
	uint64_t acc   = 0;
	uint32_t i;

	for (i = last + 1u; i > first; i--) {
		acc = (acc << 8) | (uint64_t)base[i - 1u];
	}

	acc &= ~(mask << skip);
	acc |= (v & mask) << skip;

	for (i = first; i <= last; i++) {
		base[i] = (uint8_t)(acc & 0xFFu);
		acc >>= 8;
	}
}

static inline uint64_t situ_bits_get_ne(const uint8_t *base, uint32_t off, uint32_t width)
{
	return SITU_HOST_BIG ? situ_bits_get_msb(base, off, width)
	                     : situ_bits_get_lsb(base, off, width);
}

static inline void situ_bits_set_ne(uint8_t *base, uint32_t off, uint32_t width,
        uint64_t v)
{
	if (SITU_HOST_BIG) {
		situ_bits_set_msb(base, off, width, v);
	} else {
		situ_bits_set_lsb(base, off, width, v);
	}
}

/* Sign-extend a value of `width` bits held in the low bits of `raw`. */
static inline int64_t situ_sign_extend(uint64_t raw, uint32_t width)
{
	if (width >= 64u) {
		return (int64_t)raw;
	}
	{
		uint64_t sign = (uint64_t)1 << (width - 1u);
		return (int64_t)((raw ^ sign) - sign);
	}
}

/* ------------------------------------------------------------------------
 * Text validation (section 8.6)
 *
 * situ has no string type: text is `u8 name[N]`, and `[encoding]` says what
 * the bytes are supposed to be. Saying it is only worth anything if something
 * checks, so the generated validator calls these.
 *
 * Strict, in the sense RFC 3629 requires and for the reason it gives: an
 * overlong encoding or a surrogate is a second spelling of a character that
 * already has one, and a receiver that accepts both accepts two byte
 * sequences for one value. That is the malleability problem reserved bits have
 * (section 8.8), in a different costume.
 * ------------------------------------------------------------------------ */

/* The content length of a nul-terminated field whose declared size is its
 * capacity: the offset of the first zero byte, or `capacity` if there is none.
 *
 * Returning the capacity for an unterminated field rather than reading past it
 * is the whole point: the field is a fixed number of bytes and this must not
 * be the function that leaves it. `situ_nul_terminated` is what asks whether
 * the terminator was actually there. */
static inline uint32_t situ_nul_len(const uint8_t *data, uint32_t capacity)
{
	uint32_t i;

	for (i = 0; i < capacity; i++) {
		if (data[i] == 0u) {
			return i;
		}
	}
	return capacity;
}

static inline int situ_nul_terminated(const uint8_t *data, uint32_t capacity)
{
	return situ_nul_len(data, capacity) < capacity;
}

static inline uint32_t situ_min_u32(uint32_t a, uint32_t b)
{
	return a < b ? a : b;
}

/* The bound every leaf of a size expression is held to (14.2b).
 *
 * A length at or past the view's limit saturates against the frame anyway, so
 * holding a *field* to this changes nothing a caller can observe -- and it is
 * what keeps the arithmetic that follows inside `int64_t`. Without it a varint
 * a lying message set to 1.6e19 made `(n + 1) * 2` overflow, wrap negative,
 * and read as *zero* once clamped: the member after it then landed a few bytes
 * in, well inside the frame, and was read rather than refused. An overflow
 * must saturate high, where the frame clamps it, and never collapse to zero.
 */
#define SITU_LEAF_MAX 0x7FFFFFFF

/* One leaf of a size expression, held to `SITU_LEAF_MAX`. Two spellings
 * because the sign matters: a `u64` varint above the bound is a huge length,
 * and an `i16` below zero is a negative one, and casting the first to signed
 * would turn it into the second. */
static inline int64_t situ_leaf_u64(uint64_t value)
{
	if (value > (uint64_t)SITU_LEAF_MAX) {
		return SITU_LEAF_MAX;
	}
	return (int64_t)value;
}

static inline int64_t situ_leaf_i64(int64_t value)
{
	if (value >  SITU_LEAF_MAX) { return  SITU_LEAF_MAX; }
	if (value < -SITU_LEAF_MAX) { return -SITU_LEAF_MAX; }
	return value;
}

/* A computed length, with a negative result read as zero (14.2b).
 *
 * Zero rather than a refusal: a member of negative length is a member with
 * nothing in it, every backend can say that, and `validate` still refuses the
 * message for the constraint that made it negative -- which is the error the
 * reader wants, rather than one about arithmetic.
 */
static inline uint32_t situ_nonneg_u32(int64_t value)
{
	if (value <= 0) {
		return 0u;
	}
	/* Saturating at the top as well, and this half was missed once: bounded
	 * leaves keep the arithmetic inside `int64_t`, and the *result* can
	 * still exceed `uint32_t` -- `(SITU_LEAF_MAX + 1) * 2` does. Casting
	 * truncated it to zero, which put the member after it a few bytes in
	 * rather than past the frame: the exact failure the bound was added to
	 * prevent, one step further along. */
	return value > (int64_t)UINT32_MAX ? UINT32_MAX : (uint32_t)value;
}

/* How many bytes remain in a view from `at`. Saturating, and that is the whole
 * of it: `at` is arithmetic over length fields the message controls, so it can
 * exceed the limit whatever the schema says. `limit - at` in `uint32_t` then
 * reports about four billion bytes remaining, and a `[remaining]` member hands
 * out that length with a pointer aimed past the end of the buffer.
 *
 * Found by fuzzing a schema that had a `[remaining]` tail after two
 * attacker-controlled lengths -- and found only once every schema started
 * being fuzzed rather than two of them. */
static inline uint32_t situ_remaining_u32(uint32_t limit, uint32_t at)
{
	return at >= limit ? 0u : limit - at;
}

/* Advance an offset by a length the message chose, and stop at the view.
 *
 * The other half of the same hole, one step further on. `situ_remaining_u32`
 * keeps a *length* inside the frame; this keeps an *offset* there. A member
 * placed after a variable-length region has an offset that is the sum of what
 * precedes it, and one of those terms is a field an attacker fills in: for
 * `example/packet`, `hdr.length = 0xffff` puts the tag 65581 bytes into a
 * 62-byte view, and the accessor handed back that pointer.
 *
 * Saturating rather than wrapping, and that is the point: `at + by` in
 * `uint32_t` with a 32-bit length field wraps to a small number, which is an
 * offset inside the frame pointing at bytes that are not the member. A
 * clamped offset is wrong in a way `validate` can report; a wrapped one is
 * wrong in a way nothing can see.
 *
 * Found by fuzzing `example/packet` under an address sanitizer, three seconds
 * into the first run that was fuzzing rather than eight random inputs. */
static inline uint32_t situ_advance_u32(uint32_t at, uint32_t by, uint32_t limit)
{
	const uint32_t room = situ_remaining_u32(limit, at);

	return at + (by < room ? by : room);
}

/* `pad_to(n)` (decision 0043): advance `at` to the next multiple of `n`,
 * clamped to the view. The padding is `align_up(at, n) - at`; a member after
 * a pad starts on an n-byte boundary from the message base. Clamped for the
 * same reason `situ_advance_u32` is: `at` is a sum of lengths the message
 * chose, so the aligned offset may sit past a short frame, and `validate`
 * reports that rather than the accessor running off the end. */
static inline uint32_t situ_align_up_u32(uint32_t at, uint32_t n, uint32_t limit)
{
	const uint32_t pad = (n - (at % n)) % n;

	return situ_advance_u32(at, pad, limit);
}

/* Delimited members (section 8.6.1).
 *
 * `situ_scan` returns the offset of the first occurrence of `delim` within
 * `limit` bytes, or `limit` when it is not there. The caller distinguishes the
 * two: a member whose delimiter is absent is truncated, not empty, and a
 * getter is not the place to decide what to do about that.
 *
 * Naive matching, deliberately. A delimiter is one or two bytes in every
 * format this targets, so the setup cost of anything cleverer exceeds the scan
 * it would save, and the generated code stays something a reader can check
 * against the spec they are implementing.
 */
static inline uint32_t situ_scan(const uint8_t *data, uint32_t limit,
        const uint8_t *delim, uint32_t delim_len)
{
	uint32_t i;
	uint32_t j;

	if (delim_len == 0u || delim_len > limit) {
		return limit;
	}

	for (i = 0u; i + delim_len <= limit; i++) {
		for (j = 0u; j < delim_len; j++) {
			if (data[i + j] != delim[j]) {
				break;
			}
		}
		if (j == delim_len) {
			return i;
		}
	}
	return limit;
}

/* The same, with a byte that makes the delimiter inert.
 *
 * `quote` toggles: inside a quoted run the delimiter is content. `escape`
 * applies to the byte after it, including a quote byte and including itself.
 * Either may be `SITU_NO_BYTE` to say the format does not have one.
 *
 * A quoted run left open at the end of the buffer finds no delimiter, which
 * is the same answer as a delimiter that is not there -- and the right one,
 * since the content the schema describes has not been terminated.
 */
#define SITU_NO_BYTE 0x100u

static inline uint32_t situ_scan_relaxed(const uint8_t *data, uint32_t limit,
        const uint8_t *delim, uint32_t delim_len,
        uint32_t quote, uint32_t escape)
{
	uint32_t i;
	uint32_t j;
	int      quoted = 0;

	if (delim_len == 0u || delim_len > limit) {
		return limit;
	}

	for (i = 0u; i + delim_len <= limit; i++) {
		if (escape != SITU_NO_BYTE && data[i] == (uint8_t)escape) {
			i++;		/* the next byte is content, whatever it is */
			continue;
		}
		if (quote != SITU_NO_BYTE && data[i] == (uint8_t)quote) {
			quoted = !quoted;
			continue;
		}
		if (quoted) {
			continue;
		}
		for (j = 0u; j < delim_len; j++) {
			if (data[i + j] != delim[j]) {
				break;
			}
		}
		if (j == delim_len) {
			return i;
		}
	}
	return limit;
}

/* Whether the content of a bare delimited member is representable.
 *
 * Without a quote or escape byte the content may not contain the delimiter:
 * writing back content that did would produce different framing, so such a
 * field did not come from this schema. For a CRLF-framed protocol this is the
 * header-injection check, which is why it is generated rather than remembered.
 */
static inline int situ_delimiter_absent(const uint8_t *data, uint32_t len,
        const uint8_t *delim, uint32_t delim_len)
{
	return situ_scan(data, len, delim, delim_len) == len;
}

/* Text-encoded numbers (section 8.6.2).
 *
 * Returns 0 on success. A conversion that can fail is why `repr` reports
 * TextConverted rather than ValueConverted: a byte swap is total, and `12x4`
 * is not a number.
 *
 * Refused, each for a reason a protocol cares about:
 *
 *   - an empty run, because no digits is not the number zero
 *   - a byte that is not a digit in this base, including trailing space
 *   - a value above `max`, which is the declared type's range
 *
 * Overflow is checked before it happens rather than after. Detecting it by
 * looking for a result that got smaller is a wrap, which is undefined for
 * signed types and merely wrong for unsigned ones.
 */
static inline int situ_parse_uint(const uint8_t *data, uint32_t len,
        uint32_t radix, uint64_t max, uint64_t *out)
{
	uint64_t value = 0u;
	uint32_t i;

	if (len == 0u || radix < 2u || radix > 16u) {
		return -1;
	}

	for (i = 0u; i < len; i++) {
		uint8_t  c = data[i];
		uint32_t digit;

		if (c >= (uint8_t)'0' && c <= (uint8_t)'9') {
			digit = (uint32_t)(c - (uint8_t)'0');
		} else if (c >= (uint8_t)'a' && c <= (uint8_t)'f') {
			digit = (uint32_t)(c - (uint8_t)'a') + 10u;
		} else if (c >= (uint8_t)'A' && c <= (uint8_t)'F') {
			digit = (uint32_t)(c - (uint8_t)'A') + 10u;
		} else {
			return -1;
		}

		if (digit >= radix) {
			return -1;
		}
		if (value > (max - digit) / radix) {
			return -1;
		}
		value = value * radix + digit;
	}

	*out = value;
	return 0;
}

/* Optional whitespace, and case-insensitive tokens (section 8.6.4).
 *
 * Space and horizontal tab, and nothing else. Not `isspace`, which is locale
 * dependent and includes CR, LF, VT and FF -- three of which are delimiters in
 * the protocols this is for, so trimming them would eat the framing. This is
 * HTTP's OWS and SIP's LWS, which is the set the formats actually mean.
 */
static inline int situ_is_ows(uint8_t byte)
{
	return byte == (uint8_t)' ' || byte == (uint8_t)'\t';
}

static inline uint32_t situ_trim_start(const uint8_t *data, uint32_t len)
{
	uint32_t i = 0u;

	while (i < len && situ_is_ows(data[i])) {
		i++;
	}
	return i;
}

/* The length of the content with the whitespace at both ends removed. */
static inline uint32_t situ_trim_len(const uint8_t *data, uint32_t len)
{
	uint32_t start = situ_trim_start(data, len);
	uint32_t end   = len;

	while (end > start && situ_is_ows(data[end - 1u])) {
		end--;
	}
	return end - start;
}

/* Write a value as fixed-width digits, which is `situ_parse_uint` backwards.
 *
 * Fixed width, so the leading zeros are mandatory rather than optional: a
 * field declared `hex u32 x[8]` is eight digits whatever the value, and that
 * is what makes one value one byte sequence. The only spelling freedom left
 * is case, and this writes upper -- cpio's `newc` header, ASN.1's and
 * SMTP's numbers are all upper, and a lower-case digit is refused on the way
 * in rather than tolerated here (26.86).
 *
 * Returns 0, or -1 where the value needs more digits than the field has.
 */
static inline int situ_format_uint(uint8_t *data, uint32_t len, uint32_t radix,
        uint64_t value)
{
	uint32_t i;

	if (len == 0u || radix < 2u || radix > 16u) {
		return -1;
	}

	for (i = len; i > 0u; i--) {
		const uint32_t digit = (uint32_t)(value % radix);

		data[i - 1u] = (uint8_t)(digit < 10u
		    ? (uint32_t)'0' + digit
		    : (uint32_t)'A' + digit - 10u);
		value /= radix;
	}
	return value == 0u ? 0 : -1;
}

/* Whether these digits are the spelling `situ_format_uint` would write.
 *
 * The owned form stores a *value*, so it can only give back the one spelling
 * of it. A field carrying another -- a lower-case hex digit -- is a second
 * encoding of the same number, which is what `canonical` exists to report,
 * and the honest answer is to refuse the decode rather than to round-trip it
 * into something else.
 */
static inline int situ_digits_canonical(const uint8_t *data, uint32_t len)
{
	uint32_t i;

	for (i = 0u; i < len; i++) {
		if (data[i] >= (uint8_t)'a' && data[i] <= (uint8_t)'f') {
			return 0;
		}
	}
	return 1;
}

/* ASCII case folding, and only ASCII.
 *
 * `tolower` is locale dependent: in a Turkish locale it maps `I` to a dotless
 * form, so a header name would stop matching itself. A protocol token is
 * ASCII by definition, and this folds exactly that range.
 */
static inline uint8_t situ_ascii_fold(uint8_t byte)
{
	return (byte >= (uint8_t)'A' && byte <= (uint8_t)'Z')
	       ? (uint8_t)(byte + 32u) : byte;
}

static inline int situ_bytes_eq(const uint8_t *a, uint32_t alen,
        const uint8_t *b, uint32_t blen)
{
	uint32_t i;

	if (alen != blen) {
		return 0;
	}
	for (i = 0u; i < alen; i++) {
		if (a[i] != b[i]) {
			return 0;
		}
	}
	return 1;
}

static inline int situ_ascii_ci_eq(const uint8_t *a, uint32_t alen,
        const uint8_t *b, uint32_t blen)
{
	uint32_t i;

	if (alen != blen) {
		return 0;
	}
	for (i = 0u; i < alen; i++) {
		if (situ_ascii_fold(a[i]) != situ_ascii_fold(b[i])) {
			return 0;
		}
	}
	return 1;
}

/* Whether digits are the one spelling of their value (section 8.6.2).
 *
 * A leading zero is another spelling of the same number, and for hexadecimal
 * so is a change of case. `[minimal]` is what asks for this; without it the
 * field is NonCanonical and the map says so, which is the honest default --
 * most formats do permit `007`, and refusing it would reject valid data.
 */
static inline int situ_digits_minimal(const uint8_t *data, uint32_t len,
        uint32_t radix)
{
	uint32_t i;

	if (len == 0u) {
		return 0;
	}
	if (len > 1u && data[0] == (uint8_t)'0') {
		return 0;		/* a leading zero, and the value is not zero */
	}
	if (radix > 10u) {
		for (i = 0u; i < len; i++) {
			if (data[i] >= (uint8_t)'A' && data[i] <= (uint8_t)'F') {
				return 0;	/* upper case is the second spelling */
			}
		}
	}
	return 1;
}

static inline int situ_ascii_valid(const uint8_t *data, uint32_t len)
{
	uint32_t i;

	for (i = 0; i < len; i++) {
		if (data[i] > 0x7Fu) {
			return 0;
		}
	}
	return 1;
}

static inline int situ_utf8_valid(const uint8_t *data, uint32_t len)
{
	uint32_t i = 0;

	while (i < len) {
		uint8_t  lead  = data[i];
		uint32_t extra;
		uint32_t code;
		uint32_t least;
		uint32_t k;

		if (lead < 0x80u) {
			i++;
			continue;
		}

		if (lead >= 0xC2u && lead <= 0xDFu) {
			extra = 1u; code = lead & 0x1Fu; least = 0x80u;
		} else if (lead >= 0xE0u && lead <= 0xEFu) {
			extra = 2u; code = lead & 0x0Fu; least = 0x800u;
		} else if (lead >= 0xF0u && lead <= 0xF4u) {
			extra = 3u; code = lead & 0x07u; least = 0x10000u;
		} else {
			return 0;	/* 0x80-0xC1 continuation or overlong lead, 0xF5+ */
		}

		if (i + extra >= len) {
			return 0;	/* truncated: the sequence runs off the end */
		}

		for (k = 1u; k <= extra; k++) {
			if ((data[i + k] & 0xC0u) != 0x80u) {
				return 0;
			}
			code = (code << 6) | (uint32_t)(data[i + k] & 0x3Fu);
		}

		if (code < least) {
			return 0;	/* overlong: a second spelling of a shorter form */
		}
		if (code >= 0xD800u && code <= 0xDFFFu) {
			return 0;	/* a surrogate half, which UTF-8 may not carry */
		}
		if (code > 0x10FFFFu) {
			return 0;
		}

		i += extra + 1u;
	}
	return 1;
}

/* UTF-16 (decision 0044). The code unit's byte order is the encoding's, not
 * the field's, so LE and BE are separate names and separate validators; the
 * shared core takes the order. A lone surrogate -- a high or low half with no
 * partner -- decodes to no character and is the UTF-16 analogue of utf8's
 * overlong form (section 8.8), so it is rejected the same way. */
static inline int situ_utf16_valid(const uint8_t *data, uint32_t len, int big)
{
	uint32_t i = 0;

	if ((len & 1u) != 0u) {
		return 0;	/* an odd byte count is not whole code units */
	}
	while (i < len) {
		const uint32_t hi   = big ? data[i] : data[i + 1u];
		const uint32_t lo   = big ? data[i + 1u] : data[i];
		const uint32_t unit = (hi << 8) | lo;

		if (unit >= 0xD800u && unit <= 0xDBFFu) {
			/* A high surrogate needs a low one right after it. */
			uint32_t low;

			if (i + 4u > len) {
				return 0;	/* no room for the pair */
			}
			low = big ? (uint32_t)((data[i + 2u] << 8) | data[i + 3u])
			          : (uint32_t)((data[i + 3u] << 8) | data[i + 2u]);
			if (low < 0xDC00u || low > 0xDFFFu) {
				return 0;	/* a high surrogate not followed by a low one */
			}
			i += 4u;
			continue;
		}
		if (unit >= 0xDC00u && unit <= 0xDFFFu) {
			return 0;	/* a low surrogate with no high one before it */
		}
		i += 2u;
	}
	return 1;
}

static inline int situ_utf16le_valid(const uint8_t *data, uint32_t len)
{
	return situ_utf16_valid(data, len, 0);
}

static inline int situ_utf16be_valid(const uint8_t *data, uint32_t len)
{
	return situ_utf16_valid(data, len, 1);
}

/* ------------------------------------------------------------------------
 * Checksums (section 26.15)
 *
 * A `checksum` field tells a caller which bytes are covered and when the value
 * has gone stale; it does not compute anything, because the algorithm is not
 * something the layout knows. These are the algorithms small enough to ship:
 * a few lines each, no tables, no library. Anything needing a real
 * implementation behind it -- a cryptographic hash, deflate -- stays tier-1
 * and optional.
 *
 * They are `static inline`, so a schema that names none of them emits none of
 * them. That is what makes the optional tier actually optional.
 * ------------------------------------------------------------------------ */

/* The internet checksum of RFC 1071: the one's complement of the one's
 * complement sum of 16-bit big-endian words, with an odd trailing byte padded.
 *
 * Its defining property is that a block carrying its own checksum sums to
 * zero, which is how a receiver verifies without recomputing separately. */
static inline uint16_t situ_checksum_internet(const uint8_t *data, uint32_t len)
{
	uint32_t total = 0;
	uint32_t i;

	for (i = 0; i + 1u < len; i += 2u) {
		total += ((uint32_t)data[i] << 8) | (uint32_t)data[i + 1u];
	}
	if ((len & 1u) != 0u) {
		total += (uint32_t)data[len - 1u] << 8;	/* pad the odd byte low */
	}

	/* End-around carry, twice: folding can itself carry. */
	total = (total & 0xFFFFu) + (total >> 16);
	total = (total & 0xFFFFu) + (total >> 16);

	return (uint16_t)(~total & 0xFFFFu);
}

/* Fletcher-16 over bytes, modulo 255. */
static inline uint16_t situ_fletcher16(const uint8_t *data, uint32_t len)
{
	uint32_t a = 0;
	uint32_t b = 0;
	uint32_t i;

	for (i = 0; i < len; i++) {
		a = (a + (uint32_t)data[i]) % 255u;
		b = (b + a) % 255u;
	}
	return (uint16_t)((b << 8) | a);
}

/* Fletcher-32 over 16-bit *little-endian* words, modulo 65535.
 *
 * The word order is not a free choice: the published test vectors only come
 * out right this way, and reading the words big-endian gives a byte-swapped
 * near-miss that looks plausible. */
static inline uint32_t situ_fletcher32(const uint8_t *data, uint32_t len)
{
	uint32_t a = 0;
	uint32_t b = 0;
	uint32_t i;

	for (i = 0; i + 1u < len; i += 2u) {
		a = (a + (((uint32_t)data[i + 1u] << 8) | (uint32_t)data[i])) % 0xFFFFu;
		b = (b + a) % 0xFFFFu;
	}
	if ((len & 1u) != 0u) {
		a = (a + (uint32_t)data[len - 1u]) % 0xFFFFu;
		b = (b + a) % 0xFFFFu;
	}
	return (b << 16) | a;
}

/* Adler-32 (RFC 1950), the checksum zlib carries. */
static inline uint32_t situ_adler32(const uint8_t *data, uint32_t len)
{
	uint32_t a = 1;
	uint32_t b = 0;
	uint32_t i;

	for (i = 0; i < len; i++) {
		a = (a + (uint32_t)data[i]) % 65521u;
		b = (b + a) % 65521u;
	}
	return (b << 16) | a;
}

/* ------------------------------------------------------------------------
 * Packed binary-coded decimal (section 8.1)
 *
 * Each nibble holds one decimal digit, most significant first, which is what
 * RTC chips and financial formats put on the wire. The conversion lives here
 * rather than in generated code because it is a loop, and because a loop that
 * is written once and tested once is better than one emitted per field.
 *
 * A nibble above nine is a bit pattern that is not a number. Decoding cannot
 * report that, so `situ_bcd_valid` exists to be asked first; the generated
 * validator asks it.
 * ------------------------------------------------------------------------ */

/* Whether every nibble of `packed` below `digits` is a decimal digit. */
static inline int situ_bcd_valid(uint64_t packed, uint32_t digits)
{
	uint32_t i;

	for (i = 0; i < digits; i++) {
		if (((packed >> (4u * i)) & 0xFu) > 9u) {
			return 0;
		}
	}
	return 1;
}

/* The number `packed` spells. Nibbles above nine contribute their value, so
 * the result of decoding invalid input is unspecified but bounded; check with
 * situ_bcd_valid first where that matters. */
static inline uint64_t situ_bcd_decode(uint64_t packed, uint32_t digits)
{
	uint64_t value = 0;
	uint32_t i;

	for (i = digits; i > 0u; i--) {
		value = value * 10u + ((packed >> (4u * (i - 1u))) & 0xFu);
	}
	return value;
}

/* The packed form of `value`, truncated to `digits` digits. */
static inline uint64_t situ_bcd_encode(uint64_t value, uint32_t digits)
{
	uint64_t packed = 0;
	uint32_t i;

	for (i = 0; i < digits; i++) {
		packed |= (value % 10u) << (4u * i);
		value  /= 10u;
	}
	return packed;
}

/* ------------------------------------------------------------------------
 * Variable-length integers
 *
 * LEB128: base-128 groups, least significant first, with the top bit of each
 * byte set on every group but the last. `minimal` is a schema property rather
 * than a decoder one -- a decoder that rejected non-minimal encodings could not
 * read protobuf -- so these accept them, and the capability map records that
 * the format is not canonical as a result.
 * ------------------------------------------------------------------------ */

/* Decode one varint. Returns bytes consumed, or 0 if the buffer ends mid-value
 * or the value needs more than `max_bytes`. */
static inline uint32_t situ_varint_get(const uint8_t *p, uint32_t avail,
        uint32_t max_bytes, uint64_t *out)
{
	uint64_t acc   = 0;
	uint32_t shift = 0;
	uint32_t i;

	for (i = 0; i < avail && i < max_bytes; i++) {
		uint8_t byte = p[i];

		if (shift < 64u) {
			acc |= (uint64_t)(byte & 0x7Fu) << shift;
		}
		shift += 7u;

		if ((byte & 0x80u) == 0u) {
			*out = acc;
			return i + 1u;
		}
	}

	return 0;
}

/* Decode one big-endian base-128 varint: the high group first, otherwise the
 * same shape as leb128. ASN.1's identifier octets, MIDI's delta times and
 * SQLite's record varints are all this.
 *
 * `max_bytes` is where the encoding stops, and `terminal_bits` is what the last
 * permitted byte carries. Where that is eight there is no spare bit for a
 * continuation flag, so the byte is read whole and ends the value whatever its
 * high bit says -- SQLite's ninth byte, and the reason a nine-byte varint holds
 * sixty-four bits where seven-bit groups would need ten.
 *
 * Returns bytes consumed, or 0 if the buffer ends mid-value. */
static inline uint32_t situ_varint_be_get(const uint8_t *p, uint32_t avail,
        uint32_t max_bytes, uint32_t terminal_bits, uint64_t *out)
{
	uint64_t acc = 0;
	uint32_t i;

	for (i = 0; i < avail && i < max_bytes; i++) {
		uint8_t byte = p[i];

		if (terminal_bits == 8u && i + 1u == max_bytes) {
			*out = (acc << 8) | (uint64_t)byte;
			return i + 1u;
		}

		acc = (acc << 7) | (uint64_t)(byte & 0x7Fu);
		if ((byte & 0x80u) == 0u) {
			*out = acc;
			return i + 1u;
		}
	}

	return 0;
}

/* Encoded length of a value under `situ_varint_be_get`'s rules, for the
 * minimality check: a longer encoding of the same value is a second encoding. */
static inline uint32_t situ_varint_be_len(uint64_t value, uint32_t max_bytes,
        uint32_t terminal_bits)
{
	uint32_t n = 1;

	while (value >= 0x80u) {
		value >>= 7;
		n++;
	}

	/* The whole-byte terminal form encodes one more value per length than the
	 * grouped one, so a value needing every grouped byte fits in the terminal
	 * byte instead. */
	if (terminal_bits == 8u && n > max_bytes) {
		n = max_bytes;
	}
	return n;
}

/* Encoded length of a value, so a writer can tell whether it still fits. */
static inline uint32_t situ_varint_len(uint64_t value)
{
	uint32_t n = 1;

	while (value >= 0x80u) {
		value >>= 7;
		n++;
	}
	return n;
}

/* Encode one varint minimally. Returns bytes written, or 0 if too small. */
static inline uint32_t situ_varint_put(uint8_t *p, uint32_t avail, uint64_t value)
{
	uint32_t n = situ_varint_len(value);
	uint32_t i;

	if (n > avail) {
		return 0;
	}

	for (i = 0; i + 1u < n; i++) {
		p[i] = (uint8_t)((value & 0x7Fu) | 0x80u);
		value >>= 7;
	}
	p[n - 1u] = (uint8_t)(value & 0x7Fu);
	return n;
}

/* ZigZag, as protobuf's sint32 and sint64 use it: a small magnitude stays short
 * whether it is positive or negative. */
static inline uint64_t situ_zigzag_encode(int64_t value)
{
	return ((uint64_t)value << 1) ^ (uint64_t)(value >> 63);
}

static inline int64_t situ_zigzag_decode(uint64_t raw)
{
	return (int64_t)((raw >> 1) ^ (~(raw & 1u) + 1u));
}

/* ------------------------------------------------------------------------
 * Tag-length-value iteration
 *
 * There is none here, deliberately. A cursor over a tlv region used to live in
 * this file with `tag >> 3`, `tag & 0x7` and protobuf's four wire types written
 * into it -- which is a description of one format, in the runtime, beside the
 * schema language whose whole purpose is to describe formats. Generated code
 * never called it; the only caller was a test that hand-wrote the field
 * dispatch its schema already declared.
 *
 * A tlv region's items are found the way its schema says they are, so the walk
 * is generated per region from `tag_decode` and `value_size` (section 9.5).
 * What belongs here is what that walk is built out of: `situ_varint_get`
 * above, and the bounds-checked view operations.
 * ------------------------------------------------------------------------ */

/* ------------------------------------------------------------------------
 * Secret material
 * ------------------------------------------------------------------------ */

/* Overwrite a buffer so the compiler may not elide the store (section 14.6).
 * A plain memset over storage that is about to die is dead code by the
 * standard's rules, and compilers do remove it; the volatile pointer is what
 * keeps this one. */
void situ_zeroize(void *buf, uint32_t len);

/* Human-readable name for a code, for tests and diagnostics. Never NULL. */
const char *situ_err_str(situ_err_t err);

#ifdef __cplusplus
}
#endif

#endif /* SITU_H */
