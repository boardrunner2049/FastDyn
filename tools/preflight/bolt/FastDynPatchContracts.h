//===- bolt/Passes/FastDynPatchContracts.h ----------------------*- C++ -*-===//
//
// Patch-anchor guard for FastDyn's native BOLT pipeline.
//
//===----------------------------------------------------------------------===//

#ifndef BOLT_PASSES_FASTDYNPATCHCONTRACTS_H
#define BOLT_PASSES_FASTDYNPATCHCONTRACTS_H

#include "bolt/Passes/BinaryPasses.h"

namespace llvm {
namespace bolt {

/// Record the exact original store used by BOLT's frame optimizer to replace
/// an original stack load.  The firmware report exports these dependencies so
/// the acceptance layer can audit removed memory/fault opportunities.
/// ExactAdjacentSelfMove is true only when FOP's same-slot/same-width proof is
/// paired with adjacent original store/load identities and the retained
/// replacement is a redundant same-register move.
void recordFastDynFrameForwarding(uint64_t LoadAddress,
                                  uint64_t StoreAddress, bool Deleted,
                                  bool ExactAdjacentSelfMove);

/// Mark a synthesized instruction as implementing an exact original source
/// instruction. This is deliberately separate from the original-address
/// annotation: the synthesized instruction has its own emitted address while
/// Source supplies fault and event attribution. If Source has no stable
/// original identity, Synthesized remains explicitly unmapped.
void annotateFastDynSynthesizedFrom(BinaryContext &BC, const MCInst &Source,
                                    MCInst &Synthesized);

/// Mark an instruction that replaces one exact original instruction during a
/// checked transformation.  Keep this distinct from call-veneer synthesis:
/// the output origin map must describe why the instruction exists, not merely
/// which original instruction supplies its attribution.
void annotateFastDynTransformedFrom(BinaryContext &BC, const MCInst &Source,
                                    MCInst &Transformed);

/// Mark a non-call instruction synthesized while expanding an indirect call.
/// The instruction is attributed to the exact original call site without
/// pretending to be a copy of the original call opcode.
void annotateFastDynCallPromotionExpansion(BinaryContext &BC,
                                           MCInst &Synthesized,
                                           uint64_t OriginalCallIdentity);

/// Return the exact original indirect-call identity associated with an ICP
/// instruction. Calls may carry this as secondary metadata while retaining
/// their original/cloned origin classification.
std::optional<uint64_t>
getFastDynCallPromotionSource(BinaryContext &BC, const MCInst &Inst);

/// Mark an instruction synthesized while guarding a recovered jump-table
/// index.  Keep this distinct from call promotion: its extra observation is a
/// compare/branch before the original TBB/TBH fallback, not an extra call.
void annotateFastDynJumpTablePromotionExpansion(
    BinaryContext &BC, MCInst &Synthesized,
    uint64_t OriginalDispatchIdentity);

std::optional<uint64_t>
getFastDynJumpTablePromotionSource(BinaryContext &BC, const MCInst &Inst);

bool isFastDynClonedOriginal(BinaryContext &BC, const MCInst &Inst);

/// Return the exact original instruction identity attached before native
/// transformations, if one exists.
std::optional<uint64_t> getFastDynOriginalIdentity(BinaryContext &BC,
                                                   const MCInst &Inst);

/// Record the exact instruction-identity equivalence established before BOLT
/// folds one Cortex-M function into another.  Safe firmware ICF may coalesce
/// code addresses, but it must not discard the original identities used for
/// fault/event attribution.  Return false when the two bodies cannot be
/// represented as a total instruction-identity mapping; the caller must then
/// retain both functions.
bool recordFastDynIdenticalCodeFold(BinaryContext &BC,
                                    BinaryFunction &Child,
                                    BinaryFunction &Parent);

/// Mark an instruction copied into a different function by a native BOLT
/// transformation. The instruction retains the source identity for fault and
/// event attribution, while the clone marker prevents address translation from
/// treating that identity as an offset in the destination function.
void annotateFastDynClone(BinaryContext &BC, MCInst &Inst,
                          uint64_t OriginalIdentity);

/// Import the Cortex-M vector table as a firmware constraint before ordinary
/// BOLT transformations run.  Vector-reachable handlers are retained at their
/// original addresses until exception entry/return and modeled scheduling are
/// represented by target analyses rather than inferred from normal call/return
/// control flow.
class CortexMFirmwareConstraints : public BinaryFunctionPass {
public:
  CortexMFirmwareConstraints() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "cortex-m-firmware-constraints";
  }

  Error runOnFunctions(BinaryContext &BC) override;
};

class FastDynPatchContracts : public BinaryFunctionPass {
public:
  FastDynPatchContracts() : BinaryFunctionPass(false) {}

