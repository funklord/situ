/* situ.hpp -- the C++ face of the situ runtime.
 *
 * There is no second runtime. This is a header over `situ.h`, whose functions
 * are already `extern "C"`, because a second implementation of the same
 * arithmetic is a second thing to get wrong (decision 0017 makes the same
 * argument about codecs).
 *
 * What C++ adds is not speed. It is that three things C can only document
 * become things the compiler enforces:
 *
 *   * a byte array carries its length, so a caller cannot pass the pointer
 *     without it
 *   * an error cannot be ignored, because the return is [[nodiscard]]
 *   * a sealed region's view cannot be constructed at all except by the
 *     function that verifies it -- in C the struct is there to be filled in by
 *     anybody determined enough
 *
 * C++17, and freestanding: no allocation, no exceptions, no RTTI, and nothing
 * from the standard library beyond <cstdint> and <cstddef>. `span` below is
 * twenty lines rather than <span> for that reason -- a freestanding toolchain
 * may not ship the header, and the part of it worth having is small.
 */

#ifndef SITU_HPP
#define SITU_HPP

#include <cstddef>
#include <cstdint>

#include "situ.h"

/* The runtime lives in `situ::rt`, not `situ`, because generated code lives in
 * `situ` by default and a schema is free to declare `struct message` or
 * `struct view`. A runtime that squatted on those names would make a
 * legitimate schema fail to compile for a reason the author could not see. */
namespace situ::rt {

/* The error codes, scoped. Same values as `situ_err_t`, so the two convert
 * freely and generated code can call the C runtime without translating. */
enum class err : int {
	ok         = SITU_OK,
	bounds     = SITU_ERR_BOUNDS,
	constraint = SITU_ERR_CONSTRAINT,
	version    = SITU_ERR_VERSION,
	tag        = SITU_ERR_TAG,
	stage      = SITU_ERR_STAGE,
	/* Not an error in the way the others are: the bytes so far are a valid
	 * prefix and more are needed. Separate from `bounds`, which means a read
	 * went outside the buffer -- a bug or an attack. Conflating them makes a
	 * receiver treat normal progress as hostile. */
	truncated  = SITU_ERR_TRUNCATED,
};

constexpr bool ok(err e) noexcept { return e == err::ok; }

/* A pointer and a length that travel together.
 *
 * The C backend hands out a bare `uint8_t *` and a `_COUNT` macro, and nothing
 * makes a caller use the second with the first. This is the whole of what
 * <span> offers that matters here, and none of what it offers that does not.
 */
template <typename T>
class span {
public:
	constexpr span() noexcept : data_(nullptr), size_(0) {}
	constexpr span(T *data, std::size_t size) noexcept
		: data_(data), size_(size) {}

	constexpr T          *data()  const noexcept { return data_; }
	constexpr std::size_t size()  const noexcept { return size_; }
	constexpr bool        empty() const noexcept { return size_ == 0; }

	constexpr T *begin() const noexcept { return data_; }
	constexpr T *end()   const noexcept { return data_ + size_; }

	constexpr T &operator[](std::size_t i) const noexcept { return data_[i]; }

private:
	T          *data_;
	std::size_t size_;
};

using bytes       = span<std::uint8_t>;
using const_bytes = span<const std::uint8_t>;

/* A message: the buffer, and the generation that invalidates views of it.
 *
 * Held by reference rather than owned. situ never allocates, so a message
 * wrapping a buffer it did not make has nothing to free, and a destructor
 * here would imply an ownership that does not exist.
 */
class message {
public:
	message(std::uint8_t *buffer, std::uint32_t length) noexcept
	{
		situ_msg_init(&raw_, buffer, length);
	}

	[[nodiscard]] err transmittable() const noexcept
	{
		return static_cast<err>(situ_msg_transmittable(&raw_));
	}

	/* The obligations over these bytes: a tag that no longer matches them
	 * (section 14.2), or a field that no longer equals what it derives from
	 * (section 16.1). One word for both, because a message is either ready
	 * to send or it is not.
	 *
	 * These are not [[nodiscard]]: they return nothing, and the thing a
	 * caller must not drop is `transmittable`, which is. */
	void mark_dirty(std::uint32_t bits) noexcept
	{
		situ_msg_mark_dirty(&raw_, bits);
	}

	void clear_dirty(std::uint32_t bits) noexcept
	{
		situ_msg_clear_dirty(&raw_, bits);
	}

	[[nodiscard]] bool is_stale(std::uint32_t bits) const noexcept
	{
		return (raw_.dirty & bits) != 0u;
	}

	situ_msg_t       *raw()       noexcept { return &raw_; }
	const situ_msg_t *raw() const noexcept { return &raw_; }

private:
	situ_msg_t raw_;
};

/* A view: a base, a limit, and the generation it was taken at.
 *
 * A value, deliberately. It owns nothing and refers to bytes somebody else
 * holds, so copying it is free and correct, and a destructor would be a lie
 * about what it is. Section 12.3's invalidation rule is a generation check,
 * not a lifetime.
 */
class view {
public:
	constexpr view() noexcept : raw_{nullptr, 0, 0} {}
	explicit constexpr view(situ_view_t raw) noexcept : raw_(raw) {}

	constexpr situ_view_t raw() const noexcept { return raw_; }

	constexpr std::uint8_t  *base()  const noexcept { return raw_.base; }
	constexpr std::uint32_t  limit() const noexcept { return raw_.limit; }

protected:
	situ_view_t raw_;
};

}  /* namespace situ::rt */

#endif /* SITU_HPP */
