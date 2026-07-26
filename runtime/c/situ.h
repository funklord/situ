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
	SITU_ERR_STALE	    = 6   /* view outlived a layout-shifting mutation	*/
} situ_err_t;

/* A message: the caller's buffer plus the generation counter that detects
 * views outliving a mutation that shifted layout (section 12.3). The caller
 * owns the buffer; this structure never takes a copy. */
typedef struct situ_msg {
	uint8_t	 *base;
	uint32_t  size;
	uint32_t  generation;
} situ_msg_t;

/* A view: a resolved frame base plus a bounds limit, passed by value. Field
 * access inside a view is a constant offset from base, with the bounds check
 * amortized at acquisition. The generation is the one the owning message had
 * when this view was taken. */
typedef struct situ_view {
	uint8_t	 *base;
	uint32_t  limit;
	uint32_t  generation;
} situ_view_t;

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
 * Byte-order access
 *
 * Generated accessors go through these rather than casting a pointer, which
 * would be both an alignment fault and a strict-aliasing violation on the
 * targets that matter. Every one of them compiles to a load plus a byte swap
 * on a machine that has one.
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
 * A cursor over a tlv region. Items are found by walking from the start, which
 * is why the access axis reports Sequential and lookup by tag is O(n). The
 * cursor is the honest shape of the construct, not a limitation of this
 * implementation.
 * ------------------------------------------------------------------------ */

/* Protobuf wire types, which decide how a value's extent is found. */
#define SITU_WIRE_VARINT	0u
#define SITU_WIRE_64BIT		1u
#define SITU_WIRE_LENGTH	2u
#define SITU_WIRE_32BIT		5u

typedef struct situ_tlv_iter {
	situ_view_t view;
	uint32_t    offset;	/* cursor within the region			*/
	uint64_t    tag;	/* raw tag of the current item			*/
	uint32_t    value_off;	/* where the current item's value starts	*/
	uint32_t    value_len;	/* how long it is				*/
} situ_tlv_iter_t;

static inline void situ_tlv_begin(situ_tlv_iter_t *it, situ_view_t view)
{
	it->view      = view;
	it->offset    = 0;
	it->tag       = 0;
	it->value_off = 0;
	it->value_len = 0;
}

/* Advance to the next item. Returns SITU_OK, SITU_ERR_BOUNDS at the end of the
 * region or on a truncated item, or SITU_ERR_CONSTRAINT on a wire type situ
 * does not describe. */
static inline situ_err_t situ_tlv_next(situ_tlv_iter_t *it)
{
	const uint8_t *base = it->view.base;
	uint32_t limit      = it->view.limit;
	uint64_t tag        = 0;
	uint32_t used;

	if (it->offset >= limit) {
		return SITU_ERR_BOUNDS;
	}

	used = situ_varint_get(base + it->offset, limit - it->offset, 10u, &tag);
	if (used == 0u) {
		return SITU_ERR_BOUNDS;
	}

	it->tag     = tag;
	it->offset += used;

	switch ((uint32_t)(tag & 0x7u)) {
	case SITU_WIRE_VARINT: {
		uint64_t ignored = 0;
		used = situ_varint_get(base + it->offset, limit - it->offset, 10u, &ignored);
		if (used == 0u) {
			return SITU_ERR_BOUNDS;
		}
		it->value_off = it->offset;
		it->value_len = used;
		it->offset   += used;
		return SITU_OK;
	}
	case SITU_WIRE_64BIT:
		if (limit - it->offset < 8u) {
			return SITU_ERR_BOUNDS;
		}
		it->value_off = it->offset;
		it->value_len = 8u;
		it->offset   += 8u;
		return SITU_OK;
	case SITU_WIRE_32BIT:
		if (limit - it->offset < 4u) {
			return SITU_ERR_BOUNDS;
		}
		it->value_off = it->offset;
		it->value_len = 4u;
		it->offset   += 4u;
		return SITU_OK;
	case SITU_WIRE_LENGTH: {
		uint64_t length = 0;
		used = situ_varint_get(base + it->offset, limit - it->offset, 10u, &length);
		if (used == 0u) {
			return SITU_ERR_BOUNDS;
		}
		it->offset += used;
		if (length > (uint64_t)(limit - it->offset)) {
			return SITU_ERR_BOUNDS;
		}
		it->value_off = it->offset;
		it->value_len = (uint32_t)length;
		it->offset   += (uint32_t)length;
		return SITU_OK;
	}
	default:
		/* Wire types 3 and 4 are groups, which situ does not describe. */
		return SITU_ERR_CONSTRAINT;
	}
}

static inline uint32_t situ_tlv_field(const situ_tlv_iter_t *it)
{
	return (uint32_t)(it->tag >> 3);
}

static inline uint32_t situ_tlv_wire(const situ_tlv_iter_t *it)
{
	return (uint32_t)(it->tag & 0x7u);
}

static inline const uint8_t *situ_tlv_value(const situ_tlv_iter_t *it)
{
	return it->view.base + it->value_off;
}

/* Human-readable name for a code, for tests and diagnostics. Never NULL. */
const char *situ_err_str(situ_err_t err);

#ifdef __cplusplus
}
#endif

#endif /* SITU_H */
