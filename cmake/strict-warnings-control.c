// #373: this file MUST fail to compile under MI_WARNINGS_AS_ERRORS. It reproduces the
// win-gnu `mi_scav_init defined but not used` shape -- an unused static function -- so the
// build can prove -Werror/-Wunused-function is actually reaching the compiler rather than
// silently passing through. If this file ever compiles clean, MI_WARNINGS_AS_ERRORS is not
// wired up and the CMakeLists.txt try_compile() check that consumes it must go red.

static void mi_strict_control_unused(void) { }

int main(void) { return 0; }