  const char *getName() const override { return "fastdyn-patch-contracts"; }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Attach stable original instruction addresses before mutating passes. BOLT
/// transformations that mutate or clone an MCInst carry the annotation with
/// the instruction; newly synthesized instructions remain explicitly unmapped.
class FastDynOriginTracker : public BinaryFunctionPass {
public:
  FastDynOriginTracker() : BinaryFunctionPass(false) {}

  const char *getName() const override { return "fastdyn-origin-tracker"; }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Recover movable Thumb-1 literal-pool ownership. A qualified pool is
/// attached to an existing non-fallthrough branch or return with all bytes,
/// labels, relocations and original identity intact. Shared value consumers
/// require an explicit address-change contract and typed clone capacity;
/// ambiguous/external users remain address-stable.
class ThumbLiteralPoolPlacement : public BinaryFunctionPass {
public:
  ThumbLiteralPoolPlacement() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "thumb-literal-pool-placement";
  }

  Error runOnFunctions(BinaryContext &BC) override;
};

/// Apply firmware-only publication invariants after native BOLT passes have
/// finished.  An attempted optimization remains visible in the report, but an
/// incidental transformation that violates the declared guest-observation
/// policy causes the exact input body to be installed instead.
class CortexMFirmwarePublicationPolicy : public BinaryFunctionPass {
public:
  CortexMFirmwarePublicationPolicy() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "cortex-m-firmware-publication-policy";
  }

  Error runOnFunctions(BinaryContext &BC) override;
};

/// Fold an exact x86-64 O0 multiply-by-40 idiom without changing memory,
/// control flow, calls, or the final architectural flags.
class FastDynArithmeticStrengthReduction : public BinaryFunctionPass {
public:
  FastDynArithmeticStrengthReduction() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-arithmetic-strength-reduction";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Track exact 64-bit register equality within a basic block and delete a move
/// whose source and destination are already equal. Register clobbers kill the
/// relevant facts. This commonly exposes cleanup left by FOP forwarding.
class FastDynCopyCleanup : public BinaryFunctionPass {
public:
  FastDynCopyCleanup() : BinaryFunctionPass(false) {}

  const char *getName() const override { return "fastdyn-copy-cleanup"; }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Fold an adjacent x86-64 load and self-test into one memory test only when
/// liveness proves the loaded register value is dead after the test.
class FastDynMemoryTestFolding : public BinaryFunctionPass {
public:
  FastDynMemoryTestFolding() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-memory-test-folding";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Remove an otherwise empty x86-64 frame only under BOLT's explicit ABI
/// liveness contract. Protected entry instructions and the return remain.
class FastDynTrivialFrameElimination : public BinaryFunctionPass {
public:
  FastDynTrivialFrameElimination() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-trivial-frame-elimination";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Replace a profile-observed direct call with the exact single load from a
/// previously qualified private-return-spill leaf. This is an ABI-only
/// call-boundary optimization; it does not apply under the firmware default.
class FastDynExactLeafCallInlining : public BinaryFunctionPass {
public:
  FastDynExactLeafCallInlining() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-exact-leaf-call-inlining";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Fuse an exact pointer-return accessor call into its caller after a qualified
/// leaf load. The pass retains the accessor load/add identities and requires
/// the same explicit ABI-only call-stack contract as leaf-call inlining.
class FastDynExactAccessorCallFusion : public BinaryFunctionPass {
public:
  FastDynExactAccessorCallFusion() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-exact-accessor-call-fusion";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Remove a profile-observed direct call to an exact empty leaf while keeping
/// the callee and every external entry intact. This requires the explicit ABI
/// contract because the balanced architectural and shadow-stack call effects
/// are not observable under that model.
class FastDynExactEmptyCallElimination : public BinaryFunctionPass {
public:
  FastDynExactEmptyCallElimination() : BinaryFunctionPass(false) {}

  const char *getName() const override {
    return "fastdyn-exact-empty-call-elimination";
  }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Export the candidate state after BOLT's ordinary optimization passes and
/// before instruction lowering. The firmware driver consumes this report;
/// this pass does not implement another optimizer or publish firmware.
class FastDynFirmwareReport : public BinaryFunctionPass {
public:
  FastDynFirmwareReport() : BinaryFunctionPass(false) {}

  const char *getName() const override { return "fastdyn-firmware-report"; }

  Error runOnFunctions(BinaryContext &BC) override;

  static bool isEnabled();
};

/// Export the linker-resolved function/basic-block/table addresses after BOLT
/// has assembled and linked the candidate.  The ordinary pass report is
/// intentionally pre-emission; this companion artifact closes the gap caused
/// by block alignment and branch relaxation.
Error finalizeFastDynFirmwareLayout(BinaryContext &BC);

/// Record final input-to-output addresses for relocations carried inside
/// instruction-owned firmware data. This runs independently of report output
/// because SHT_REL correctness is an emission requirement, not a diagnostic.
Error recordFastDynFirmwareRelocationTranslations(BinaryContext &BC);

} // namespace bolt
} // namespace llvm

#endif
