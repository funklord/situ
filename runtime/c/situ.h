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

/* Human-readable name for a code, for tests and diagnostics. Never NULL. */
const char *situ_err_str(situ_err_t err);

#ifdef __cplusplus
}
#endif

#endif /* SITU_H */
