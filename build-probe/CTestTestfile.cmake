# CMake generated Testfile for 
# Source directory: C:/Users/niteris/dev/mimalloc-pprof
# Build directory: C:/Users/niteris/dev/mimalloc-pprof/build-probe
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test(test-api "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-api.exe")
set_tests_properties(test-api PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;868;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-api-fill "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-api-fill.exe")
set_tests_properties(test-api-fill PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;868;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-stress-heaps "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-stress-heaps.exe")
set_tests_properties(test-stress-heaps PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;868;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-stress-subprocs "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-stress-subprocs.exe")
set_tests_properties(test-stress-subprocs PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;868;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-stress "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-stress.exe")
set_tests_properties(test-stress PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;868;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-profile "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-profile.exe")
set_tests_properties(test-profile PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;877;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-profile-accum "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-profile.exe")
set_tests_properties(test-profile-accum PROPERTIES  ENVIRONMENT "MIMALLOC_PROF_ACCUM=1" _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;878;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-profile-auto "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-profile.exe")
set_tests_properties(test-profile-auto PROPERTIES  ENVIRONMENT "MIMALLOC_PROF=1" _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;880;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-profile-race "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-profile-race.exe")
set_tests_properties(test-profile-race PROPERTIES  TIMEOUT "300" _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;894;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-memory-events "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-memory-events.exe")
set_tests_properties(test-memory-events PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;904;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-memory-events-env-enabled "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-memory-events.exe" "--env-enabled-check")
set_tests_properties(test-memory-events-env-enabled PROPERTIES  ENVIRONMENT "MIMALLOC_MEMORY_EVENTS=1" _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;905;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
add_test(test-stress-dynamic "C:/tools/python13/Lib/site-packages/cmake/data/bin/cmake.exe" "-E" "env" "MIMALLOC_VERBOSE=1" "C:/Users/niteris/dev/mimalloc-pprof/build-probe/mimalloc-test-stress-dynamic.exe")
set_tests_properties(test-stress-dynamic PROPERTIES  _BACKTRACE_TRIPLES "C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;917;add_test;C:/Users/niteris/dev/mimalloc-pprof/CMakeLists.txt;0;")
