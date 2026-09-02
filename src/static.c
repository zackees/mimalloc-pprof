/* ----------------------------------------------------------------------------
Copyright (c) 2018-2020, Microsoft Research, Daan Leijen
This is free software; you can redistribute it and/or modify it under the
terms of the MIT license. A copy of the license can be found in the file
"LICENSE" at the root of this distribution.
-----------------------------------------------------------------------------*/
#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE
#endif
#if defined(__sun)
// same remarks as os.c for the static's context.
#undef _XOPEN_SOURCE
#undef _POSIX_C_SOURCE
#endif

#include "mimalloc.h"
#include "mimalloc/internal.h"

// For a static override we create a single object file
// containing the whole library. If it is linked first
// it will override all the standard library allocation
// functions (on Unix's).
#include "alloc.c"          // includes alloc-override.c and free.c
#include "alloc-aligned.c"
#include "alloc-posix.c"
#include "arena.c"
#include "bitmap.c"
#if MI_DEBUG > 2
#include "diagnostic.c"
#endif
#include "heap.c"
#if !defined(_WIN32) && !defined(__wasi__)
#include "fork.c"           // #270: pthread_atfork handlers (POSIX only; CMake guards the standalone build the same way)
#endif
#include "init.c"
#include "libc.c"
#include "memory-events.c"
#include "dhat.c"
#include "dhat-stack.c"
#include "options.c"
#include "os.c"
#include "page.c"           // includes page-queue.c
#include "page-map.c"
#include "profile.c"
#if MI_PPROF
#include "profile-stack.c"
#include "profile-maps.c"
#endif
#include "random.c"
#include "stats.c"
#include "subproc.c"
#include "theap.c"
#include "threadlocal.c"
#include "prim/prim.c"
#include "prim/prim-tls.c"
#if MI_OSX_ZONE
#include "prim/osx/alloc-override-zone.c"
#endif
