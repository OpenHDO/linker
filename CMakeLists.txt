cmake_minimum_required(VERSION 3.20)
project(openhdo_linker LANGUAGES NONE)

include(CTest)
find_package(Python3 COMPONENTS Interpreter REQUIRED)

add_test(
  NAME linker_contract
  COMMAND ${Python3_EXECUTABLE} -m unittest discover -s ${CMAKE_CURRENT_SOURCE_DIR}/tests -v
)
set_tests_properties(linker_contract PROPERTIES
  ENVIRONMENT "PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR}/src"
)
