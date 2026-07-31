#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "mimalloc" for configuration "Release"
set_property(TARGET mimalloc APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(mimalloc PROPERTIES
  IMPORTED_IMPLIB_RELEASE "${_IMPORT_PREFIX}/lib/libmimalloc.dll.a"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/bin/mimalloc.dll"
  )

list(APPEND _cmake_import_check_targets mimalloc )
list(APPEND _cmake_import_check_files_for_mimalloc "${_IMPORT_PREFIX}/lib/libmimalloc.dll.a" "${_IMPORT_PREFIX}/bin/mimalloc.dll" )

# Import target "mimalloc-static" for configuration "Release"
set_property(TARGET mimalloc-static APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(mimalloc-static PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "C"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/mimalloc-3.4/libmimalloc.a"
  )

list(APPEND _cmake_import_check_targets mimalloc-static )
list(APPEND _cmake_import_check_files_for_mimalloc-static "${_IMPORT_PREFIX}/lib/mimalloc-3.4/libmimalloc.a" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
