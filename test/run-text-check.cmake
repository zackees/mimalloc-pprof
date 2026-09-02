# #268 (Bun parity P3): a positive/negative text-presence check for a test that must exit
# zero, mirroring run-negative.cmake's structure but for the opposite exit-code contract.
# `ENVIRONMENT` ctest properties on the driving `cmake -P` process are inherited by
# execute_process, so env vars (e.g. MIMALLOC_VERBOSE=1) are set via the ctest test's own
# ENVIRONMENT property, not via variables here -- keeps this script's surface small and
# reuses ci/bundle_tests.py's existing ENVIRONMENT lowering.
if(NOT DEFINED TEST_EXE)
  message(FATAL_ERROR "TEST_EXE is required")
endif()
if(NOT DEFINED EXPECTED_TEXT)
  message(FATAL_ERROR "EXPECTED_TEXT is required")
endif()
if(NOT DEFINED MODE)
  message(FATAL_ERROR "MODE is required (REQUIRE or FORBID)")
endif()
if(NOT MODE STREQUAL "REQUIRE" AND NOT MODE STREQUAL "FORBID")
  message(FATAL_ERROR "MODE must be REQUIRE or FORBID, got '${MODE}'")
endif()

execute_process(
  COMMAND "${TEST_EXE}"
  RESULT_VARIABLE result
  OUTPUT_VARIABLE output
  ERROR_VARIABLE error
  TIMEOUT 10)

set(combined "${output}${error}")
if("${result}" MATCHES "timeout")
  message(FATAL_ERROR "text check timed out instead of exiting\n${combined}")
endif()
if(NOT "${result}" STREQUAL "0")
  message(FATAL_ERROR "expected a zero exit, got ${result}\n${combined}")
endif()

string(FIND "${combined}" "${EXPECTED_TEXT}" found_at)
if(MODE STREQUAL "REQUIRE" AND found_at EQUAL -1)
  message(FATAL_ERROR "expected '${EXPECTED_TEXT}' in output, not found\n${combined}")
endif()
if(MODE STREQUAL "FORBID" AND NOT found_at EQUAL -1)
  message(FATAL_ERROR "forbidden text '${EXPECTED_TEXT}' found in output\n${combined}")
endif()

message(STATUS "text check passed (MODE=${MODE}): ${EXPECTED_TEXT}")
