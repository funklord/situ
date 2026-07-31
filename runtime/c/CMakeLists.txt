# libsitu -- the C runtime, as a CMake target.
#
# Self-contained, the way the Makefile beside it is: `cd runtime/c && cmake -B
# build && cmake --build build` works on its own, with no cache variable from
# anywhere above. That is section 24's rule for every sub-project here, and it
# is what makes this usable by a project that vendors the runtime and wants
# nothing else from this tree.

cmake_minimum_required(VERSION 3.16)
project(libsitu C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

add_library(situ STATIC situ.c)
add_library(situ::situ ALIAS situ)

target_include_directories(situ PUBLIC
	$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}>
	$<INSTALL_INTERFACE:include>)

# The same flags the Makefile uses, and for the same reason: `-Wconversion` and
# `-Wsign-conversion` are where a byte-layout library's bugs live, and section
# 24 requires the runtime to build clean under them for a Cortex-A55 as well as
# for the host. PRIVATE, because a consumer's warning policy is the consumer's.
target_compile_options(situ PRIVATE
	-Wall -Wextra -Werror -Wconversion -Wsign-conversion)

install(TARGETS situ ARCHIVE DESTINATION lib)
install(FILES situ.h DESTINATION include)
