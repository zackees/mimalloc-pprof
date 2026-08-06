if(NOT DEFINED TEST_EXE)
  message(FATAL_ERROR "TEST_EXE is required")
endif()
if(NOT DEFINED EXPECTED_TEXT)
  message(FATAL_ERROR "EXPECTED_TEXT is required")
endif()

if(DEFINED TEST_ARG)
  execute_process(
    COMMAND "${TEST_EXE}" "${TEST_ARG}"
    RESULT_VARIABLE result
    OUTPUT_VARIABLE output
    ERROR_VARIABLE error
    TIMEOUT 10)
else()
  execute_process(
    COMMAND "${TEST_EXE}"
    RESULT_VARIABLE result
    OUTPUT_VARIABLE output
    ERROR_VARIABLE error
    TIMEOUT 10)
endif()

set(combined "${output}${error}")
if("${result}" MATCHES "timeout")
  message(FATAL_ERROR "negative control timed out instead of failing fast\n${combined}")
endif()
if("${result}" STREQUAL "0")
  message(FATAL_ERROR "negative control unexpectedly exited successfully\n${combined}")
endif()
string(FIND "${combined}" "${EXPECTED_TEXT}" expected_at)
if(expected_at EQUAL -1)
  message(FATAL_ERROR
    "negative control failed for the wrong reason (result=${result}); expected '${EXPECTED_TEXT}'\n${combined}")
endif()

message(STATUS "negative control fired as expected (result=${result}): ${EXPECTED_TEXT}")
