# Load this directory at the end of the pinned BOLT CMake project.
set(LLVM_LINK_COMPONENTS X86 MC MCDisassembler Object Support TransformUtils)
add_bolt_tool(scopt-native scopt_native.cpp DISABLE_LLVM_LINK_LLVM_DYLIB)
target_include_directories(scopt-native PRIVATE "${BOLT_SOURCE_DIR}/include")
target_link_libraries(scopt-native PRIVATE LLVMBOLTPasses LLVMBOLTTargetX86 LLVMBOLTCore LLVMBOLTUtils)
