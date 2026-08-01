/* #128 B1: mi_subproc_delete must not leak the subproc's main heap.
   A non-main subproc's heap_main is dynamically allocated, but
   _mi_heap_force_destroy skips mi_heap_free for it because _mi_is_heap_main()
   resolves via heap->subproc and is therefore TRUE for it. */
#include <mimalloc.h>
#include <mimalloc-stats.h>
#include <stdio.h>
#include <stddef.h>
#define ROUNDS 3000
static size_t committed_now(void) {
  mi_stats_t_decl(s);
  if (!mi_subproc_stats_get(mi_subproc_main(), &s)) return 0;
  return (size_t)s.committed.current;
}
int main(void) {
  for (int i = 0; i < 50; i++) {
    mi_subproc_id_t sp = mi_subproc_new();
    if (sp._mi_subproc_id != NULL) mi_subproc_destroy(sp);
  }
  const size_t base = committed_now();
  for (int i = 0; i < ROUNDS; i++) {
    mi_subproc_id_t sp = mi_subproc_new();
    if (sp._mi_subproc_id == NULL) { printf("subproc_new failed at round %d\n", i); return 2; }
    mi_subproc_destroy(sp);
  }
  const size_t after = committed_now();
  const ptrdiff_t delta = (ptrdiff_t)after - (ptrdiff_t)base;
  printf("base=%zu after=%zu delta=%td over %d rounds\n", base, after, delta, ROUNDS);
  if (delta > (1 << 20)) { printf("FAIL: leaked %td bytes across %d subprocs\n", delta, ROUNDS); return 1; }
  printf("ok: bounded\n");
  return 0;
}
