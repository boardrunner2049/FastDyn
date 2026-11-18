# Offline BOLT bridge

Build on x86-64 Linux with CMake, Ninja, the LLVM 20.1.2 development libraries,
and the pinned [LLVM 20.1.2 release source archive](https://github.com/llvm/llvm-project/releases/download/llvmorg-20.1.2/llvm-project-20.1.2.src.tar.xz).

```sh
tools/scopt/native/build.sh llvm-project-20.1.2.src.tar.xz /path/to/native-work
```

The script verifies the release archive and produces
`/path/to/native-work/build/bin/scopt-native`. The two checked-in adaptations
add an optional CMake target and expose a narrowly scoped wrapper for the
existing BOLT `Inliner::inlineCall` method. They apply to the unmodified
llvmorg-20.1.2 sources. The LLVM code remains under Apache 2.0 with LLVM
Exceptions. The bridge leaves the original ELF image and its layout intact.

The executable takes one JSON object on standard input. Fields are `version`
(integer 1), `image_path`, `image_sha256`, `address` (ELF virtual address),
`size`, and `code_sha256`. The bridge uses the verified bytes to construct
a BOLT `BinaryFunction` and CFG. It inserts a symbolic CALL into a synthetic
caller and uses BOLT's inliner to clone the CFG and remove its returns. That
symbolic edge also serves as the backend's representation of a certified
constant-pool indirect target. The Python caller verifies that binding.

A successful response contains version 1, status `ok`, hexadecimal `code`,
`origins` mapping each source instruction offset/size to an emitted offset/size,
and `relocations` listing each four-byte PC-relative continuation field. Place
the fragment at its eventual offset and write the displacement relative to the
end of that field. Internal branches are relaxed to rel32 and resolved within
the fragment. All offsets in the response are relative to their source or
fragment start. Refusals carry status `refused`, a stable `reason`, and
a diagnostic `detail`.

The bridge rejects instructions outside its bounded integer subset, including
stack uses, loads or stores, indirect branches, nested calls, and nonstandard
returns. It expects a single complete function range in one executable ELF
section. The caller verifies symbol boundaries, runtime identity, CET and
recovery assumptions, and the final native correspondence after placement.
The response is a candidate and does not confer any runtime legality.
