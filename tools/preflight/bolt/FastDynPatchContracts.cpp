//===- bolt/Passes/FastDynPatchContracts.cpp ------------------------------===//
//
// Patch-anchor guard for FastDyn's native BOLT pipeline.
//
//===----------------------------------------------------------------------===//

#include "bolt/Passes/FastDynPatchContracts.h"
#include "bolt/Core/BinaryData.h"
#include "bolt/Core/BinaryFunction.h"
#include "bolt/Core/BinarySection.h"
#include "bolt/Passes/DataflowInfoManager.h"
#include "bolt/Passes/LivenessAnalysis.h"
#include "bolt/Passes/RegAnalysis.h"
#include "bolt/Utils/CommandLineOpts.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/MC/MCDwarf.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/FormatVariadic.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Endian.h"
#include "llvm/Support/raw_ostream.h"
#include <array>
#include <cinttypes>
#include <map>
#include <set>
#include <vector>

using namespace llvm;
using namespace llvm::bolt;

namespace opts {
extern cl::opt<bool> PreserveFirmwareInstructionCount;
}

namespace {

constexpr StringLiteral FastDynOriginalAddressAnnotation(
    "FastDynOriginalAddress");
constexpr StringLiteral FastDynClonedOriginalAnnotation(
    "FastDynClonedOriginal");
constexpr StringLiteral FastDynSynthesizedFromAnnotation(
    "FastDynSynthesizedFrom");
constexpr StringLiteral FastDynTransformedFromAnnotation(
    "FastDynTransformedFrom");
constexpr StringLiteral FastDynCallPromotionFromAnnotation(
    "FastDynCallPromotionFrom");
constexpr StringLiteral FastDynJumpTablePromotionFromAnnotation(
    "FastDynJumpTablePromotionFrom");
constexpr StringLiteral FastDynSynthesizedControlEdgeFromAnnotation(
    "FastDynSynthesizedControlEdgeFrom");

cl::OptionCategory FastDynCategory("FastDyn preflight options");

cl::opt<std::string> PatchContractsFilename(
    "fastdyn-patch-contracts",
    cl::desc("validate and protect FastDyn patch bindings from JSON"),
    cl::value_desc("patch-bindings.json"), cl::cat(FastDynCategory));

cl::opt<std::string> FirmwareReportFilename(
    "fastdyn-firmware-report",
    cl::desc("write a post-optimization firmware candidate report"),
    cl::value_desc("report.json"), cl::cat(FastDynCategory));

cl::opt<bool> ArithmeticStrengthReduction(
    "fastdyn-arithmetic-strength-reduction",
    cl::desc("fold exact, effect-free x86-64 O0 arithmetic idioms"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> CopyCleanup(
    "fastdyn-copy-cleanup",
    cl::desc("delete checked x86-64 copies exposed by forwarding"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> CopyCleanupHotOnly(
    "fastdyn-copy-cleanup-hot-only",
    cl::desc("emit checked copy cleanup only in profile-observed blocks"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> CopyCleanupAdjacentOnly(
    "fastdyn-copy-cleanup-adjacent-only",
    cl::desc("limit copy cleanup equality facts to the immediately prior move"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> MemoryTestFolding(
    "fastdyn-memory-test-folding",
    cl::desc("fold a dead x86-64 load result into its adjacent self-test"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> TrivialFrameElimination(
    "fastdyn-trivial-frame-elimination",
    cl::desc("remove an exact empty x86-64 frame under --assume-abi"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> ExactLeafCallInlining(
    "fastdyn-exact-leaf-call-inlining",
    cl::desc("inline an exact qualified x86-64 leaf load under --assume-abi"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> ExactAccessorCallFusion(
    "fastdyn-exact-accessor-call-fusion",
    cl::desc("fuse an exact qualified x86-64 pointer accessor call under --assume-abi"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> ExactEmptyCallElimination(
    "fastdyn-exact-empty-call-elimination",
    cl::desc("remove observed calls to exact empty x86-64 leaves under --assume-abi"),
    cl::init(false), cl::cat(FastDynCategory));

cl::opt<bool> AllowLiteralAddressChanges(
    "firmbolt-allow-literal-address-changes",
    cl::desc("allow checked Thumb value-pool cloning to change literal "
             "access addresses"),
    cl::init(false), cl::cat(FastDynCategory));

struct OriginalInstructionRecord {
  uint64_t Address;
  uint64_t Size;
  std::string Text;
  bool MayLoad;
  bool MayStore;
  bool IsCall;
  bool IsBranch;
  bool IsUnconditionalBranch;
  bool IsReturn;
  std::optional<uint64_t> BranchTargetAddress;
};

struct FrameForwardingRecord {
  uint64_t LoadAddress;
  uint64_t StoreAddress;
  bool Deleted;
};

struct ArithmeticFusionRecord {
  std::string Kind;
  uint64_t FunctionAddress;
  uint64_t ReplacementOriginalAddress;
  std::vector<uint64_t> ConsumedOriginalAddresses;
  uint64_t KnownExecutionCount;
  std::vector<uint64_t> ReplacementOriginalAddresses{};
};

struct CopyCleanupRecord {
  std::string Kind;
  uint64_t FunctionAddress;
  uint64_t RetainedOriginalAddress;
  uint64_t RemovedOriginalAddress;
  uint64_t KnownExecutionCount;
};

/// The tracker and reporter execute in the same BOLT invocation.  Retain the
/// decoded pre-transformation effect inventory here so the firmware wrapper
/// can distinguish a mapped rewrite from a deleted/reordered memory effect.
/// This is reporting state only; it does not authorize a transformation.
std::map<uint64_t, OriginalInstructionRecord> OriginalInstructions;
std::map<uint64_t, FrameForwardingRecord> FrameForwardings;
std::set<std::pair<uint64_t, uint64_t>> OriginalControlEdges;
std::vector<ArithmeticFusionRecord> ArithmeticFusions;
std::vector<CopyCleanupRecord> CopyCleanups;

Expected<json::Value> readContracts() {
  ErrorOr<std::unique_ptr<MemoryBuffer>> Buffer =
      MemoryBuffer::getFile(PatchContractsFilename);
  if (!Buffer)
    return createStringError(Buffer.getError(), "cannot read %s",
                             PatchContractsFilename.c_str());
  return json::parse(Buffer.get()->getBuffer());
}

bool hasInstructionAt(BinaryContext &BC, BinaryFunction &BF, uint64_t PC) {
  for (BinaryBasicBlock &BB : BF) {
    // BOLT does not retain an instruction-offset annotation on every first
    // instruction (notably the function entry).  The block's input range is
    // still an exact decoder-derived boundary, so use it for the first real
    // instruction and annotations for the remaining instructions.
    bool FirstRealInstruction = true;
    for (MCInst &Inst : BB) {
      if (BC.MIB->isPseudo(Inst))
        continue;
      if (FirstRealInstruction && BF.getAddress() + BB.getOffset() == PC)
        return true;
      FirstRealInstruction = false;
      std::optional<uint32_t> Offset = BC.MIB->getOffset(Inst);
      if (Offset && BF.getAddress() + *Offset == PC)
        return true;
    }
  }
  return false;
}

uint64_t reportInstructionSize(BinaryContext &BC, const MCInst &Inst) {
  if (BC.MIB->isPseudo(Inst))
    return 0;
  if (BC.isARM())
    return BC.MII->get(Inst.getOpcode()).getSize();
  return BC.computeInstructionSize(Inst);
}

uint64_t reportFunctionSize(BinaryContext &BC, const BinaryFunction &BF) {
  if (!BF.hasCFG())
    return BF.getSize();
  if (BF.requiresAddressStableLayout())
    return BF.getSize();
  if (!BC.isARM())
    return BF.estimateSize();
  uint64_t Size = 0;
  for (const BinaryBasicBlock &BB : BF)
    for (const MCInst &Inst : BB) {
      Size += reportInstructionSize(BC, Inst);
      if (const InlineData *Data = BC.MIB->getInlineData(Inst))
        Size += Data->Size;
      if (const InlineDataBlocks *Blocks =
              BC.MIB->getInlineDataBlocks(Inst))
        for (const InlineDataBlock &Block : *Blocks) {
          Size += offsetToAlignment(BF.getAddress() + Size,
                                    Align(Block.Alignment));
          Size += Block.Bytes.size();
        }
      if (const JumpTable *JT = BF.getJumpTable(Inst);
          JT && JT->isThumbInline())
        Size += JT->getSize() + JT->InlineTrailingBytes.size();
    }
  return Size;
}

using FirmwareModification = BinaryFunction::FirmwareModification;
using FirmwarePublicationRejection =
    BinaryFunction::FirmwarePublicationRejection;

static constexpr std::array<std::pair<FirmwareModification, StringLiteral>, 20>
    FirmwareModificationNames = {{
        {FirmwareModification::CFGNormalization, "cfg_normalization"},
        {FirmwareModification::UnreachableCodeElimination,
         "unreachable_code_elimination"},
        {FirmwareModification::BlockReordering, "block_reordering"},
        {FirmwareModification::NopRemoval, "nop_removal"},
        {FirmwareModification::FrameLoadForwarding,
         "frame_load_forwarding"},
        {FirmwareModification::FrameStoreRemoval, "frame_store_removal"},
        {FirmwareModification::AllocationCombining,
         "allocation_combining"},
        {FirmwareModification::BranchRelaxation, "branch_relaxation"},
        {FirmwareModification::VeneerSlotAllocation,
         "veneer_slot_allocation"},
        {FirmwareModification::LiteralPoolPlacement,
         "literal_pool_placement"},
        {FirmwareModification::InstructionShortening,
         "instruction_shortening"},
        {FirmwareModification::Inlining, "inlining"},
        {FirmwareModification::TailDuplication, "tail_duplication"},
        {FirmwareModification::LoopInversion, "loop_inversion"},
        {FirmwareModification::ControlFlowPeepholes,
         "control_flow_peepholes"},
        {FirmwareModification::IndirectCallPromotion,
         "indirect_call_promotion"},
        {FirmwareModification::JumpTablePromotion,
         "jump_table_promotion"},
        {FirmwareModification::MethodLoadElimination,
         "method_load_elimination"},
        {FirmwareModification::ReadOnlyLoadElision,
         "read_only_load_elision"},
        {FirmwareModification::FunctionSplitting, "function_splitting"},
    }};

json::Array firmwareModificationReasons(const BinaryFunction &BF) {
  json::Array Reasons;
  for (const auto &[Reason, Name] : FirmwareModificationNames)
    if (BF.hasFirmwareModification(Reason))
      Reasons.push_back(Name);
  return Reasons;
}

static constexpr std::array<
    std::pair<FirmwarePublicationRejection, StringLiteral>, 7>
    FirmwarePublicationRejectionNames = {{
        {FirmwarePublicationRejection::InstructionCountChanged,
         "instruction_count_changed"},
        {FirmwarePublicationRejection::ExceptionContractUnsupported,
         "exception_contract_unsupported"},
        {FirmwarePublicationRejection::LiteralAddressChanged,
         "literal_address_changed"},
        {FirmwarePublicationRejection::ExceedsQualifiedRange,
         "exceeds_qualified_range"},
        {FirmwarePublicationRejection::MissingEmittedBody,
         "missing_emitted_body"},
        {FirmwarePublicationRejection::UnmappedSplitFragment,
         "unmapped_split_fragment"},
        {FirmwarePublicationRejection::InlineDataPlacementInvalid,
         "inline_data_placement_invalid"},
    }};

json::Array firmwarePublicationRejections(const BinaryFunction &BF) {
  json::Array Reasons;
  for (const auto &[Reason, Name] : FirmwarePublicationRejectionNames)
    if (BF.hasFirmwarePublicationRejection(Reason))
      Reasons.push_back(Name);
  return Reasons;
}

} // namespace

Error CortexMFirmwareConstraints::runOnFunctions(BinaryContext &BC) {
  if (!BC.isARM())
    return Error::success();

  BC.PreserveFirmwareInstructionCount =
      opts::PreserveFirmwareInstructionCount;

  const BinarySection *VectorSection = nullptr;
  StringRef Contents;
  uint64_t VectorAddress = 0;
  StringRef VectorSource;
  ErrorOr<BinarySection &> VectorSectionOrErr =
      BC.getUniqueSectionByName(".isr_vector");
  if (VectorSectionOrErr) {
    VectorSection = &*VectorSectionOrErr;
    Contents = VectorSection->getContents();
    VectorAddress = VectorSection->getAddress();
    VectorSource = ".isr_vector section";
  } else {
    // Bare-metal linker scripts commonly place the vector table at the start
    // of .text and retain only a conventional symbol.  Treat this as an
    // explicit representation, not a heuristic scan of executable bytes.
    static constexpr std::array<StringLiteral, 3> VectorSymbolNames = {
        "vector_table", "g_pfnVectors", "__Vectors"};
    const BinaryData *VectorData = nullptr;
    for (StringRef Name : VectorSymbolNames) {
      VectorData = BC.getBinaryDataByName(Name);
      if (VectorData) {
        VectorSource = Name;
        break;
      }
    }
    if (!VectorData) {
      return createStringError(
          inconvertibleErrorCode(),
          "Cortex-M production optimization requires an explicit .isr_vector "
          "section or one of vector_table, g_pfnVectors, or __Vectors; use "
          "--firmbolt-arm-full-pipeline=false only for identity diagnostics");
    }
    VectorSection = &VectorData->getSection();
    VectorAddress = VectorData->getAddress();
    const uint64_t Offset = VectorData->getOffset();
    const uint64_t Size = VectorData->getSize();
    const StringRef SectionContents = VectorSection->getContents();
    if (Size < 8 || Offset > SectionContents.size() ||
        Size > SectionContents.size() - Offset)
      return createStringError(
          inconvertibleErrorCode(),
          "Cortex-M vector symbol %s at 0x%" PRIx64
          " has invalid section range [0x%" PRIx64 ", +0x%" PRIx64 ")",
          VectorSource.str().c_str(), VectorAddress, Offset, Size);
    Contents = SectionContents.slice(Offset, Offset + Size);
  }

  if (Contents.size() < 8 || Contents.size() % 4 != 0)
    return createStringError(
        inconvertibleErrorCode(),
        "Cortex-M vector section at 0x%" PRIx64
        " has invalid size 0x%zx (expected at least two 32-bit entries)",
        VectorAddress, Contents.size());

  auto ReadWord = [&](size_t Offset) -> uint32_t {
    const uint8_t *Data =
        reinterpret_cast<const uint8_t *>(Contents.data() + Offset);
    return BC.AsmInfo->isLittleEndian()
               ? support::endian::read32le(Data)
               : support::endian::read32be(Data);
  };

  const uint32_t InitialSP = ReadWord(0);
  std::set<uint64_t> ProtectedAddresses;
  uint64_t ReservedEntries = 0;
  uint64_t InvalidModeEntries = 0;
  uint64_t UnresolvedEntries = 0;
  uint64_t SecondaryEntryHandlers = 0;
  uint64_t PrimaryEntryHandlers = 0;
  uint64_t SecurityStateFunctions = 0;
  uint64_t FirmwareVeneerSlots = 0;
  SmallVector<BinaryFunction *, 4> VeneerSlotFunctions;

  for (size_t Offset = 4; Offset < Contents.size(); Offset += 4) {
    const uint32_t RawHandler = ReadWord(Offset);
    if (RawHandler == 0 || RawHandler == UINT32_MAX) {
      ++ReservedEntries;
      continue;
    }

    // All Cortex-M exceptions enter Thumb state.  An even non-zero vector is
    // malformed for this execution profile; preserve it in the image but do
    // not manufacture a function binding for it.
    if ((RawHandler & 1U) == 0) {
      ++InvalidModeEntries;
      continue;
    }

    const uint64_t HandlerAddress = RawHandler & ~uint64_t(1);
    BinaryFunction *Handler = BC.getBinaryFunctionAtAddress(HandlerAddress);
    bool IsSecondaryEntry = false;
    if (!Handler) {
      // A vector may name a secondary/assembly entry inside a symbolized
      // function.  Protect the entire containing function; nearest-address
      // rebinding is forbidden, but exact containment is sufficient to block
      // transformations until secondary-entry semantics are modeled.
      Handler = BC.getBinaryFunctionContainingAddress(
          HandlerAddress, /*CheckPastEnd=*/false, /*UseMaxSize=*/true);
      if (Handler)
        ++SecondaryEntryHandlers;
      IsSecondaryEntry = Handler != nullptr;
    }
    if (!Handler) {
      ++UnresolvedEntries;
      continue;
    }

    if (ProtectedAddresses.insert(HandlerAddress).second) {
      Handler->setCortexMVectorEntry();
      if (IsSecondaryEntry)
        Handler->setSimple(false);
      else
        ++PrimaryEntryHandlers;
    }
  }

  // External veneer capacity is accepted only through an explicit typed input
  // range.  Do not infer slack from symbol gaps, NOP runs or cold profile data.
  // The section/name pair is intentionally redundant: it lets a linker script
  // reserve placement while giving each independently allocated slot a stable
  // identity and exact size.
  static constexpr StringLiteral VeneerSectionName = ".firmbolt.veneer";
  static constexpr StringLiteral VeneerSlotPrefix =
      "__firmbolt_thumb_veneer_slot";
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    const bool HasSlotName = BF.getOneName().starts_with(VeneerSlotPrefix);
    const BinarySection *Origin = BF.getOriginSection();
    const bool IsVeneerSection =
        Origin && Origin->getName() == VeneerSectionName;
    if (!HasSlotName && !IsVeneerSection)
      continue;
    if (!HasSlotName || !IsVeneerSection)
      return createStringError(
          inconvertibleErrorCode(),
          "Cortex-M veneer reservation %s must use both section %s and prefix "
          "%s",
          BF.getPrintName().c_str(), VeneerSectionName.str().c_str(),
          VeneerSlotPrefix.str().c_str());
    if (!Origin->isAllocatable() || !Origin->isText() || Origin->isWritable())
      return createStringError(
          inconvertibleErrorCode(),
          "Cortex-M veneer slot %s is not in read-only executable allocated "
          "memory",
          BF.getPrintName().c_str());
    if ((BF.getAddress() & 3) || BF.getSize() < 8 || BF.isMultiEntry() ||
        BF.isSplit() || BF.hasConstantIsland() || BF.isCortexMVectorEntry())
      return createStringError(
          inconvertibleErrorCode(),
          "Cortex-M veneer slot %s must be word-aligned, at least 8 bytes, "
          "single-entry, unsplit, island-free and absent from the vector "
          "table",
          BF.getPrintName().c_str());
    BF.setFirmwareVeneerSlot();
    VeneerSlotFunctions.push_back(&BF);
    ++FirmwareVeneerSlots;
  }

  // The typed section is a capacity declaration, not permission to overwrite
  // a live entry.  Reject every statically visible original code or relocation
  // reference to a slot.  Unknown indirect targets remain outside this bounded
  // contract and therefore cannot be represented by the slot ABI.
  if (!VeneerSlotFunctions.empty()) {
    auto IsVeneerSlotSymbol = [&](const MCSymbol *Symbol) {
      if (!Symbol)
        return false;
      const BinaryFunction *Target = BC.getFunctionForSymbol(Symbol);
      return Target && Target->isFirmwareVeneerSlot();
    };
    for (const auto &BFI : BC.getBinaryFunctions()) {
      const BinaryFunction &BF = BFI.second;
      if (!BF.hasCFG())
        continue;
      for (const BinaryBasicBlock &BB : BF)
        for (const MCInst &Inst : BB)
          if (IsVeneerSlotSymbol(BC.MIB->getTargetSymbol(Inst)))
            return createStringError(
                inconvertibleErrorCode(),
                "Cortex-M veneer slot has an original direct code reference "
                "from %s",
                BF.getPrintName().c_str());
    }
    for (const BinarySection &Section : BC.allocatableSections())
      for (const Relocation &Reloc : Section.relocations())
        if (IsVeneerSlotSymbol(Reloc.Symbol))
          return createStringError(
              inconvertibleErrorCode(),
              "Cortex-M veneer slot has an original relocation reference in "
              "section %s at offset 0x%" PRIx64,
              Section.getName().str().c_str(), Reloc.Offset);
  }

  // Armv8-M security transitions, secure gateways and security attribution
  // tests are not ordinary calls/branches or pure register operations.  The
  // backend can decode and exactly retain them, but optimizing their containing
  // function requires an explicit Secure/Non-secure state contract.  Fence the
  // complete function before any normalizing or data-flow pass runs.
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple())
      continue;
    bool HasSecurityStateInstruction = false;
    for (const BinaryBasicBlock &BB : BF) {
      for (const MCInst &Inst : BB) {
        const StringRef Name = BC.MII->getName(Inst.getOpcode());
        if (Name == "t2SG" || Name == "t2TT" || Name == "t2TTT" ||
            Name == "t2TTA" || Name == "t2TTAT" || Name == "tBXNS" ||
            Name == "tBXNS_RET" || Name == "tBLXNSr" ||
            Name == "tBLXNS_CALL" || Name == "t2PACG" ||
            Name == "t2AUTG" || Name == "t2BXAUT" ||
            Name == "t2BXAUT_RET" || Name == "t2PAC" ||
            Name == "t2PACBTI" || Name == "t2BTI" || Name == "t2AUT") {
          HasSecurityStateInstruction = true;
          break;
        }
      }
      if (HasSecurityStateInstruction)
        break;
    }
    if (!HasSecurityStateInstruction)
      continue;
    BF.setSimple(false);
    ++SecurityStateFunctions;
  }

  BC.outs() << "BOLT-INFO: Cortex-M firmware constraints: vector=0x"
            << Twine::utohexstr(VectorAddress) << ", source=" << VectorSource
            << ", entries="
            << Contents.size() / 4 << ", initial-sp=0x"
            << Twine::utohexstr(InitialSP) << ", protected-handlers="
            << ProtectedAddresses.size() << ", reserved=" << ReservedEntries
            << ", invalid-thumb=" << InvalidModeEntries
            << ", secondary-entry-handlers=" << SecondaryEntryHandlers
            << ", primary-entry-handlers=" << PrimaryEntryHandlers
            << ", veneer-slots=" << FirmwareVeneerSlots
            << ", security-state-functions=" << SecurityStateFunctions
            << ", unresolved=" << UnresolvedEntries << "\n";
  return Error::success();
}

void llvm::bolt::recordFastDynFrameForwarding(uint64_t LoadAddress,
                                              uint64_t StoreAddress,
                                              bool Deleted) {
  FrameForwardings.insert_or_assign(
      LoadAddress, FrameForwardingRecord{LoadAddress, StoreAddress, Deleted});
}

void llvm::bolt::annotateFastDynSynthesizedFrom(BinaryContext &BC,
                                                const MCInst &Source,
                                                MCInst &Synthesized) {
  const std::optional<unsigned> OriginIndex =
      BC.MIB->getAnnotationIndex(FastDynOriginalAddressAnnotation);
  if (!OriginIndex || !BC.MIB->hasAnnotation(Source, *OriginIndex))
    return;
  const unsigned SynthesizedIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynSynthesizedFromAnnotation);
  if (BC.MIB->hasAnnotation(Synthesized, SynthesizedIndex))
    return;
  BC.MIB->addAnnotation(
      Synthesized, SynthesizedIndex,
      BC.MIB->getAnnotationAs<uint64_t>(Source, *OriginIndex));
}

void llvm::bolt::annotateFastDynTransformedFrom(BinaryContext &BC,
                                                const MCInst &Source,
                                                MCInst &Transformed) {
  const std::optional<unsigned> OriginIndex =
      BC.MIB->getAnnotationIndex(FastDynOriginalAddressAnnotation);
  if (!OriginIndex || !BC.MIB->hasAnnotation(Source, *OriginIndex))
    return;
  const unsigned TransformedIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynTransformedFromAnnotation);
  if (BC.MIB->hasAnnotation(Transformed, TransformedIndex))
    return;
  BC.MIB->addAnnotation(
      Transformed, TransformedIndex,
      BC.MIB->getAnnotationAs<uint64_t>(Source, *OriginIndex));
}

void llvm::bolt::annotateFastDynCallPromotionExpansion(
    BinaryContext &BC, MCInst &Synthesized, uint64_t OriginalCallIdentity) {
  const unsigned AnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynCallPromotionFromAnnotation);
  if (!BC.MIB->hasAnnotation(Synthesized, AnnotationIndex))
    BC.MIB->addAnnotation(Synthesized, AnnotationIndex,
                          OriginalCallIdentity);
}

std::optional<uint64_t>
llvm::bolt::getFastDynCallPromotionSource(BinaryContext &BC,
                                         const MCInst &Inst) {
  const std::optional<unsigned> AnnotationIndex =
      BC.MIB->getAnnotationIndex(FastDynCallPromotionFromAnnotation);
  if (!AnnotationIndex || !BC.MIB->hasAnnotation(Inst, *AnnotationIndex))
    return std::nullopt;
  return BC.MIB->getAnnotationAs<uint64_t>(Inst, *AnnotationIndex);
}

void llvm::bolt::annotateFastDynJumpTablePromotionExpansion(
    BinaryContext &BC, MCInst &Synthesized,
    uint64_t OriginalDispatchIdentity) {
  const unsigned AnnotationIndex = BC.MIB->getOrCreateAnnotationIndex(
      FastDynJumpTablePromotionFromAnnotation);
  if (!BC.MIB->hasAnnotation(Synthesized, AnnotationIndex))
    BC.MIB->addAnnotation(Synthesized, AnnotationIndex,
                          OriginalDispatchIdentity);
}

std::optional<uint64_t>
llvm::bolt::getFastDynJumpTablePromotionSource(BinaryContext &BC,
                                               const MCInst &Inst) {
  const std::optional<unsigned> AnnotationIndex =
      BC.MIB->getAnnotationIndex(FastDynJumpTablePromotionFromAnnotation);
  if (!AnnotationIndex || !BC.MIB->hasAnnotation(Inst, *AnnotationIndex))
    return std::nullopt;
  return BC.MIB->getAnnotationAs<uint64_t>(Inst, *AnnotationIndex);
}

bool llvm::bolt::isFastDynClonedOriginal(BinaryContext &BC,
                                        const MCInst &Inst) {
  const std::optional<unsigned> AnnotationIndex =
      BC.MIB->getAnnotationIndex(FastDynClonedOriginalAnnotation);
  return AnnotationIndex && BC.MIB->hasAnnotation(Inst, *AnnotationIndex);
}

std::optional<uint64_t>
llvm::bolt::getFastDynOriginalIdentity(BinaryContext &BC,
                                      const MCInst &Inst) {
  const std::optional<unsigned> OriginIndex =
      BC.MIB->getAnnotationIndex(FastDynOriginalAddressAnnotation);
  if (!OriginIndex || !BC.MIB->hasAnnotation(Inst, *OriginIndex))
    return std::nullopt;
  return BC.MIB->getAnnotationAs<uint64_t>(Inst, *OriginIndex);
}

void llvm::bolt::annotateFastDynClone(BinaryContext &BC, MCInst &Inst,
                                     uint64_t OriginalIdentity) {
  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  const unsigned CloneIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynClonedOriginalAnnotation);
  if (!BC.MIB->hasAnnotation(Inst, OriginIndex))
    BC.MIB->addAnnotation(Inst, OriginIndex, OriginalIdentity);
  if (!BC.MIB->hasAnnotation(Inst, CloneIndex))
    BC.MIB->addAnnotation(Inst, CloneIndex, uint8_t{1});
}

bool FastDynPatchContracts::isEnabled() {
  return !PatchContractsFilename.empty();
}

bool FastDynOriginTracker::isEnabled() {
  return !FirmwareReportFilename.empty();
}

Error FastDynOriginTracker::runOnFunctions(BinaryContext &BC) {
  const unsigned AnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  uint64_t Annotated = 0;
  OriginalInstructions.clear();
  FrameForwardings.clear();
  OriginalControlEdges.clear();
  ArithmeticFusions.clear();
  CopyCleanups.clear();
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG())
      continue;
    for (BinaryBasicBlock &BB : BF) {
      const uint64_t SourceAddress = BF.getAddress() + BB.getOffset();
      for (const BinaryBasicBlock *Successor : BB.successors())
        OriginalControlEdges.emplace(
            SourceAddress, BF.getAddress() + Successor->getOffset());
      uint64_t CurrentOffset = BB.getOffset();
      for (MCInst &Inst : BB) {
        if (BC.MIB->isPseudo(Inst))
          continue;
        std::optional<uint32_t> Offset = BC.MIB->getOffset(Inst);
        if (Offset)
          CurrentOffset = *Offset;
        if (!BC.MIB->hasAnnotation(Inst, AnnotationIndex)) {
          BC.MIB->addAnnotation(
              Inst, AnnotationIndex, BF.getAddress() + CurrentOffset);
          ++Annotated;
        }
        const uint64_t Address = BF.getAddress() + CurrentOffset;
        const uint64_t Size = reportInstructionSize(BC, Inst);
        std::string Text;
        raw_string_ostream TextOS(Text);
        MCInst PrintableInst = Inst;
        BC.MIB->stripAnnotations(PrintableInst);
        BC.InstPrinter->printInst(&PrintableInst, 0, "", *BC.STI, TextOS);
        TextOS.flush();
        const MCInstrDesc &Desc = BC.MII->get(Inst.getOpcode());
        const MCSymbol *TargetSymbol = BC.MIB->isBranch(Inst)
                                           ? BC.MIB->getTargetSymbol(Inst)
                                           : nullptr;
        const BinaryBasicBlock *TargetBB =
            TargetSymbol ? BF.getBasicBlockForLabel(TargetSymbol) : nullptr;
        OriginalInstructions.insert_or_assign(
            Address, OriginalInstructionRecord{Address, Size, std::move(Text),
                                               Desc.mayLoad(), Desc.mayStore(),
                                               BC.MIB->isCall(Inst),
                                               BC.MIB->isBranch(Inst),
                                               BC.MIB->isUnconditionalBranch(Inst),
                                               BC.MIB->isReturn(Inst),
                                               TargetBB
                                                   ? std::optional<uint64_t>(
                                                         BF.getAddress() +
                                                         TargetBB->getOffset())
                                                   : std::nullopt});
        CurrentOffset += Size;
      }
    }
  }
  BC.outs() << "BOLT-INFO: FastDyn attached " << Annotated
            << " original instruction identities\n";
  return Error::success();
}

Error ThumbLiteralPoolPlacement::runOnFunctions(BinaryContext &BC) {
  if (!BC.isARM())
    return Error::success();

  struct ReferencePlan {
    MCInst *Inst;
    BinaryBasicBlock *BB;
    uint64_t TargetAddress;
    MCInst *OwnerBranch{nullptr};
    MCSymbol *CloneLabel{nullptr};
  };
  struct PoolPlan {
    uint64_t StartOffset;
    uint64_t EndOffset;
    uint64_t BlockStartOffset{0};
    BinaryBasicBlock *OwnerBB{nullptr};
    MCInst *OwnerBranch{nullptr};
    SmallVector<ReferencePlan, 4> References;
    SmallVector<uint64_t, 2> PaddingInstructionAddresses;
    InlineDataBlock Block;
  };

  uint64_t QualifiedFunctions = 0;
  uint64_t PlacedPools = 0;
  uint64_t ReboundReferences = 0;
  uint64_t RetainedFunctions = 0;
  uint64_t UnreferencedIslandFunctions = 0;
  uint64_t ClonedPools = 0;
  uint64_t ConsumedCapacityBytes = 0;

  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored() || BF.isSplit() ||
        BF.isMultiEntry() || BF.isCortexMVectorEntry() || BF.hasJumpTables() ||
        !BF.hasConstantIsland())
      continue;

    BinaryFunction::IslandInfo &Islands = BF.getIslandInfo();
    if (opts::Verbosity >= 2)
      BC.outs() << "BOLT-INFO: literal-pool model for " << BF.getPrintName()
                << " has " << Islands.Relocations.size()
                << " retained relocation(s)\n";

    // Some ARM symbol boundaries conservatively acquire island metadata even
    // though their executable body has no data PC-relative reference.  Classify
    // those functions before asking whether an island can be placed; an empty
    // plan is not a rejected transformation when there is nothing to rewrite.
    uint64_t PCRelReferences = 0;
    for (BinaryBasicBlock &BB : BF)
      for (MCInst &Inst : BB)
        if (!BC.MIB->isPseudo(Inst) && !BC.MIB->isBranch(Inst) &&
            !BC.MIB->isCall(Inst) && BC.MIB->hasPCRelOperand(Inst))
          ++PCRelReferences;
    if (!PCRelReferences) {
      ++UnreferencedIslandFunctions;
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: skipped literal-pool placement in "
                  << BF.getPrintName()
                  << ": no PC-relative pool references\n";
      continue;
    }

    if (Islands.HasDynamicRelocations || !Islands.Dependency.empty()) {
      ++RetainedFunctions;
      continue;
    }
    const StringRef OriginName = BF.getOriginSection()->getName();
    const std::string RelName = (Twine(".rel") + OriginName).str();
    const std::string RelaName = (Twine(".rela") + OriginName).str();
    const bool HasUnmodeledInputRelocations =
        Islands.Relocations.empty() &&
        llvm::any_of(BC.sections(), [&](const BinarySection &Section) {
          return Section.getName() == RelName ||
                 Section.getName() == RelaName;
        });
    if (HasUnmodeledInputRelocations) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in "
                  << BF.getPrintName()
                  << ": input relocation section is not modeled\n";
      ++RetainedFunctions;
      continue;
    }

    SmallVector<PoolPlan, 4> Plans;
    SmallVector<PoolPlan *, 2> CapacityPlans;
    StringRef RejectReason;
    for (auto Data = Islands.DataOffsets.begin();
         Data != Islands.DataOffsets.end(); ++Data) {
      auto NextData = std::next(Data);
      auto Code = Islands.CodeOffsets.lower_bound(*Data);
      uint64_t End = BF.getSize();
      if (Code != Islands.CodeOffsets.end())
        End = std::min(End, *Code);
      if (NextData != Islands.DataOffsets.end())
        End = std::min(End, *NextData);
      if (*Data < End)
        Plans.push_back(PoolPlan{*Data, End});
    }
    if (Plans.empty()) {
      ++RetainedFunctions;
      continue;
    }

    auto findPlan = [&](uint64_t Address) -> PoolPlan * {
      if (Address < BF.getAddress())
        return nullptr;
      const uint64_t Offset = Address - BF.getAddress();
      for (PoolPlan &Plan : Plans)
        if (Offset >= Plan.StartOffset && Offset < Plan.EndOffset)
          return &Plan;
      return nullptr;
    };

    bool Qualified = true;
    for (BinaryBasicBlock &BB : BF) {
      for (MCInst &Inst : BB) {
        // Direct control transfers are PC-relative too, but they reference
        // code labels rather than bytes in a literal island.  Classifying
        // them as data references would reject every guarded pool at its
        // mandatory skip branch.
        if (BC.MIB->isPseudo(Inst) || BC.MIB->isBranch(Inst) ||
            BC.MIB->isCall(Inst) || !BC.MIB->hasPCRelOperand(Inst))
          continue;
        const StringRef OpcodeName = BC.MII->getName(Inst.getOpcode());
        if ((OpcodeName != StringRef("tLDRpci") &&
             OpcodeName != StringRef("t2LDRpci") &&
             OpcodeName != StringRef("tADR")) ||
            Inst.getNumOperands() < 2 ||
            !Inst.getOperand(1).isExpr()) {
          RejectReason = "unsupported PC-relative instruction";
          Qualified = false;
          break;
        }
        const auto [Symbol, Addend] =
            BC.MIB->getTargetSymbolInfo(Inst.getOperand(1).getExpr());
        if (!Symbol) {
          RejectReason = "literal operand has no symbol";
          Qualified = false;
          break;
        }
        ErrorOr<uint64_t> SymbolValue = BC.getSymbolValue(*Symbol);
        if (!SymbolValue) {
          RejectReason = "literal symbol has no resolved value";
          Qualified = false;
          break;
        }
        const uint64_t Target = *SymbolValue + Addend;
        if (OpcodeName == StringRef("t2LDRpci") &&
            AllowLiteralAddressChanges) {
          RejectReason =
              "Thumb-2 literal cloning is not qualified by this contract";
          Qualified = false;
          break;
        }
        PoolPlan *Plan = findPlan(Target);
        if (!Plan || (Target & 3) || Target + 4 >
                                         BF.getAddress() + Plan->EndOffset) {
          RejectReason = "literal target is outside a qualified pool";
          Qualified = false;
          break;
        }
        Plan->References.push_back({&Inst, &BB, Target});
      }
      if (!Qualified)
        break;
    }
    if (!Qualified) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in " << BF.getPrintName()
                  << ": " << RejectReason << "\n";
      ++RetainedFunctions;
      continue;
    }

    // Every referenced pool must have a proven non-fallthrough owner: either
    // a direct one-successor skip branch to the following code block, or an
    // adjacent return/unconditional terminal transfer with no CFG successor.
    // The latter is the common "terminal literal pool" form emitted after a
    // return without a redundant skip branch. Both forms prove that the data
    // bytes cannot be executed and keep placement tied to a concrete owner.
    for (PoolPlan &Plan : Plans) {
      if (Plan.References.empty())
        continue;
      BinaryBasicBlock *ResumeBB = nullptr;
      for (BinaryBasicBlock &BB : BF)
        if (BB.getOffset() == Plan.EndOffset) {
          ResumeBB = &BB;
          break;
        }
      if (ResumeBB)
        for (BinaryBasicBlock &BB : BF) {
          MCInst *Branch = BB.getLastNonPseudoInstr();
          if (!Branch || !BC.MIB->isUnconditionalBranch(*Branch) ||
              BB.succ_size() != 1 || BB.getSuccessor() != ResumeBB ||
              BC.MIB->getInlineData(*Branch) ||
              BC.MIB->getInlineDataBlocks(*Branch))
            continue;
          const std::optional<uint32_t> Offset = BC.MIB->getOffset(*Branch);
          const std::optional<uint32_t> Size =
              BC.MIB->getInstructionSize(*Branch);
          if (!Offset || !Size || *Offset + *Size > Plan.StartOffset ||
              Plan.StartOffset - (*Offset + *Size) > 3)
            continue;
          Plan.BlockStartOffset = *Offset + *Size;
          Plan.OwnerBB = &BB;
          Plan.OwnerBranch = Branch;
          break;
        }

      if (!Plan.OwnerBranch)
        for (BinaryBasicBlock &BB : BF) {
          MCInst *Terminator = BB.getLastNonPseudoInstr();
          if (!Terminator || BB.succ_size() > 1 ||
              (!BC.MIB->isReturn(*Terminator) &&
               !BC.MIB->isUnconditionalBranch(*Terminator)) ||
              BC.MIB->getInlineData(*Terminator) ||
              BC.MIB->getInlineDataBlocks(*Terminator))
            continue;
          const std::optional<uint32_t> Offset =
              BC.MIB->getOffset(*Terminator);
          const std::optional<uint32_t> Size =
              BC.MIB->getInstructionSize(*Terminator);
          if (!Offset || !Size || *Offset + *Size > Plan.StartOffset ||
              Plan.StartOffset - (*Offset + *Size) > 3)
            continue;
          Plan.BlockStartOffset = *Offset + *Size;
          Plan.OwnerBB = &BB;
          Plan.OwnerBranch = Terminator;
          break;
        }
      if (!Plan.OwnerBranch) {
        RejectReason = "pool has no proven non-fallthrough owner";
        Qualified = false;
        break;
      }

      if (!AllowLiteralAddressChanges) {
        // The default firmware contract preserves every literal access
        // address. Keep one shared pool at its original skip owner and bind
        // all users to that one identity. Final layout validation requires
        // its exact address as well as architectural reach.
        for (ReferencePlan &Reference : Plan.References)
          Reference.OwnerBranch = Plan.OwnerBranch;
      } else {
        // Under an explicit relaxed contract, a shared value pool may be
        // cloned after multiple existing non-fallthrough owners. Each
        // consumer must already end in either an unconditional transfer or a
        // return. ADR is excluded because, unlike LDR (literal), it observes
        // the address.
        DenseMap<BinaryBasicBlock *, MCInst *> ConsumerBranches;
        for (ReferencePlan &Reference : Plan.References) {
          MCInst *Branch = nullptr;
          if (Reference.BB == Plan.OwnerBB) {
            Branch = Plan.OwnerBranch;
          } else {
            Branch = Reference.BB->getLastNonPseudoInstr();
            if (!Branch ||
                (!BC.MIB->isUnconditionalBranch(*Branch) &&
                 !BC.MIB->isReturn(*Branch)) ||
                Reference.BB->succ_size() > 1 ||
                BC.MIB->getInlineData(*Branch) ||
                BC.MIB->getInlineDataBlocks(*Branch)) {
              RejectReason =
                  "shared pool consumer has no non-fallthrough owner";
              Qualified = false;
              break;
            }
            if (BC.MII->getName(Reference.Inst->getOpcode()) !=
                StringRef("tLDRpci")) {
              RejectReason = "shared pool has an address-producing consumer";
              Qualified = false;
              break;
            }
          }
          Reference.OwnerBranch = Branch;
          ConsumerBranches.try_emplace(Reference.BB, Branch);
        }
        if (!Qualified)
          break;

        for (const auto &[ConsumerBB, Branch] : ConsumerBranches) {
          const std::optional<uint32_t> BranchOffset =
              BC.MIB->getOffset(*Branch);
          const std::optional<uint32_t> BranchSize =
              BC.MIB->getInstructionSize(*Branch);
          if (!BranchOffset || !BranchSize) {
            RejectReason = "shared pool owner has no exact branch offset";
            Qualified = false;
            break;
          }
          uint64_t FirstReferencePC = std::numeric_limits<uint64_t>::max();
          uint64_t MaxTargetOffset = 0;
          for (const ReferencePlan &Reference : Plan.References) {
            if (Reference.BB != ConsumerBB)
              continue;
            const std::optional<uint32_t> ReferenceOffset =
                BC.MIB->getOffset(*Reference.Inst);
            if (!ReferenceOffset) {
              RejectReason = "shared pool reference has no exact offset";
              Qualified = false;
              break;
            }
            FirstReferencePC = std::min<uint64_t>(
                FirstReferencePC,
                alignDown(uint64_t(*ReferenceOffset) + 4, uint64_t(4)));
            MaxTargetOffset = std::max<uint64_t>(
                MaxTargetOffset,
                Reference.TargetAddress -
                    (BF.getAddress() + Plan.StartOffset));
          }
          if (!Qualified)
            break;
          const uint64_t EstimatedPoolStart =
              alignTo(uint64_t(*BranchOffset) + *BranchSize, uint64_t(4));
          if (FirstReferencePC == std::numeric_limits<uint64_t>::max() ||
              EstimatedPoolStart + MaxTargetOffset < FirstReferencePC ||
              EstimatedPoolStart + MaxTargetOffset - FirstReferencePC >
                  1020) {
            RejectReason = "shared pool consumer exceeds Thumb-1 reach";
            Qualified = false;
            break;
          }
        }
        if (!Qualified)
          break;
      }

      // The assembler can encode pool alignment as one or more NOPs after
      // the owner terminator and the $d marker. Those bytes are unreachable by
      // construction and become typed data, but only when they have no
      // predecessor or entry identity and are exact no-ops.
      for (BinaryBasicBlock &BB : BF)
        for (MCInst &Inst : BB) {
          if (BC.MIB->isPseudo(Inst))
            continue;
          const std::optional<uint32_t> Offset = BC.MIB->getOffset(Inst);
          const std::optional<uint32_t> Size =
              BC.MIB->getInstructionSize(Inst);
          if (!Offset || !Size || *Offset < Plan.BlockStartOffset ||
              *Offset >= Plan.StartOffset)
            continue;
          const bool IsArchitecturalNoOp =
              BC.MIB->isNoop(Inst) ||
              (BC.MII->getName(Inst.getOpcode()) == StringRef("tMOVr") &&
               Inst.getNumOperands() >= 2 && Inst.getOperand(0).isReg() &&
               Inst.getOperand(1).isReg() &&
               Inst.getOperand(0).getReg() == Inst.getOperand(1).getReg());
          if (*Offset + *Size > Plan.StartOffset ||
              !IsArchitecturalNoOp || BB.pred_size() != 0 ||
              BF.isEntryPoint(BB)) {
            RejectReason = "pool alignment bytes are reachable or non-NOP";
            Qualified = false;
            break;
          }
          Plan.PaddingInstructionAddresses.push_back(BF.getAddress() +
                                                     *Offset);
        }
      if (!Qualified)
        break;
    }
    if (!Qualified) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in " << BF.getPrintName()
                  << ": " << RejectReason << "\n";
      ++RetainedFunctions;
      continue;
    }

    // Pool duplication consumes only explicitly typed capacity inside the
    // same qualified function range. MaxSize gaps are not firmware capacity.
    // The conservative two-byte allowance covers either Thumb halfword
    // residue for every cloned block; final emission still uses exact linked
    // alignment and must fit the symbol-bounded function size.
    uint64_t RequiredCloneCapacity = 0;
    uint64_t FunctionClones = 0;
    for (const PoolPlan &Plan : Plans) {
      SmallVector<const MCInst *, 4> Owners;
      for (const ReferencePlan &Reference : Plan.References)
        if (!llvm::is_contained(Owners, Reference.OwnerBranch))
          Owners.push_back(Reference.OwnerBranch);
      if (Owners.size() > 1) {
        FunctionClones += Owners.size() - 1;
        RequiredCloneCapacity +=
            (Owners.size() - 1) * (Plan.EndOffset - Plan.StartOffset) +
            Owners.size() * 2;
      }
    }
    uint64_t AvailableCloneCapacity = 0;
    if (RequiredCloneCapacity)
      for (PoolPlan &Plan : Plans) {
        if (!Plan.References.empty())
          continue;
        const uint64_t Address = BF.getAddress() + Plan.StartOffset;
        const BinaryData *Data = BC.getBinaryDataAtAddress(Address);
        if (!Data ||
            !Data->nameStartsWith("__firmbolt_thumb_literal_capacity") ||
            Data->getAddress() != Address ||
            Data->getSize() != Plan.EndOffset - Plan.StartOffset)
          continue;
        CapacityPlans.push_back(&Plan);
        AvailableCloneCapacity += Data->getSize();
        if (AvailableCloneCapacity >= RequiredCloneCapacity)
          break;
      }
    if (RequiredCloneCapacity > AvailableCloneCapacity) {
      RejectReason = "shared pool lacks typed clone capacity";
      Qualified = false;
    }
    if (!Qualified) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in "
                  << BF.getPrintName() << ": " << RejectReason << "\n";
      ++RetainedFunctions;
      continue;
    }

    DenseSet<const MCInst *> AllowedPoolReferences;
    for (const PoolPlan &Plan : Plans)
      for (const ReferencePlan &Reference : Plan.References)
        AllowedPoolReferences.insert(Reference.Inst);

    // Moving a pool is legal only with a complete statically visible
    // reference set. Check every symbolic instruction operand and allocated
    // relocation, not just the owning function's literal-load opcode.
    for (const auto &OtherBFI : BC.getBinaryFunctions()) {
      const BinaryFunction &OtherBF = OtherBFI.second;
      if (!OtherBF.hasCFG())
        continue;
      for (const BinaryBasicBlock &OtherBB : OtherBF)
        for (const MCInst &Inst : OtherBB) {
          for (const MCOperand &Operand : MCPlus::primeOperands(Inst)) {
            if (!Operand.isExpr())
              continue;
            const auto [Symbol, Addend] =
                BC.MIB->getTargetSymbolInfo(Operand.getExpr());
            if (!Symbol)
              continue;
            ErrorOr<uint64_t> Value = BC.getSymbolValue(*Symbol);
            if (Value && findPlan(*Value + Addend) &&
                !AllowedPoolReferences.contains(&Inst)) {
              RejectReason = "pool has a non-owner instruction reference";
              Qualified = false;
              break;
            }
          }
          if (!Qualified)
            break;
        }
      if (!Qualified)
        break;
    }
    if (Qualified)
      for (const BinarySection &Section : BC.allocatableSections()) {
        for (const Relocation &Reloc : Section.relocations()) {
          if (!Reloc.Symbol)
            continue;
          ErrorOr<uint64_t> Value = BC.getSymbolValue(*Reloc.Symbol);
          if (Value && findPlan(*Value + Reloc.Addend)) {
            RejectReason = "pool has an external relocation reference";
            Qualified = false;
            break;
          }
        }
        if (!Qualified)
          break;
      }
    if (Qualified) {
      // Fully linked firmware commonly omits relocation records. A raw
      // address in a vector, object table, or another literal pool is still a
      // reference and would become stale if this pool moved. Conservatively
      // scan every byte position in allocated contents for an exact 32-bit
      // Arm/Thumb address. This can retain a coincidental numeric constant,
      // but it never converts missing relocation evidence into permission.
      for (const BinarySection &Section : BC.allocatableSections()) {
        const StringRef Contents = Section.getContents();
        for (uint64_t Offset = 0; Offset + 4 <= Contents.size(); ++Offset) {
          const uint64_t SourceAddress = Section.getAddress() + Offset;
          const bool IsInsideOwnedPool = llvm::any_of(
              Plans, [&](const PoolPlan &Plan) {
                return SourceAddress >= BF.getAddress() + Plan.StartOffset &&
                       SourceAddress + 4 <=
                           BF.getAddress() + Plan.EndOffset;
              });
          if (IsInsideOwnedPool)
            continue;
          const uint64_t Raw = support::endian::read32le(
              reinterpret_cast<const uint8_t *>(Contents.data() + Offset));
          if (findPlan(Raw) || findPlan(Raw & ~uint64_t(1))) {
            RejectReason = "pool has a raw allocated-section reference";
            Qualified = false;
            break;
          }
        }
        if (!Qualified)
          break;
      }
    }
    if (!Qualified) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in " << BF.getPrintName()
                  << ": " << RejectReason << "\n";
      ++RetainedFunctions;
      continue;
    }

    ErrorOr<ArrayRef<uint8_t>> FunctionData = BF.getData();
    if (!FunctionData) {
      ++RetainedFunctions;
      continue;
    }

    DenseMap<MCInst *, InlineDataBlocks> BlocksByOwner;
    for (PoolPlan &Plan : Plans) {
      if (Plan.References.empty())
        continue;
      // Keep alignment padding as unreachable instructions until UCE, then
      // recreate only the typed pool with an explicit architectural
      // alignment. This makes the cloned label word-aligned even when layout
      // changes the owner's modulo-four address.
      Plan.Block.OriginalAddress = BF.getAddress() + Plan.StartOffset;
      Plan.Block.Alignment = 4;
      Plan.Block.EstimatedAlignmentPadding =
          Plan.StartOffset - Plan.BlockStartOffset;
      if (Plan.Block.EstimatedAlignmentPadding) {
        if (Plan.Block.EstimatedAlignmentPadding != 2) {
          RejectReason = "pool requires unsupported alignment padding";
          Qualified = false;
          break;
        }
        Plan.Block.AlignmentFillValue = support::endian::read16le(
            FunctionData->data() + Plan.BlockStartOffset);
        Plan.Block.AlignmentFillValueSize = 2;
      }
      Plan.Block.DataStartOffset = 0;
      Plan.Block.Bytes.append(FunctionData->begin() + Plan.StartOffset,
                              FunctionData->begin() + Plan.EndOffset);

      for (auto Reloc = Islands.Relocations.lower_bound(Plan.StartOffset);
           Reloc != Islands.Relocations.end() &&
           Reloc->first < Plan.EndOffset;
           ++Reloc) {
        const Relocation &R = Reloc->second;
        const size_t Size = R.getSize(BC.getRelocationHandler());
        if (!Size || Reloc->first + Size > Plan.EndOffset || R.isRELR()) {
          RejectReason = "pool contains an unsupported relocation";
          Qualified = false;
          break;
        }
        Plan.Block.Relocations.push_back(
            {static_cast<uint32_t>(Reloc->first - Plan.StartOffset),
             R.Symbol, R.Type, R.Addend, R.Value});
      }
      if (!Qualified)
        break;

      SmallVector<MCInst *, 4> ConsumerOwners;
      for (const ReferencePlan &Reference : Plan.References)
        if (!llvm::is_contained(ConsumerOwners, Reference.OwnerBranch))
          ConsumerOwners.push_back(Reference.OwnerBranch);
      for (MCInst *Owner : ConsumerOwners) {
        InlineDataBlock CloneBlock = Plan.Block;
        CloneBlock.Labels.clear();
        // A cloned block can land at either halfword residue after block
        // reordering. Reserve the maximum Thumb padding in size estimates;
        // the emitter inserts it only when the final address needs it.
        if (Owner != Plan.OwnerBranch) {
          CloneBlock.EstimatedAlignmentPadding = 2;
          CloneBlock.AlignmentFillValue = 0xbf00;
          CloneBlock.AlignmentFillValueSize = 2;
        }
        DenseMap<uint64_t, MCSymbol *> Labels;
        for (ReferencePlan &Reference : Plan.References) {
          if (Reference.OwnerBranch != Owner)
            continue;
          MCSymbol *&Clone = Labels[Reference.TargetAddress];
          if (!Clone) {
            Clone = BC.Ctx->createNamedTempSymbol("ThumbLiteralClone");
            CloneBlock.Labels.push_back(
                {static_cast<uint32_t>(Reference.TargetAddress -
                                       Plan.Block.OriginalAddress),
                 Clone});
          }
          Reference.CloneLabel = Clone;
        }
        llvm::sort(CloneBlock.Labels, [](const auto &A, const auto &B) {
          return A.first < B.first;
        });
        BlocksByOwner[Owner].push_back(std::move(CloneBlock));
      }
    }
    if (!Qualified) {
      if (opts::Verbosity >= 2)
        BC.outs() << "BOLT-INFO: retained literal pools in " << BF.getPrintName()
                  << ": " << RejectReason << "\n";
      ++RetainedFunctions;
      continue;
    }

    for (PoolPlan &Plan : Plans) {
      if (Plan.References.empty())
        continue;
      for (const ReferencePlan &Reference : Plan.References) {
        MCSymbol *Clone = Reference.CloneLabel;
        assert(Clone && "qualified literal clone must have a label");
        Reference.Inst->getOperand(1) = MCOperand::createExpr(
            MCSymbolRefExpr::create(Clone, *BC.Ctx));
        ++ReboundReferences;
      }
    }
    for (auto &[Owner, Blocks] : BlocksByOwner)
      BC.MIB->setInlineDataBlocks(*Owner, Blocks);

    for (const PoolPlan &Plan : Plans) {
      if (Plan.References.empty())
        continue;
      Islands.InlineOwnedDataOffsets.insert(Plan.StartOffset);
      for (uint64_t Address : Plan.PaddingInstructionAddresses)
        Islands.InlineOwnedPaddingInstructionAddresses.insert(Address);
    }
    for (const PoolPlan *Capacity : CapacityPlans) {
      Islands.InlineOwnedDataOffsets.insert(Capacity->StartOffset);
      ConsumedCapacityBytes += Capacity->EndOffset - Capacity->StartOffset;
    }
    ClonedPools += FunctionClones;

    // The guarded alignment no-ops are now represented by the owned block's
    // explicit alignment fill. Remove their decoded instruction nodes even
    // when the caller disables generic unreachable-code elimination.
    for (BinaryBasicBlock &BB : BF)
      for (auto It = BB.begin(); It != BB.end();) {
        const std::optional<uint32_t> Offset = BC.MIB->getOffset(*It);
        if (Offset && Islands.InlineOwnedPaddingInstructionAddresses.count(
                          BF.getAddress() + *Offset))
          It = BB.eraseInstruction(It);
        else
          ++It;
      }

    BF.clearRequiresAddressStableLayout(
        BinaryFunction::AddressStabilityReason::ThumbPCRelative);
    BF.setFirmwareModified(
        BinaryFunction::FirmwareModification::LiteralPoolPlacement);
    ++QualifiedFunctions;
    for (const auto &[Owner, Blocks] : BlocksByOwner)
      PlacedPools += Blocks.size();
  }

  BC.outs() << "BOLT-INFO: Thumb literal-pool placement qualified "
            << QualifiedFunctions << " function(s), attached " << PlacedPools
            << " owner block(s), rebound " << ReboundReferences
            << " PC-relative reference(s), and retained " << RetainedFunctions
            << " function(s); skipped " << UnreferencedIslandFunctions
            << " unreferenced island function(s)\n";
  if (ClonedPools || ConsumedCapacityBytes)
    BC.outs() << "BOLT-INFO: shared literal-pool cloning created "
              << ClonedPools << " clone(s) and consumed "
              << ConsumedCapacityBytes << " typed capacity byte(s)\n";
  return Error::success();
}

Error CortexMFirmwarePublicationPolicy::runOnFunctions(BinaryContext &BC) {
  if (!BC.isARM())
    return Error::success();

  uint64_t AttemptedFunctions = 0;
  uint64_t RejectedFunctions = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    // Branch repair and final block layout can synthesize merge edges after
    // ICP constructed its initial expansion. Recover their provenance from
    // the exact promoted/fallback call association in the same block before
    // publication; never infer it from address proximity.
    if (BF.hasCFG() &&
        (BF.hasFirmwareModification(
             FirmwareModification::IndirectCallPromotion) ||
         BF.hasFirmwareModification(
             FirmwareModification::JumpTablePromotion))) {
      DenseMap<uint32_t, uint64_t> CallIdentityByInputOffset;
      DenseSet<uint32_t> AmbiguousInputOffsets;
      for (BinaryBasicBlock &BB : BF) {
        if (BB.getOffset() == BinaryBasicBlock::INVALID_OFFSET)
          continue;
        for (const MCInst &Inst : BB)
          if (BC.MIB->isCall(Inst) || BC.MIB->isTailCall(Inst)) {
            std::optional<uint64_t> CallIdentity;
            if (std::optional<uint64_t> Source =
                    getFastDynCallPromotionSource(BC, Inst))
              CallIdentity = *Source;
            if (isFastDynClonedOriginal(BC, Inst))
              if (std::optional<uint64_t> Source =
                      getFastDynOriginalIdentity(BC, Inst))
                CallIdentity = *Source;
            if (!CallIdentity)
              continue;
            const uint32_t Offset = BB.getOffset();
            auto [It, Inserted] =
                CallIdentityByInputOffset.try_emplace(Offset, *CallIdentity);
            if (!Inserted && It->second != *CallIdentity) {
              CallIdentityByInputOffset.erase(Offset);
              AmbiguousInputOffsets.insert(Offset);
            }
          }
      }
      for (BinaryBasicBlock &BB : BF) {
        if (BB.getOffset() == BinaryBasicBlock::INVALID_OFFSET ||
            AmbiguousInputOffsets.contains(BB.getOffset()))
          continue;
        auto Identity = CallIdentityByInputOffset.find(BB.getOffset());
        if (Identity == CallIdentityByInputOffset.end())
          continue;
        for (MCInst &Inst : BB)
          if (BC.MIB->isBranch(Inst) &&
              !getFastDynOriginalIdentity(BC, Inst) &&
              !getFastDynCallPromotionSource(BC, Inst))
            annotateFastDynCallPromotionExpansion(BC, Inst,
                                                  Identity->second);
      }
    }
    if (!BF.hasCFG() || !BF.hasFirmwareModificationAttempt())
      continue;
    ++AttemptedFunctions;

    // Synthesized scalar data is emitted immediately after its owner branch.
    // Recompute that exact in-place address after all ordinary layout passes,
    // before MC emission.  Thumb-1 generators insert explicit unreachable
    // padding from the original call position; if another transformation has
    // changed the parity, retain the reference body instead of allowing an
    // assembler failure or an architecturally different literal address.
    uint64_t InlineOffset = 0;
    bool InvalidInlinePlacement = false;
    for (const BinaryBasicBlock &BB : BF) {
      if ((BC.AlignBlocks || BC.PreserveBlocksAlignment) &&
          BB.getAlignment() > 1)
        InlineOffset += offsetToAlignment(BF.getAddress() + InlineOffset,
                                          BB.getAlign());
      for (const MCInst &Inst : BB) {
        if (BC.MIB->isPseudo(Inst))
          continue;
        const std::optional<uint32_t> Size =
            BC.MIB->getInstructionSize(Inst);
        if (!Size) {
          InvalidInlinePlacement = true;
          break;
        }
        InlineOffset += *Size;
        if (const JumpTable *JT = BF.getJumpTable(Inst))
          if (JT->isThumbInline())
            InlineOffset +=
                JT->getSize() + JT->InlineTrailingBytes.size();
        if (const InlineData *Data = BC.MIB->getInlineData(Inst)) {
          if (!isAligned(Align(Data->Alignment),
                         BF.getAddress() + InlineOffset)) {
            InvalidInlinePlacement = true;
            break;
          }
          InlineOffset += Data->Size;
        }
        if (const InlineDataBlocks *Blocks =
                BC.MIB->getInlineDataBlocks(Inst))
          for (const InlineDataBlock &Block : *Blocks) {
            InlineOffset += offsetToAlignment(BF.getAddress() + InlineOffset,
                                              Align(Block.Alignment));
            InlineOffset += Block.Bytes.size();
          }
      }
      if (InvalidInlinePlacement)
        break;
    }
    if (InvalidInlinePlacement) {
      BF.rejectFirmwarePublication(
          FirmwarePublicationRejection::InlineDataPlacementInvalid);
      ++RejectedFunctions;
      BC.outs() << "BOLT-INFO: Firm-BOLT retained original "
                << BF.getPrintName()
                << ": synthesized inline-data placement is invalid\n";
      continue;
    }

    // Exact vector entries have a bounded phase-one contract: preserve the
    // entry, instruction identities/order/count, stack stores, and the exact
    // exception-return instruction.  Ordinary FOP satisfies that contract by
    // replacing a same-width stack reload with a register move.  Any other
    // transformation (including store deletion, layout or CFG edits) retains
    // the original handler until the richer exception/event model exists.
    if (BF.isCortexMVectorEntry()) {
      const uint32_t AllowedMask = static_cast<uint32_t>(
          FirmwareModification::FrameLoadForwarding);
      if (BF.getFirmwareModificationMask() & ~AllowedMask) {
        BF.rejectFirmwarePublication(
            FirmwarePublicationRejection::ExceptionContractUnsupported);
        ++RejectedFunctions;
        BC.outs() << "BOLT-INFO: Firm-BOLT retained original handler "
                  << BF.getPrintName()
                  << ": transformation exceeds the exception-entry contract\n";
        continue;
      }
      bool DeletedForwarding = false;
      const uint64_t End = BF.getAddress() + BF.getSize();
      for (const auto &[LoadAddress, Forwarding] : FrameForwardings)
        if (LoadAddress >= BF.getAddress() && LoadAddress < End &&
            Forwarding.Deleted) {
          DeletedForwarding = true;
          break;
        }
      if (DeletedForwarding) {
        BF.rejectFirmwarePublication(
            FirmwarePublicationRejection::ExceptionContractUnsupported);
        ++RejectedFunctions;
        BC.outs() << "BOLT-INFO: Firm-BOLT retained original handler "
                  << BF.getPrintName()
                  << ": forwarding deleted an exception-visible instruction\n";
        continue;
      }
    }

    // A typed veneer slot is explicitly outside the admitted original control
    // flow and exists to be replaced.  Its instruction count is capacity, not
    // modeled guest work.  The source call remains subject to the ordinary
    // instruction-count policy.
    if (BF.isFirmwareVeneerSlot() ||
        !opts::PreserveFirmwareInstructionCount)
      continue;

    if (BF.hasFirmwareModification(
            FirmwareModification::LiteralPoolPlacement) &&
        !AllowLiteralAddressChanges) {
      uint64_t EstimatedOffset = 0;
      bool LiteralAddressChanged = false;
      for (const BinaryBasicBlock &BB : BF) {
        if ((BC.AlignBlocks || BC.PreserveBlocksAlignment) &&
            BB.getAlignment() > 1)
          EstimatedOffset += offsetToAlignment(
              BF.getAddress() + EstimatedOffset, BB.getAlign());
        for (const MCInst &Inst : BB) {
          if (BC.MIB->isPseudo(Inst))
            continue;
          const std::optional<uint32_t> Size =
              BC.MIB->getInstructionSize(Inst);
          if (!Size) {
            LiteralAddressChanged = true;
            break;
          }
          EstimatedOffset += *Size;
          if (const JumpTable *JT = BF.getJumpTable(Inst))
            if (JT->isThumbInline())
              EstimatedOffset +=
                  JT->getSize() + JT->InlineTrailingBytes.size();
          if (const InlineData *Data = BC.MIB->getInlineData(Inst))
            EstimatedOffset += Data->Size;
          if (const InlineDataBlocks *Blocks =
                  BC.MIB->getInlineDataBlocks(Inst))
            for (const InlineDataBlock &Block : *Blocks) {
              EstimatedOffset += offsetToAlignment(
                  BF.getAddress() + EstimatedOffset,
                  Align(Block.Alignment));
              if (BF.getAddress() + EstimatedOffset !=
                  Block.OriginalAddress) {
                LiteralAddressChanged = true;
                break;
              }
              EstimatedOffset += Block.Bytes.size();
            }
          if (LiteralAddressChanged)
            break;
        }
        if (LiteralAddressChanged)
          break;
      }
      if (LiteralAddressChanged) {
        BF.rejectFirmwarePublication(
            FirmwarePublicationRejection::LiteralAddressChanged);
        ++RejectedFunctions;
        BC.outs() << "BOLT-INFO: Firm-BOLT retained original "
                  << BF.getPrintName()
                  << ": literal-pool access address changed\n";
        continue;
      }
    }

    const uint64_t OriginalEnd = BF.getAddress() + BF.getSize();
    auto OriginalIt = OriginalInstructions.lower_bound(BF.getAddress());
    uint64_t OriginalInstructionCount = 0;
    while (OriginalIt != OriginalInstructions.end() &&
           OriginalIt->first < OriginalEnd) {
      ++OriginalInstructionCount;
      ++OriginalIt;
    }
    if (BF.hasIslandsInfo()) {
      const uint64_t Consumed =
          BF.getIslandInfo().InlineOwnedPaddingInstructionAddresses.size();
      if (Consumed > OriginalInstructionCount)
        return createStringError(
            inconvertibleErrorCode(),
            "literal-pool padding count exceeds instruction count in %s",
            BF.getPrintName().c_str());
      OriginalInstructionCount -= Consumed;
    }
    const uint64_t CandidateInstructionCount = BF.getNumNonPseudos();
    if (OriginalInstructionCount == CandidateInstructionCount)
      continue;

    BF.rejectFirmwarePublication(
        FirmwarePublicationRejection::InstructionCountChanged);
    ++RejectedFunctions;
    BC.outs() << "BOLT-INFO: Firm-BOLT retained original "
              << BF.getPrintName() << ": guest instruction count changed "
              << OriginalInstructionCount << " -> "
              << CandidateInstructionCount << "\n";
  }

  BC.outs() << "BOLT-INFO: Firm-BOLT publication policy checked "
            << AttemptedFunctions << " attempted function(s), rejected "
            << RejectedFunctions << " for guest instruction-count changes\n";
  // Compact mode can retain a rejected function at its original address.
  // Whole-image relocation mode cannot mix a mutated candidate CFG with the
  // old bytes after addresses and inbound references have moved.  Reject the
  // complete candidate atomically until a reconstruction service can emit an
  // immutable reference body at its new location.
  if (BC.HasRelocations && RejectedFunctions)
    return createStringError(
        inconvertibleErrorCode(),
        "Cortex-M relocation candidate has %" PRIu64
        " function(s) rejected by the firmware publication contract",
        RejectedFunctions);
  return Error::success();
}

bool FastDynArithmeticStrengthReduction::isEnabled() {
  return ArithmeticStrengthReduction;
}

bool FastDynCopyCleanup::isEnabled() { return CopyCleanup; }

bool FastDynMemoryTestFolding::isEnabled() { return MemoryTestFolding; }

bool FastDynTrivialFrameElimination::isEnabled() {
  return TrivialFrameElimination;
}

bool FastDynExactLeafCallInlining::isEnabled() {
  return ExactLeafCallInlining;
}

bool FastDynExactAccessorCallFusion::isEnabled() {
  return ExactAccessorCallFusion;
}

bool FastDynExactEmptyCallElimination::isEnabled() {
  return ExactEmptyCallElimination;
}

Error FastDynTrivialFrameElimination::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn trivial-frame elimination is qualified only for x86");
  if (!opts::AssumeABI)
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn trivial-frame elimination requires --assume-abi");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  uint64_t Eliminated = 0;
  uint64_t Weighted = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored() ||
        BF.isMultiEntry() || BF.size() != 1 ||
        BF.getPersonalityFunction() != nullptr || BF.getLSDAAddress() != 0)
      continue;
    BinaryBasicBlock &BB = *BF.begin();
    std::vector<size_t> RealIndices;
    SmallVector<const MCCFIInstruction *, 4> CFIInstructions;
    bool HasUnsupportedPseudo = false;
    for (size_t Index = 0; Index < BB.size(); ++Index) {
      const MCInst &Inst = *(BB.begin() + Index);
      if (!BC.MIB->isPseudo(Inst)) {
        RealIndices.push_back(Index);
        continue;
      }
      if (!BC.MIB->isCFI(Inst)) {
        HasUnsupportedPseudo = true;
        continue;
      }
      const MCCFIInstruction *CFI = BF.getCFIFor(Inst);
      if (!CFI) {
        HasUnsupportedPseudo = true;
        continue;
      }
      CFIInstructions.push_back(CFI);
    }
    if (HasUnsupportedPseudo || RealIndices.size() < 4)
      continue;

    size_t Base = 0;
    const MCInst &PossibleEntry = *(BB.begin() + RealIndices[0]);
    if (BC.MII->getName(PossibleEntry.getOpcode()) == "ENDBR64") {
      const MCInst &Entry = *(BB.begin() + RealIndices[0]);
      (void)Entry;
      Base = 1;
    }
    if (RealIndices.size() < Base + 4)
      continue;
    MCInst &Push = *(BB.begin() + RealIndices[Base]);
    MCInst &SetFrame = *(BB.begin() + RealIndices[Base + 1]);
    MCInst &Pop = *(BB.begin() + RealIndices[RealIndices.size() - 2]);
    MCInst &Ret = *(BB.begin() + RealIndices.back());
    if (BC.MII->getName(Push.getOpcode()) != "PUSH64r" ||
        BC.MII->getName(SetFrame.getOpcode()) != "MOV64rr" ||
        BC.MII->getName(Pop.getOpcode()) != "POP64r" ||
        !BC.MIB->isReturn(Ret) ||
        MCPlus::getNumPrimeOperands(Push) != 1 ||
        MCPlus::getNumPrimeOperands(SetFrame) != 2 ||
        MCPlus::getNumPrimeOperands(Pop) != 1 ||
        !Push.getOperand(0).isReg() ||
        !SetFrame.getOperand(0).isReg() ||
        !SetFrame.getOperand(1).isReg() ||
        !Pop.getOperand(0).isReg() ||
        StringRef(BC.MRI->getName(Push.getOperand(0).getReg())) != "RBP" ||
        StringRef(BC.MRI->getName(SetFrame.getOperand(0).getReg())) != "RBP" ||
        StringRef(BC.MRI->getName(SetFrame.getOperand(1).getReg())) != "RSP" ||
        StringRef(BC.MRI->getName(Pop.getOperand(0).getReg())) != "RBP")
      continue;

    // Removing the machine frame also removes only its exact unwind-state
    // transitions.  An absent FDE program is supported; any present program
    // must be the canonical RBP frame sequence.  Reject personality/LSDA
    // functions above rather than silently discarding exception metadata.
    if (!CFIInstructions.empty()) {
      const int RBP = BC.MRI->getDwarfRegNum(Push.getOperand(0).getReg(), false);
      const int RSP =
          BC.MRI->getDwarfRegNum(SetFrame.getOperand(1).getReg(), false);
      if (RBP < 0 || RSP < 0 || CFIInstructions.size() != 4 ||
          CFIInstructions[0]->getOperation() !=
              MCCFIInstruction::OpDefCfaOffset ||
          CFIInstructions[0]->getOffset() != 16 ||
          CFIInstructions[1]->getOperation() !=
              MCCFIInstruction::OpOffset ||
          CFIInstructions[1]->getRegister() != static_cast<unsigned>(RBP) ||
          CFIInstructions[1]->getOffset() != -16 ||
          CFIInstructions[2]->getOperation() !=
              MCCFIInstruction::OpDefCfaRegister ||
          CFIInstructions[2]->getRegister() != static_cast<unsigned>(RBP) ||
          CFIInstructions[3]->getOperation() != MCCFIInstruction::OpDefCfa ||
          CFIInstructions[3]->getRegister() != static_cast<unsigned>(RSP) ||
          CFIInstructions[3]->getOffset() != 8)
        continue;
    }

    const BitVector &RBPAliases =
        BC.MIB->getAliases(Push.getOperand(0).getReg(), false);
    const BitVector &RSPAliases =
        BC.MIB->getAliases(SetFrame.getOperand(1).getReg(), false);
    bool PrivateReturnSpill = false;
    size_t PrivateStoreRealPosition = 0;
    if (RealIndices.size() == Base + 6) {
      const MCInst &ValueLoad = *(BB.begin() + RealIndices[Base + 2]);
      const MCInst &PrivateStore = *(BB.begin() + RealIndices[Base + 3]);
      PrivateReturnSpill =
          BC.MII->getName(ValueLoad.getOpcode()) == "MOV64rm" &&
          BC.MII->getName(PrivateStore.getOpcode()) == "MOV64mr" &&
          MCPlus::getNumPrimeOperands(ValueLoad) == 6 &&
          MCPlus::getNumPrimeOperands(PrivateStore) == 6 &&
          ValueLoad.getOperand(0).isReg() &&
          ValueLoad.getOperand(1).isReg() &&
          ValueLoad.getOperand(2).isImm() &&
          ValueLoad.getOperand(3).isReg() &&
          ValueLoad.getOperand(5).isReg() &&
          PrivateStore.getOperand(0).isReg() &&
          PrivateStore.getOperand(1).isImm() &&
          PrivateStore.getOperand(2).isReg() &&
          PrivateStore.getOperand(3).isImm() &&
          PrivateStore.getOperand(4).isReg() &&
          PrivateStore.getOperand(5).isReg() &&
          StringRef(BC.MRI->getName(ValueLoad.getOperand(0).getReg())) ==
              "RAX" &&
          StringRef(BC.MRI->getName(ValueLoad.getOperand(1).getReg())) ==
              "RIP" &&
          ValueLoad.getOperand(2).getImm() == 1 &&
          ValueLoad.getOperand(3).getReg() == 0 &&
          ValueLoad.getOperand(5).getReg() == 0 &&
          PrivateStore.getOperand(0).getReg() ==
              Push.getOperand(0).getReg() &&
          PrivateStore.getOperand(1).getImm() == 1 &&
          PrivateStore.getOperand(2).getReg() == 0 &&
          PrivateStore.getOperand(3).getImm() == -8 &&
          PrivateStore.getOperand(4).getReg() == 0 &&
          StringRef(BC.MRI->getName(PrivateStore.getOperand(5).getReg())) ==
              "RAX";
      PrivateStoreRealPosition = Base + 3;
    }
    bool UnsafeBody = false;
    for (size_t Position = Base + 2;
         Position + 2 < RealIndices.size(); ++Position) {
      const MCInst &Inst = *(BB.begin() + RealIndices[Position]);
      if (PrivateReturnSpill && Position == PrivateStoreRealPosition)
        continue;
      const StringRef Opcode = BC.MII->getName(Inst.getOpcode());
      if (BC.MIB->isCall(Inst) || BC.MIB->isBranch(Inst) ||
          BC.MIB->isReturn(Inst) || Opcode.starts_with("PUSH") ||
          Opcode.starts_with("POP") || Opcode.starts_with("ENTER") ||
          Opcode.starts_with("LEAVE")) {
        UnsafeBody = true;
        break;
      }
      for (const MCOperand &Operand : Inst) {
        if (!Operand.isReg() || Operand.getReg() == 0)
          continue;
        if (RBPAliases.test(Operand.getReg()) ||
            RSPAliases.test(Operand.getReg())) {
          UnsafeBody = true;
          break;
        }
      }
      if (UnsafeBody)
        break;
    }
    if (UnsafeBody)
      continue;
    std::array<MCInst *, 4> Pattern{&Push, &SetFrame, &Pop, &Ret};
    std::array<uint64_t, 4> Origins{};
    bool HasAllOrigins = true;
    for (size_t Index = 0; Index != Pattern.size(); ++Index) {
      if (!BC.MIB->hasAnnotation(*Pattern[Index], OriginIndex)) {
        HasAllOrigins = false;
        break;
      }
      Origins[Index] =
          BC.MIB->getAnnotationAs<uint64_t>(*Pattern[Index], OriginIndex);
    }
    if (!HasAllOrigins)
      continue;
    uint64_t PrivateStoreOrigin = 0;
    if (PrivateReturnSpill) {
      MCInst &PrivateStore =
          *(BB.begin() + RealIndices[PrivateStoreRealPosition]);
      if (!BC.MIB->hasAnnotation(PrivateStore, OriginIndex))
        continue;
      PrivateStoreOrigin =
          BC.MIB->getAnnotationAs<uint64_t>(PrivateStore, OriginIndex);
    }

    // The function now leaves CFA and RBP unchanged, so every CFI directive
    // describing the removed push/frame/pop must disappear as well. Retain
    // the protected entry (when present), every proven stack-independent leaf
    // body instruction, and the original return.
    for (size_t Index = BB.size(); Index-- > 0;) {
      const MCInst &Inst = *(BB.begin() + Index);
      if (BC.MIB->isCFI(Inst) || Index == RealIndices[Base] ||
          Index == RealIndices[Base + 1] ||
          Index == RealIndices[RealIndices.size() - 2] ||
          (PrivateReturnSpill &&
           Index == RealIndices[PrivateStoreRealPosition]))
        BB.eraseInstructionAtIndex(Index);
    }
    std::vector<uint64_t> Consumed{Origins[0], Origins[1], Origins[2]};
    StringRef Kind = "x86_trivial_frame_elimination";
    if (PrivateReturnSpill) {
      Consumed.insert(Consumed.begin() + 2, PrivateStoreOrigin);
      Kind = "x86_private_return_spill_frame_elimination";
    }
    ArithmeticFusions.push_back(ArithmeticFusionRecord{
        Kind.str(), BF.getAddress(), Origins[3], std::move(Consumed),
        BB.getKnownExecutionCount()});
    ++Eliminated;
    Weighted += BB.getKnownExecutionCount();
  }
  BC.outs() << "BOLT-INFO: FastDyn trivial-frame elimination removed "
            << Eliminated << " frame(s) (dyn count: " << Weighted << ")\n";
  return Error::success();
}

Error FastDynExactLeafCallInlining::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact leaf-call inlining is qualified only for x86");
  if (!opts::AssumeABI)
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact leaf-call inlining requires --assume-abi");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  std::set<uint64_t> QualifiedCalleeAddresses;
  for (const ArithmeticFusionRecord &Fusion : ArithmeticFusions)
    if (Fusion.Kind == "x86_private_return_spill_frame_elimination")
      QualifiedCalleeAddresses.insert(Fusion.FunctionAddress);

  struct LeafReplacement {
    MCInst Load;
    uint64_t LoadOrigin;
  };
  std::map<const BinaryFunction *, LeafReplacement> Replacements;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!QualifiedCalleeAddresses.count(BF.getAddress()) || !BF.hasCFG() ||
        !BF.isSimple() || BF.isIgnored() || BF.isMultiEntry() ||
        BF.size() != 1)
      continue;
    BinaryBasicBlock &BB = *BF.begin();
    SmallVector<const MCInst *, 3> Real;
    for (const MCInst &Inst : BB)
      if (!BC.MIB->isPseudo(Inst))
        Real.push_back(&Inst);
    size_t Base = 0;
    if (!Real.empty() && BC.MII->getName(Real[0]->getOpcode()) == "ENDBR64")
      Base = 1;
    if (Real.size() != Base + 2 ||
        BC.MII->getName(Real[Base]->getOpcode()) != "MOV64rm" ||
        !BC.MIB->isReturn(*Real[Base + 1]) ||
        !BC.MIB->hasAnnotation(*Real[Base], OriginIndex))
      continue;
    const MCInst &Load = *Real[Base];
    if (MCPlus::getNumPrimeOperands(Load) != 6 ||
        !Load.getOperand(0).isReg() || !Load.getOperand(1).isReg() ||
        !Load.getOperand(2).isImm() || !Load.getOperand(3).isReg() ||
        !Load.getOperand(5).isReg() ||
        StringRef(BC.MRI->getName(Load.getOperand(0).getReg())) != "RAX" ||
        StringRef(BC.MRI->getName(Load.getOperand(1).getReg())) != "RIP" ||
        Load.getOperand(2).getImm() != 1 ||
        Load.getOperand(3).getReg() != 0 || Load.getOperand(5).getReg() != 0)
      continue;
    Replacements.insert_or_assign(
        &BF, LeafReplacement{Load, BC.MIB->getAnnotationAs<uint64_t>(
                                       Load, OriginIndex)});
  }

  uint64_t Inlined = 0;
  uint64_t Weighted = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &Caller = BFI.second;
    if (!Caller.hasCFG() || Caller.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : Caller) {
      if (BB.getKnownExecutionCount() == 0)
        continue;
      for (auto II = BB.begin(); II != BB.end(); ++II) {
        if (BC.MIB->isPseudo(*II) || !BC.MIB->isCall(*II) ||
            !BC.MIB->hasAnnotation(*II, OriginIndex))
          continue;
        const MCSymbol *Target = BC.MIB->getTargetSymbol(*II);
        const BinaryFunction *Callee =
            Target ? BC.getFunctionForSymbol(Target) : nullptr;
        auto Replacement = Replacements.find(Callee);
        if (Replacement == Replacements.end())
          continue;
        const uint64_t CallOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(*II, OriginIndex);
        SmallVector<MCInst, 1> NewCode{Replacement->second.Load};
        II = BB.replaceInstruction(II, NewCode.begin(), NewCode.end());
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_exact_leaf_call_inline", Caller.getAddress(),
            Replacement->second.LoadOrigin, {CallOrigin},
            BB.getKnownExecutionCount()});
        ++Inlined;
        Weighted += BB.getKnownExecutionCount();
      }
    }
  }
  BC.outs() << "BOLT-INFO: FastDyn exact leaf-call inlining replaced "
            << Inlined << " call(s) (dyn count: " << Weighted << ")\n";
  return Error::success();
}

Error FastDynExactAccessorCallFusion::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact accessor-call fusion is qualified only for x86");
  if (!opts::AssumeABI)
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact accessor-call fusion requires --assume-abi");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  struct AccessorReplacement {
    MCInst Load;
    MCInst Add;
    uint64_t LoadOrigin;
    uint64_t AddOrigin;
  };
  std::map<const BinaryFunction *, AccessorReplacement> Replacements;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored() ||
        BF.isMultiEntry() || BF.size() != 1 ||
        BF.getPersonalityFunction() != nullptr || BF.getLSDAAddress() != 0)
      continue;
    BinaryBasicBlock &BB = *BF.begin();
    SmallVector<const MCInst *, 9> Real;
    for (const MCInst &Inst : BB)
      if (!BC.MIB->isPseudo(Inst))
        Real.push_back(&Inst);
    size_t Base = 0;
    if (!Real.empty() && BC.MII->getName(Real[0]->getOpcode()) == "ENDBR64")
      Base = 1;
    if (Real.size() != Base + 8)
      continue;
    const MCInst &Push = *Real[Base];
    const MCInst &SetFrame = *Real[Base + 1];
    const MCInst &ArgumentSpill = *Real[Base + 2];
    const MCInst &ValueLoad = *Real[Base + 3];
    const MCInst &ReturnSpill = *Real[Base + 4];
    const MCInst &Add = *Real[Base + 5];
    const MCInst &Pop = *Real[Base + 6];
    const MCInst &Ret = *Real[Base + 7];
    if (BC.MII->getName(Push.getOpcode()) != "PUSH64r" ||
        BC.MII->getName(SetFrame.getOpcode()) != "MOV64rr" ||
        BC.MII->getName(ArgumentSpill.getOpcode()) != "MOV64mr" ||
        BC.MII->getName(ValueLoad.getOpcode()) != "MOV64rm" ||
        BC.MII->getName(ReturnSpill.getOpcode()) != "MOV64mr" ||
        BC.MII->getName(Add.getOpcode()) != "ADD64ri8" ||
        BC.MII->getName(Pop.getOpcode()) != "POP64r" ||
        !BC.MIB->isReturn(Ret) ||
        MCPlus::getNumPrimeOperands(Push) != 1 ||
        MCPlus::getNumPrimeOperands(SetFrame) != 2 ||
        MCPlus::getNumPrimeOperands(ArgumentSpill) != 6 ||
        MCPlus::getNumPrimeOperands(ValueLoad) != 6 ||
        MCPlus::getNumPrimeOperands(ReturnSpill) != 6 ||
        !Push.getOperand(0).isReg() ||
        !SetFrame.getOperand(0).isReg() || !SetFrame.getOperand(1).isReg() ||
        !ValueLoad.getOperand(0).isReg() || !ValueLoad.getOperand(1).isReg() ||
        !ValueLoad.getOperand(2).isImm() || !ValueLoad.getOperand(3).isReg() ||
        !ValueLoad.getOperand(4).isImm() || !ValueLoad.getOperand(5).isReg() ||
        MCPlus::getNumPrimeOperands(Add) != 3 ||
        !Add.getOperand(0).isReg() || !Add.getOperand(2).isImm())
      continue;
    if (StringRef(BC.MRI->getName(Push.getOperand(0).getReg())) != "RBP" ||
        StringRef(BC.MRI->getName(SetFrame.getOperand(0).getReg())) != "RBP" ||
        StringRef(BC.MRI->getName(SetFrame.getOperand(1).getReg())) != "RSP" ||
        StringRef(BC.MRI->getName(ValueLoad.getOperand(0).getReg())) != "RAX" ||
        StringRef(BC.MRI->getName(ValueLoad.getOperand(1).getReg())) != "RDI" ||
        ValueLoad.getOperand(2).getImm() != 1 ||
        ValueLoad.getOperand(3).getReg() != 0 ||
        ValueLoad.getOperand(4).getImm() != 0 ||
        ValueLoad.getOperand(5).getReg() != 0 ||
        StringRef(BC.MRI->getName(Add.getOperand(0).getReg())) != "RAX" ||
        Add.getOperand(2).getImm() != 8 ||
        !BC.MIB->hasAnnotation(ValueLoad, OriginIndex) ||
        !BC.MIB->hasAnnotation(Add, OriginIndex))
      continue;

    auto ExactStackStore = [&](const MCInst &Store, int64_t Displacement,
                               StringRef Source) {
      return Store.getOperand(0).isReg() && Store.getOperand(1).isImm() &&
             Store.getOperand(2).isReg() && Store.getOperand(3).isImm() &&
             Store.getOperand(4).isReg() && Store.getOperand(5).isReg() &&
             StringRef(BC.MRI->getName(Store.getOperand(0).getReg())) ==
                 "RBP" &&
             Store.getOperand(1).getImm() == 1 &&
             Store.getOperand(2).getReg() == 0 &&
             Store.getOperand(3).getImm() == Displacement &&
             Store.getOperand(4).getReg() == 0 &&
             StringRef(BC.MRI->getName(Store.getOperand(5).getReg())) ==
                 Source;
    };
    if (!ExactStackStore(ArgumentSpill, -24, "RDI") ||
        !ExactStackStore(ReturnSpill, -8, "RAX"))
      continue;

    MCInst DirectLoad = ValueLoad;
    DirectLoad.getOperand(1).setReg(ValueLoad.getOperand(0).getReg());
    Replacements.insert_or_assign(
        &BF, AccessorReplacement{
                 DirectLoad, Add,
                 BC.MIB->getAnnotationAs<uint64_t>(ValueLoad, OriginIndex),
                 BC.MIB->getAnnotationAs<uint64_t>(Add, OriginIndex)});
  }

  uint64_t Fused = 0;
  uint64_t Weighted = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &Caller = BFI.second;
    if (!Caller.hasCFG() || Caller.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : Caller) {
      if (BB.getKnownExecutionCount() == 0 || BB.size() < 2)
        continue;
      for (size_t Index = 1; Index < BB.size(); ++Index) {
        MCInst &Call = *(BB.begin() + Index);
        MCInst &Move = *(BB.begin() + Index - 1);
        if (BC.MIB->isPseudo(Call) || BC.MIB->isPseudo(Move) ||
            !BC.MIB->isCall(Call) ||
            BC.MII->getName(Move.getOpcode()) != "MOV64rr" ||
            MCPlus::getNumPrimeOperands(Move) != 2 ||
            !Move.getOperand(0).isReg() || !Move.getOperand(1).isReg() ||
            StringRef(BC.MRI->getName(Move.getOperand(0).getReg())) != "RDI" ||
            StringRef(BC.MRI->getName(Move.getOperand(1).getReg())) != "RAX" ||
            !BC.MIB->hasAnnotation(Move, OriginIndex) ||
            !BC.MIB->hasAnnotation(Call, OriginIndex))
          continue;
        const MCSymbol *Target = BC.MIB->getTargetSymbol(Call);
        const BinaryFunction *Callee =
            Target ? BC.getFunctionForSymbol(Target) : nullptr;
        auto Replacement = Replacements.find(Callee);
        if (Replacement == Replacements.end())
          continue;
        const uint64_t MoveOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(Move, OriginIndex);
        const uint64_t CallOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(Call, OriginIndex);
        SmallVector<MCInst, 2> NewCode{Replacement->second.Load,
                                       Replacement->second.Add};
        BB.eraseInstructionAtIndex(Index - 1);
        auto ReplaceAt = BB.begin() + Index - 1;
        BB.replaceInstruction(ReplaceAt, NewCode.begin(), NewCode.end());
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_exact_pointer_accessor_call_inline", Caller.getAddress(),
            Replacement->second.LoadOrigin, {MoveOrigin, CallOrigin},
            BB.getKnownExecutionCount(),
            {Replacement->second.LoadOrigin, Replacement->second.AddOrigin}});
        ++Fused;
        Weighted += BB.getKnownExecutionCount();
      }
    }
  }
  BC.outs() << "BOLT-INFO: FastDyn exact accessor-call fusion replaced "
            << Fused << " call(s) (dyn count: " << Weighted << ")\n";
  return Error::success();
}

Error FastDynExactEmptyCallElimination::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact empty-call elimination is qualified only for x86");
  if (!opts::AssumeABI)
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn exact empty-call elimination requires --assume-abi");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  std::map<const BinaryFunction *, uint64_t> EmptyLeaves;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored() ||
        BF.isMultiEntry() || BF.size() != 1 ||
        BF.getPersonalityFunction() != nullptr || BF.getLSDAAddress() != 0)
      continue;
    BinaryBasicBlock &BB = *BF.begin();
    SmallVector<const MCInst *, 2> Real;
    for (const MCInst &Inst : BB)
      if (!BC.MIB->isPseudo(Inst))
        Real.push_back(&Inst);
    size_t Base = 0;
    if (!Real.empty() && BC.MII->getName(Real[0]->getOpcode()) == "ENDBR64")
      Base = 1;
    if (Real.size() != Base + 1 || !BC.MIB->isReturn(*Real[Base]) ||
        !BC.MIB->hasAnnotation(*Real[Base], OriginIndex))
      continue;
    EmptyLeaves.insert_or_assign(
        &BF, BC.MIB->getAnnotationAs<uint64_t>(*Real[Base], OriginIndex));
  }

  uint64_t Eliminated = 0;
  uint64_t Weighted = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &Caller = BFI.second;
    if (!Caller.hasCFG() || Caller.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : Caller) {
      if (BB.getKnownExecutionCount() == 0)
        continue;
      for (size_t Index = 0; Index < BB.size();) {
        MCInst &Call = *(BB.begin() + Index);
        if (BC.MIB->isPseudo(Call) || !BC.MIB->isCall(Call) ||
            !BC.MIB->hasAnnotation(Call, OriginIndex)) {
          ++Index;
          continue;
        }
        const MCSymbol *Target = BC.MIB->getTargetSymbol(Call);
        const BinaryFunction *Callee =
            Target ? BC.getFunctionForSymbol(Target) : nullptr;
        const auto Empty = EmptyLeaves.find(Callee);
        if (Empty == EmptyLeaves.end()) {
          ++Index;
          continue;
        }
        const uint64_t CallOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(Call, OriginIndex);
        BB.eraseInstructionAtIndex(Index);
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_exact_empty_call_elimination", Caller.getAddress(),
            Empty->second, {CallOrigin}, BB.getKnownExecutionCount(), {}});
        ++Eliminated;
        Weighted += BB.getKnownExecutionCount();
      }
    }
  }
  BC.outs() << "BOLT-INFO: FastDyn exact empty-call elimination removed "
            << Eliminated << " call(s) (dyn count: " << Weighted << ")\n";
  return Error::success();
}

Error FastDynMemoryTestFolding::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn memory-test folding is qualified only for x86");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  uint64_t Folded = 0;
  uint64_t Weighted = 0;
  RegAnalysis RA(BC, &BC.getBinaryFunctions(), nullptr);
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored())
      continue;
    DataflowInfoManager Info(BF, &RA, nullptr);
    LivenessAnalysis &LA = Info.getLivenessAnalysis();
    for (BinaryBasicBlock &BB : BF) {
      if (BB.size() < 2)
        continue;
      for (size_t TestIndex = BB.size() - 1; TestIndex > 0; --TestIndex) {
        auto Test = BB.begin() + TestIndex;
        auto Load = Test - 1;
        if (BC.MIB->isPseudo(*Load) || BC.MIB->isPseudo(*Test) ||
            BC.MII->getName(Load->getOpcode()) != "MOV64rm" ||
            BC.MII->getName(Test->getOpcode()) != "TEST64rr" ||
            MCPlus::getNumPrimeOperands(*Load) == 0 ||
            !Load->getOperand(0).isReg())
          continue;
        const MCPhysReg Reg = Load->getOperand(0).getReg();
        const BitVector LiveOut = LA.getLiveOut(*Test);
        if (LiveOut.anyCommon(BC.MIB->getAliases(Reg)) ||
            !BC.MIB->hasAnnotation(*Load, OriginIndex) ||
            !BC.MIB->hasAnnotation(*Test, OriginIndex))
          continue;
        const uint64_t LoadOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(*Load, OriginIndex);
        const uint64_t TestOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(*Test, OriginIndex);
        const std::optional<uint32_t> LoadOffset = BC.MIB->getOffset(*Load);
        if (!BC.MIB->replaceRegSelfTestWithMem(*Test, *Load, Reg))
          continue;
        if (LoadOffset)
          BC.MIB->setOffset(*Test, *LoadOffset);
        BC.MIB->addAnnotation(*Test, OriginIndex, LoadOrigin);
        BB.eraseInstructionAtIndex(TestIndex - 1);
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_dead_load_self_test_to_memory_test", BF.getAddress(),
            LoadOrigin, {LoadOrigin, TestOrigin},
            BB.getKnownExecutionCount()});
        ++Folded;
        Weighted += BB.getKnownExecutionCount();
      }
    }
  }
  BC.outs() << "BOLT-INFO: FastDyn memory-test folding replaced " << Folded
            << " pair(s) (dyn count: " << Weighted << ")\n";
  return Error::success();
}

Error FastDynCopyCleanup::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(inconvertibleErrorCode(),
                             "FastDyn copy cleanup is qualified only for x86");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  auto isMov64rr = [&](const MCInst &Inst) {
    const StringRef Name = BC.MII->getName(Inst.getOpcode());
    return Name == "MOV64rr" || Name == "MOV64rr_REV";
  };
  auto isMov32rr = [&](const MCInst &Inst) {
    const StringRef Name = BC.MII->getName(Inst.getOpcode());
    return Name == "MOV32rr" || Name == "MOV32rr_REV";
  };
  auto operands = [](const MCInst &Inst, MCPhysReg &Dst, MCPhysReg &Src) {
    if (MCPlus::getNumPrimeOperands(Inst) != 2 ||
        !Inst.getOperand(0).isReg() || !Inst.getOperand(1).isReg())
      return false;
    Dst = Inst.getOperand(0).getReg();
    Src = Inst.getOperand(1).getReg();
    return true;
  };
  auto key = [](MCPhysReg A, MCPhysReg B) {
    return std::pair<MCPhysReg, MCPhysReg>{std::min(A, B), std::max(A, B)};
  };
  auto is32BitGPR = [&](MCPhysReg Reg) {
    const StringRef Name = BC.MRI->getName(Reg);
    return Name == "EAX" || Name == "EBX" || Name == "ECX" ||
           Name == "EDX" || Name == "ESI" || Name == "EDI" ||
           Name == "EBP" || Name == "ESP" || Name == "R8D" ||
           Name == "R9D" || Name == "R10D" || Name == "R11D" ||
           Name == "R12D" || Name == "R13D" || Name == "R14D" ||
           Name == "R15D";
  };
  auto exact32BitDef = [&](const MCInst &Inst)
      -> std::optional<MCPhysReg> {
    const StringRef Name = BC.MII->getName(Inst.getOpcode());
    // Keep this proof deliberately bounded to unconditional opcode families
    // exercised by the firmware corpus.  Every accepted instruction has an
    // explicit first destination and an x86 32-bit GPR write, which zeros the
    // upper half of the corresponding 64-bit register.
    if (!(Name.starts_with("ADD32") || Name.starts_with("SUB32") ||
          Name.starts_with("SHL32") || Name.starts_with("SHR32") ||
          Name.starts_with("MOV32")))
      return std::nullopt;
    const MCInstrDesc &Desc = BC.MII->get(Inst.getOpcode());
    if (Desc.getNumDefs() == 0 || MCPlus::getNumPrimeOperands(Inst) == 0 ||
        !Inst.getOperand(0).isReg())
      return std::nullopt;
    const MCPhysReg Reg = Inst.getOperand(0).getReg();
    return is32BitGPR(Reg) ? std::optional<MCPhysReg>(Reg) : std::nullopt;
  };

  uint64_t Weighted = 0;
  RegAnalysis RA(BC, &BC.getBinaryFunctions(), nullptr);
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : BF) {
      std::map<std::pair<MCPhysReg, MCPhysReg>, uint64_t> EqualRegisters;
      std::optional<std::pair<MCPhysReg, uint64_t>> PreviousExact32BitDef;
      for (size_t Index = 0; Index < BB.size();) {
        auto Inst = BB.begin() + Index;
        if (BC.MIB->isPseudo(*Inst)) {
          ++Index;
          continue;
        }
        MCPhysReg Dst = 0, Src = 0;
        const std::optional<MCPhysReg> CurrentExact32BitDef =
            exact32BitDef(*Inst);
        const uint64_t CurrentOrigin =
            BC.MIB->hasAnnotation(*Inst, OriginIndex)
                ? BC.MIB->getAnnotationAs<uint64_t>(*Inst, OriginIndex)
                : 0;
        if (isMov64rr(*Inst) && operands(*Inst, Dst, Src) && Dst != Src &&
            Index + 1 < BB.size()) {
          auto Next = BB.begin() + Index + 1;
          const StringRef NextName = BC.MII->getName(Next->getOpcode());
          const std::optional<MCPlusBuilder::X86MemOperand> Memory =
              BC.MIB->evaluateX86MemoryOperand(*Next);
          const uint64_t NextOrigin =
              BC.MIB->hasAnnotation(*Next, OriginIndex)
                  ? BC.MIB->getAnnotationAs<uint64_t>(*Next, OriginIndex)
                  : 0;
          if (!BC.MIB->isPseudo(*Next) && NextName == "MOV64rm" && Memory &&
              Memory->BaseRegNum == Dst && Memory->IndexRegNum == 0 &&
              Memory->ScaleImm == 1 && Memory->DispImm == 0 &&
              Memory->DispExpr == nullptr && Memory->SegRegNum == 0 &&
              MCPlus::getNumPrimeOperands(*Next) > 0 &&
              Next->getOperand(0).isReg() &&
              Next->getOperand(0).getReg() == Dst && CurrentOrigin != 0 &&
              NextOrigin != 0 &&
              (!CopyCleanupHotOnly || BB.getKnownExecutionCount() != 0) &&
              BC.MIB->replaceMemOperandBaseReg(*Next, Dst, Src)) {
            CopyCleanups.push_back(CopyCleanupRecord{
                "x86_mov64_copy_folded_into_zero_disp_load", BF.getAddress(),
                NextOrigin, CurrentOrigin, BB.getKnownExecutionCount()});
            Weighted += BB.getKnownExecutionCount();
            BB.eraseInstructionAtIndex(Index);
            continue;
          }
        }
        if (isMov32rr(*Inst) && operands(*Inst, Dst, Src) && Dst == Src &&
            PreviousExact32BitDef && PreviousExact32BitDef->first == Dst &&
            PreviousExact32BitDef->second != 0 && CurrentOrigin != 0 &&
            (!CopyCleanupHotOnly || BB.getKnownExecutionCount() != 0)) {
          CopyCleanups.push_back(CopyCleanupRecord{
              "x86_mov32_self_after_exact_32bit_def", BF.getAddress(),
              PreviousExact32BitDef->second, CurrentOrigin,
              BB.getKnownExecutionCount()});
          if (auto Forwarding = FrameForwardings.find(CurrentOrigin);
              Forwarding != FrameForwardings.end())
            Forwarding->second.Deleted = true;
          Weighted += BB.getKnownExecutionCount();
          BB.eraseInstructionAtIndex(Index);
          continue;
        }
        if (isMov64rr(*Inst) && operands(*Inst, Dst, Src) && Dst != Src) {
          const auto Equality = key(Dst, Src);
          const auto Existing = EqualRegisters.find(Equality);
          if (Existing != EqualRegisters.end() && Existing->second != 0 &&
              (!CopyCleanupHotOnly || BB.getKnownExecutionCount() != 0) &&
              BC.MIB->hasAnnotation(*Inst, OriginIndex)) {
            const uint64_t RemovedOrigin =
                BC.MIB->getAnnotationAs<uint64_t>(*Inst, OriginIndex);
            CopyCleanups.push_back(CopyCleanupRecord{
                "x86_mov64_equal_registers", BF.getAddress(),
                Existing->second, RemovedOrigin, BB.getKnownExecutionCount()});
            Weighted += BB.getKnownExecutionCount();
            BB.eraseInstructionAtIndex(Index);
            if (CopyCleanupAdjacentOnly)
              ++Index;
            continue;
          }

          if (CopyCleanupAdjacentOnly)
            EqualRegisters.clear();
          for (auto It = EqualRegisters.begin(); It != EqualRegisters.end();) {
            if (It->first.first == Dst || It->first.second == Dst)
              It = EqualRegisters.erase(It);
            else
              ++It;
          }
          const uint64_t Origin = BC.MIB->hasAnnotation(*Inst, OriginIndex)
                                      ? BC.MIB->getAnnotationAs<uint64_t>(
                                            *Inst, OriginIndex)
                                      : 0;
          EqualRegisters.insert_or_assign(Equality, Origin);
          PreviousExact32BitDef = std::nullopt;
          ++Index;
          continue;
        }

        BitVector Clobbers(BC.MRI->getNumRegs(), false);
        RA.getInstClobberList(*Inst, Clobbers);
        if (CopyCleanupAdjacentOnly) {
          EqualRegisters.clear();
          PreviousExact32BitDef =
              CurrentExact32BitDef && CurrentOrigin
                  ? std::optional<std::pair<MCPhysReg, uint64_t>>(
                        {*CurrentExact32BitDef, CurrentOrigin})
                  : std::nullopt;
          ++Index;
          continue;
        }
        // A register equality can have originated from a FOP-replaced memory
        // load.  An intervening store may alias that original location even
        // when it clobbers neither register.  Without a proved no-alias fact,
        // do not use the equality to delete a later replacement copy.
        if (BC.MIB->mayStore(*Inst)) {
          EqualRegisters.clear();
          PreviousExact32BitDef =
              CurrentExact32BitDef && CurrentOrigin
                  ? std::optional<std::pair<MCPhysReg, uint64_t>>(
                        {*CurrentExact32BitDef, CurrentOrigin})
                  : std::nullopt;
          ++Index;
          continue;
        }
        for (auto It = EqualRegisters.begin(); It != EqualRegisters.end();) {
          if (Clobbers.test(It->first.first) ||
              Clobbers.test(It->first.second))
            It = EqualRegisters.erase(It);
          else
            ++It;
        }
        PreviousExact32BitDef =
            CurrentExact32BitDef && CurrentOrigin
                ? std::optional<std::pair<MCPhysReg, uint64_t>>(
                      {*CurrentExact32BitDef, CurrentOrigin})
                : std::nullopt;
        ++Index;
      }
    }
  }

  BC.outs() << "BOLT-INFO: FastDyn copy cleanup deleted "
            << CopyCleanups.size() << " redundant copy/copies (dyn count: "
            << Weighted << ")\n";
  return Error::success();
}

Error FastDynArithmeticStrengthReduction::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn arithmetic strength reduction is qualified only for x86");

  unsigned Lea64rOpcode = 0;
  for (unsigned Opcode = 0; Opcode != BC.MII->getNumOpcodes(); ++Opcode)
    if (BC.MII->getName(Opcode) == "LEA64r") {
      Lea64rOpcode = Opcode;
      break;
    }
  if (!Lea64rOpcode)
    return createStringError(inconvertibleErrorCode(),
                             "x86 LEA64r opcode is unavailable");

  const unsigned OriginIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  uint64_t WeightedRewrites = 0;
  auto opcodeIs = [&](const MCInst &Inst, StringRef Name) {
    const StringRef Actual = BC.MII->getName(Inst.getOpcode());
    return Actual == Name ||
           (Actual.size() == Name.size() + StringRef("_REV").size() &&
            Actual.starts_with(Name) && Actual.ends_with("_REV"));
  };
  auto twoRegOperands = [](const MCInst &Inst, MCPhysReg &Dst,
                           MCPhysReg &Src) {
    if (MCPlus::getNumPrimeOperands(Inst) != 2 ||
        !Inst.getOperand(0).isReg() ||
        !Inst.getOperand(1).isReg())
      return false;
    Dst = Inst.getOperand(0).getReg();
    Src = Inst.getOperand(1).getReg();
    return true;
  };
  auto shiftOperands = [](const MCInst &Inst, MCPhysReg &Dst,
                          MCPhysReg &Src, int64_t &Amount) {
    if (MCPlus::getNumPrimeOperands(Inst) != 3 ||
        !Inst.getOperand(0).isReg() ||
        !Inst.getOperand(1).isReg() || !Inst.getOperand(2).isImm())
      return false;
    Dst = Inst.getOperand(0).getReg();
    Src = Inst.getOperand(1).getReg();
    Amount = Inst.getOperand(2).getImm();
    return true;
  };
  auto addOperands = [](const MCInst &Inst, MCPhysReg &Dst, MCPhysReg &Lhs,
                        MCPhysReg &Rhs) {
    if (MCPlus::getNumPrimeOperands(Inst) != 3 ||
        !Inst.getOperand(0).isReg() ||
        !Inst.getOperand(1).isReg() || !Inst.getOperand(2).isReg())
      return false;
    Dst = Inst.getOperand(0).getReg();
    Lhs = Inst.getOperand(1).getReg();
    Rhs = Inst.getOperand(2).getReg();
    return true;
  };
  auto leaMulFiveOperands = [](const MCInst &Inst, MCPhysReg &Dst,
                               MCPhysReg &Input) {
    if (MCPlus::getNumPrimeOperands(Inst) != 6 ||
        !Inst.getOperand(0).isReg() || !Inst.getOperand(1).isReg() ||
        !Inst.getOperand(2).isImm() || !Inst.getOperand(3).isReg() ||
        !Inst.getOperand(4).isImm() || !Inst.getOperand(5).isReg())
      return false;
    Dst = Inst.getOperand(0).getReg();
    Input = Inst.getOperand(1).getReg();
    return Input != 0 && Inst.getOperand(2).getImm() == 4 &&
           Inst.getOperand(3).getReg() == Input &&
           Inst.getOperand(4).getImm() == 0 &&
           Inst.getOperand(5).getReg() == 0;
  };

  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : BF) {
      for (size_t InstructionIndex = 0; InstructionIndex < BB.size();) {
        auto Move = BB.begin() + InstructionIndex;
        if (BC.MIB->isPseudo(*Move)) {
          ++InstructionIndex;
          continue;
        }
        auto ShiftTwo = std::next(Move);
        auto Add = ShiftTwo == BB.end() ? BB.end() : std::next(ShiftTwo);
        auto ShiftThree = Add == BB.end() ? BB.end() : std::next(Add);
        if (ShiftTwo == BB.end() || Add == BB.end() || ShiftThree == BB.end() ||
            BC.MIB->isPseudo(*ShiftTwo) || BC.MIB->isPseudo(*Add) ||
            BC.MIB->isPseudo(*ShiftThree) ||
            !opcodeIs(*Move, "MOV64rr") ||
            !opcodeIs(*ShiftTwo, "SHL64ri") ||
            !opcodeIs(*Add, "ADD64rr") ||
            !opcodeIs(*ShiftThree, "SHL64ri")) {
          ++InstructionIndex;
          continue;
        }

        MCPhysReg MoveDst = 0, MoveSrc = 0;
        MCPhysReg ShiftTwoDst = 0, ShiftTwoSrc = 0;
        MCPhysReg AddDst = 0, AddLhs = 0, AddRhs = 0;
        MCPhysReg ShiftThreeDst = 0, ShiftThreeSrc = 0;
        int64_t ShiftTwoAmount = 0, ShiftThreeAmount = 0;
        if (!twoRegOperands(*Move, MoveDst, MoveSrc) || MoveDst == MoveSrc ||
            !shiftOperands(*ShiftTwo, ShiftTwoDst, ShiftTwoSrc,
                           ShiftTwoAmount) ||
            !addOperands(*Add, AddDst, AddLhs, AddRhs) ||
            !shiftOperands(*ShiftThree, ShiftThreeDst, ShiftThreeSrc,
                           ShiftThreeAmount) ||
            ShiftTwoAmount != 2 || ShiftThreeAmount != 3 ||
            ShiftTwoDst != MoveDst || ShiftTwoSrc != MoveDst ||
            AddDst != MoveDst || AddLhs != MoveDst || AddRhs != MoveSrc ||
            ShiftThreeDst != MoveDst || ShiftThreeSrc != MoveDst) {
          ++InstructionIndex;
          continue;
        }

        std::array<uint64_t, 4> Origins{};
        std::array<BinaryBasicBlock::iterator, 4> Pattern{
            Move, ShiftTwo, Add, ShiftThree};
        bool HasAllOrigins = true;
        for (size_t Index = 0; Index != Pattern.size(); ++Index) {
          if (!BC.MIB->hasAnnotation(*Pattern[Index], OriginIndex)) {
            HasAllOrigins = false;
            break;
          }
          Origins[Index] = BC.MIB->getAnnotationAs<uint64_t>(
              *Pattern[Index], OriginIndex);
        }
        if (!HasAllOrigins) {
          ++InstructionIndex;
          continue;
        }

        const std::optional<uint32_t> OriginalOffset = BC.MIB->getOffset(*Move);
        Move->clear();
        Move->setOpcode(Lea64rOpcode);
        Move->addOperand(MCOperand::createReg(MoveDst));
        Move->addOperand(MCOperand::createReg(MoveSrc));
        Move->addOperand(MCOperand::createImm(4));
        Move->addOperand(MCOperand::createReg(MoveSrc));
        Move->addOperand(MCOperand::createImm(0));
        Move->addOperand(MCOperand::createReg(0));
        if (OriginalOffset)
          BC.MIB->setOffset(*Move, *OriginalOffset);
        BC.MIB->addAnnotation(*Move, OriginIndex, Origins[0]);
        BB.eraseInstructionAtIndex(InstructionIndex + 1);
        BB.eraseInstructionAtIndex(InstructionIndex + 1);
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_mul40_lea_shl", BF.getAddress(), Origins[0],
            std::vector<uint64_t>(Origins.begin(), Origins.end()),
            BB.getKnownExecutionCount()});
        WeightedRewrites += BB.getKnownExecutionCount();
        InstructionIndex += 2;
      }
    }
  }

  RegAnalysis RA(BC, &BC.getBinaryFunctions(), nullptr);
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored())
      continue;
    DataflowInfoManager Info(BF, &RA, nullptr);
    LivenessAnalysis &LA = Info.getLivenessAnalysis();
    for (BinaryBasicBlock &BB : BF) {
      if (BB.size() < 2)
        continue;
      for (size_t EndIndex = BB.size() - 1; EndIndex > 0; --EndIndex) {
        auto Add = BB.begin() + EndIndex;
        auto Move = Add - 1;
        if (BC.MIB->isPseudo(*Move) || BC.MIB->isPseudo(*Add) ||
            !opcodeIs(*Move, "MOV64rr") || !opcodeIs(*Add, "ADD64rr") ||
            LA.isAlive(&*Add, BC.MIB->getFlagsReg()))
          continue;
        MCPhysReg MoveDst = 0, MoveSrc = 0;
        MCPhysReg AddDst = 0, AddLhs = 0, AddRhs = 0;
        if (!twoRegOperands(*Move, MoveDst, MoveSrc) || MoveDst == MoveSrc ||
            !addOperands(*Add, AddDst, AddLhs, AddRhs) ||
            AddDst != MoveDst || AddLhs != MoveDst)
          continue;
        const MCPhysReg EffectiveRhs = AddRhs == MoveDst ? MoveSrc : AddRhs;
        if (!BC.MIB->hasAnnotation(*Move, OriginIndex) ||
            !BC.MIB->hasAnnotation(*Add, OriginIndex))
          continue;
        const uint64_t MoveOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(*Move, OriginIndex);
        const uint64_t AddOrigin =
            BC.MIB->getAnnotationAs<uint64_t>(*Add, OriginIndex);
        const std::optional<uint32_t> OriginalOffset = BC.MIB->getOffset(*Move);
        Move->clear();
        Move->setOpcode(Lea64rOpcode);
        Move->addOperand(MCOperand::createReg(MoveDst));
        Move->addOperand(MCOperand::createReg(MoveSrc));
        Move->addOperand(MCOperand::createImm(1));
        Move->addOperand(MCOperand::createReg(EffectiveRhs));
        Move->addOperand(MCOperand::createImm(0));
        Move->addOperand(MCOperand::createReg(0));
        if (OriginalOffset)
          BC.MIB->setOffset(*Move, *OriginalOffset);
        BC.MIB->addAnnotation(*Move, OriginIndex, MoveOrigin);
        BB.eraseInstructionAtIndex(EndIndex);
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_flag_dead_mov_add_to_lea", BF.getAddress(), MoveOrigin,
            {MoveOrigin, AddOrigin}, BB.getKnownExecutionCount()});
        WeightedRewrites += BB.getKnownExecutionCount();
      }
    }
  }

  // GCC -O0 commonly materializes a 40-byte array stride as
  //
  //   lea (Index,Index,4), Tmp
  //   shl $3, Tmp
  //   mov Tmp, Index
  //   lea Symbol(%rip), Tmp
  //
  // The final LEA is a non-faulting complete overwrite of Tmp and does not
  // read it.  Retargeting the first two instructions to Index therefore
  // preserves the flags and all live register values while deleting the
  // copy.  Requiring that exact fourth instruction is deliberately narrower
  // than assuming liveness or a private stack/register convention.
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG() || !BF.isSimple() || BF.isIgnored())
      continue;
    for (BinaryBasicBlock &BB : BF) {
      for (size_t InstructionIndex = 0; InstructionIndex + 3 < BB.size();) {
        auto Lea = BB.begin() + InstructionIndex;
        auto Shift = Lea + 1;
        auto Move = Lea + 2;
        auto Clobber = Lea + 3;
        if (BC.MIB->isPseudo(*Lea) || BC.MIB->isPseudo(*Shift) ||
            BC.MIB->isPseudo(*Move) || BC.MIB->isPseudo(*Clobber) ||
            !opcodeIs(*Lea, "LEA64r") || !opcodeIs(*Shift, "SHL64ri") ||
            !opcodeIs(*Move, "MOV64rr") ||
            !opcodeIs(*Clobber, "LEA64r")) {
          ++InstructionIndex;
          continue;
        }

        MCPhysReg LeaDst = 0, LeaInput = 0;
        MCPhysReg ShiftDst = 0, ShiftSrc = 0;
        MCPhysReg MoveDst = 0, MoveSrc = 0;
        int64_t ShiftAmount = 0;
        if (!leaMulFiveOperands(*Lea, LeaDst, LeaInput) ||
            !shiftOperands(*Shift, ShiftDst, ShiftSrc, ShiftAmount) ||
            !twoRegOperands(*Move, MoveDst, MoveSrc) ||
            LeaDst == LeaInput || ShiftAmount != 3 ||
            ShiftDst != LeaDst || ShiftSrc != LeaDst ||
            MoveDst != LeaInput || MoveSrc != LeaDst) {
          ++InstructionIndex;
          continue;
        }

        bool ClobberDefinesTmp = false;
        bool ClobberUsesTmp = false;
        const BitVector &TmpAliases = BC.MIB->getAliases(LeaDst, false);
        for (const MCOperand &Operand : BC.MIB->defOperands(*Clobber))
          if (Operand.isReg() && Operand.getReg() == LeaDst)
            ClobberDefinesTmp = true;
        for (const MCOperand &Operand : BC.MIB->useOperands(*Clobber))
          if (Operand.isReg() && TmpAliases.test(Operand.getReg()))
            ClobberUsesTmp = true;
        if (!ClobberDefinesTmp || ClobberUsesTmp) {
          ++InstructionIndex;
          continue;
        }

        std::array<uint64_t, 3> Origins{};
        std::array<BinaryBasicBlock::iterator, 3> Pattern{Lea, Shift, Move};
        bool HasAllOrigins = true;
        for (size_t Index = 0; Index != Pattern.size(); ++Index) {
          if (!BC.MIB->hasAnnotation(*Pattern[Index], OriginIndex)) {
            HasAllOrigins = false;
            break;
          }
          Origins[Index] = BC.MIB->getAnnotationAs<uint64_t>(
              *Pattern[Index], OriginIndex);
        }
        if (!HasAllOrigins) {
          ++InstructionIndex;
          continue;
        }

        Lea->getOperand(0).setReg(LeaInput);
        Shift->getOperand(0).setReg(LeaInput);
        Shift->getOperand(1).setReg(LeaInput);
        BB.eraseInstructionAtIndex(InstructionIndex + 2);
        ArithmeticFusions.push_back(ArithmeticFusionRecord{
            "x86_mul40_dead_tmp_copy_coalesce", BF.getAddress(), Origins[0],
            std::vector<uint64_t>(Origins.begin(), Origins.end()),
            BB.getKnownExecutionCount()});
        WeightedRewrites += BB.getKnownExecutionCount();
        InstructionIndex += 3;
      }
    }
  }

  uint64_t Mul40Rewrites = 0;
  uint64_t MovAddRewrites = 0;
  uint64_t Mul40CopyCoalesces = 0;
  uint64_t Mul40Weighted = 0;
  uint64_t MovAddWeighted = 0;
  uint64_t Mul40CopyWeighted = 0;
  for (const ArithmeticFusionRecord &Record : ArithmeticFusions) {
    uint64_t *RewriteCount = &MovAddRewrites;
    uint64_t *WeightedCount = &MovAddWeighted;
    if (Record.Kind == "x86_mul40_lea_shl") {
      RewriteCount = &Mul40Rewrites;
      WeightedCount = &Mul40Weighted;
    } else if (Record.Kind == "x86_mul40_dead_tmp_copy_coalesce") {
      RewriteCount = &Mul40CopyCoalesces;
      WeightedCount = &Mul40CopyWeighted;
    }
    ++*RewriteCount;
    *WeightedCount += Record.KnownExecutionCount;
  }
  BC.outs() << "BOLT-INFO: FastDyn arithmetic strength reduction folded "
            << Mul40Rewrites << " multiply-by-40 and " << MovAddRewrites
            << " flag-dead mov/add idiom(s), and coalesced "
            << Mul40CopyCoalesces
            << " dead temporary copy/copies; profile-weighted counts "
            << Mul40Weighted << ", " << MovAddWeighted << ", and "
            << Mul40CopyWeighted << " (total "
            << WeightedRewrites << ")\n";
  return Error::success();
}

Error FastDynPatchContracts::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86())
    return createStringError(inconvertibleErrorCode(),
                             "FastDyn patch-contract pass is qualified only for x86");
  Expected<json::Value> RootOrError = readContracts();
  if (!RootOrError)
    return RootOrError.takeError();
  json::Object *Root = RootOrError->getAsObject();
  if (!Root)
    return createStringError(inconvertibleErrorCode(),
                             "FastDyn patch contracts must be a JSON object");
  std::optional<int64_t> Version = Root->getInteger("schema_version");
  if (!Version || *Version != 1)
    return createStringError(inconvertibleErrorCode(),
                             "FastDyn patch contract schema must be 1");
  json::Array *Bindings = Root->getArray("bindings");
  if (!Bindings)
    return createStringError(inconvertibleErrorCode(),
                             "FastDyn patch contracts require a bindings array");

  size_t ProtectedFunctions = 0;
  for (json::Value &Value : *Bindings) {
    json::Object *Binding = Value.getAsObject();
    if (!Binding)
      return createStringError(inconvertibleErrorCode(),
                               "FastDyn binding must be a JSON object");
    std::optional<StringRef> ID = Binding->getString("binding_id");
    std::optional<int64_t> OriginalPC = Binding->getInteger("original_pc");
    std::optional<int64_t> CurrentPC = Binding->getInteger("current_pc");
    std::optional<StringRef> ISA = Binding->getString("isa");
    std::optional<StringRef> Mode = Binding->getString("mode");
    std::optional<StringRef> Placement = Binding->getString("placement");
    std::optional<StringRef> Provenance = Binding->getString("provenance");
    std::optional<StringRef> ClonePolicy = Binding->getString("clone_policy");
    if (!ID || !OriginalPC || !CurrentPC || !ISA || !Mode || !Placement ||
        !Provenance || !ClonePolicy)
      return createStringError(inconvertibleErrorCode(),
                               "FastDyn binding is missing required fields");
    if (*OriginalPC < 0 || *CurrentPC < 0 || *OriginalPC != *CurrentPC ||
        *ISA != "x86_64" || *Mode != "long" || *Placement != "exact_anchor" ||
        *Provenance != "identity" || *ClonePolicy != "forbidden")
      return createStringError(inconvertibleErrorCode(),
                               "FastDyn binding %s is outside the conservative "
                               "identity-anchor policy",
                               ID->str().c_str());

    const uint64_t PC = static_cast<uint64_t>(*CurrentPC);
    BinaryFunction *BF = BC.getBinaryFunctionContainingAddress(PC);
    if (!BF || !hasInstructionAt(BC, *BF, PC))
      return createStringError(inconvertibleErrorCode(),
                               "FastDyn binding %s has no instruction at 0x%" PRIx64,
                               ID->str().c_str(), PC);
    if (!BF->isIgnored()) {
      BF->setIgnored();
      ++ProtectedFunctions;
    }
    BC.outs() << "BOLT-INFO: FastDyn protected binding " << *ID << " at 0x"
              << Twine::utohexstr(PC) << " in " << BF->getPrintName() << '\n';
  }

  BC.outs() << "BOLT-INFO: FastDyn validated " << Bindings->size()
            << " binding(s) and protected " << ProtectedFunctions
            << " function(s)\n";
  return Error::success();
}

bool FastDynFirmwareReport::isEnabled() {
  return !FirmwareReportFilename.empty();
}

Error FastDynFirmwareReport::runOnFunctions(BinaryContext &BC) {
  if (!BC.isX86() && !BC.isARM())
    return createStringError(
        inconvertibleErrorCode(),
        "FastDyn post-optimization firmware report is qualified only for x86 "
        "and ARM/Thumb");

  json::Array Functions;
  uint64_t OriginalBytes = 0;
  uint64_t EstimatedBytes = 0;
  uint64_t InstructionCount = 0;
  uint64_t InstructionsWithOriginalOffset = 0;
  uint64_t SynthesizedControlEdges = 0;
  uint64_t VerifiedSynthesizedControlEdges = 0;
  uint64_t SynthesizedCallVeneerInstructions = 0;
  uint64_t SynthesizedTransformedInstructions = 0;
  uint64_t SynthesizedCallPromotionInstructions = 0;
  uint64_t SynthesizedJumpTablePromotionInstructions = 0;
  uint64_t SynthesizedInlineDataBytes = 0;
  uint64_t UnmappedInstructions = 0;
  uint64_t ExclusiveLoadInstructions = 0;
  uint64_t ExclusiveStoreInstructions = 0;
  uint64_t ExclusiveClearInstructions = 0;
  uint64_t ClonedOriginalInstructions = 0;
  uint64_t SizeChangedFunctions = 0;
  uint64_t IgnoredFunctions = 0;
  uint64_t AddressStableFunctions = 0;
  uint64_t FirmwareModifiedFunctions = 0;
  uint64_t FirmwareModificationAttemptFunctions = 0;
  uint64_t FirmwarePublicationRejectedFunctions = 0;
  std::array<uint64_t, FirmwareModificationNames.size()>
      FirmwareModificationCounts{};
  const unsigned OriginAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  const unsigned SynthesizedFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynSynthesizedFromAnnotation);
  const unsigned TransformedFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynTransformedFromAnnotation);
  const unsigned CallPromotionFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynCallPromotionFromAnnotation);
  const unsigned JumpTablePromotionFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynJumpTablePromotionFromAnnotation);
  const unsigned SynthesizedControlEdgeFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynSynthesizedControlEdgeFromAnnotation);
  const unsigned ClonedOriginalAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynClonedOriginalAnnotation);

  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    const uint64_t OriginalSize = BF.getSize();
    const uint64_t EstimatedSize = reportFunctionSize(BC, BF);
    const uint64_t NumInstructions = BF.getNumNonPseudos();
    OriginalBytes += OriginalSize;
    EstimatedBytes += EstimatedSize;
    InstructionCount += NumInstructions;
    SizeChangedFunctions += OriginalSize != EstimatedSize;
    IgnoredFunctions += BF.isIgnored();
    AddressStableFunctions += BF.requiresAddressStableLayout();
    FirmwareModifiedFunctions += BF.isFirmwareModified();
    FirmwareModificationAttemptFunctions += BF.hasFirmwareModificationAttempt();
    FirmwarePublicationRejectedFunctions +=
        BF.getFirmwarePublicationRejectionMask() != 0;
    for (size_t Index = 0; Index < FirmwareModificationNames.size(); ++Index)
      FirmwareModificationCounts[Index] +=
          BF.hasFirmwareModification(FirmwareModificationNames[Index].first);

    json::Array Instructions;
    json::Array OriginalInstructionInventory;
    json::Array OriginalControlEdgeInventory;
    json::Array PostControlEdgeInventory;
    json::Array InlineJumpTables;
    bool ControlEdgeSetPreserved = false;
    uint64_t EstimatedOffset = 0;
    uint64_t Ordinal = 0;
    if (BF.hasCFG()) {
      auto OriginalIt = OriginalInstructions.lower_bound(BF.getAddress());
      const uint64_t OriginalEnd = BF.getAddress() + OriginalSize;
      while (OriginalIt != OriginalInstructions.end() &&
             OriginalIt->first < OriginalEnd) {
        const OriginalInstructionRecord &Record = OriginalIt->second;
        json::Object OriginalInstruction;
        OriginalInstruction["original_address"] =
            static_cast<int64_t>(Record.Address);
        OriginalInstruction["original_offset"] =
            static_cast<int64_t>(Record.Address - BF.getAddress());
        OriginalInstruction["size"] = static_cast<int64_t>(Record.Size);
        OriginalInstruction["text"] = Record.Text;
        OriginalInstruction["may_load"] = Record.MayLoad;
        OriginalInstruction["may_store"] = Record.MayStore;
        OriginalInstruction["is_call"] = Record.IsCall;
        OriginalInstruction["is_branch"] = Record.IsBranch;
        OriginalInstruction["is_unconditional_branch"] =
            Record.IsUnconditionalBranch;
        OriginalInstruction["is_return"] = Record.IsReturn;
        if (Record.BranchTargetAddress)
          OriginalInstruction["branch_target_original_address"] =
              static_cast<int64_t>(*Record.BranchTargetAddress);
        else
          OriginalInstruction["branch_target_original_address"] = nullptr;
        OriginalInstructionInventory.push_back(std::move(OriginalInstruction));
        ++OriginalIt;
      }
      std::set<std::pair<uint64_t, uint64_t>> FunctionOriginalEdges;
      for (const auto &Edge : OriginalControlEdges)
        if (Edge.first >= BF.getAddress() && Edge.first < OriginalEnd)
          FunctionOriginalEdges.insert(Edge);
      std::set<std::pair<uint64_t, uint64_t>> PostControlEdges;
      for (const BinaryBasicBlock &BB : BF) {
        if (BB.getOffset() == BinaryBasicBlock::INVALID_OFFSET)
          continue;
        for (const BinaryBasicBlock *Successor : BB.successors()) {
          if (Successor->getOffset() == BinaryBasicBlock::INVALID_OFFSET)
            continue;
          PostControlEdges.emplace(BF.getAddress() + BB.getOffset(),
                                   BF.getAddress() + Successor->getOffset());
        }
      }
      for (const auto &[Source, Target] : FunctionOriginalEdges) {
        json::Object Edge;
        Edge["source_block_original_address"] = static_cast<int64_t>(Source);
        Edge["target_block_original_address"] = static_cast<int64_t>(Target);
        OriginalControlEdgeInventory.push_back(std::move(Edge));
      }
      for (const auto &[Source, Target] : PostControlEdges) {
        json::Object Edge;
        Edge["source_block_original_address"] = static_cast<int64_t>(Source);
        Edge["target_block_original_address"] = static_cast<int64_t>(Target);
        PostControlEdgeInventory.push_back(std::move(Edge));
      }
      ControlEdgeSetPreserved = FunctionOriginalEdges == PostControlEdges;
      for (BinaryBasicBlock &BB : BF) {
        const std::optional<uint64_t> SourceBlockAddress =
            BB.getOffset() == BinaryBasicBlock::INVALID_OFFSET
                ? std::nullopt
                : std::optional<uint64_t>(BF.getAddress() + BB.getOffset());
        for (MCInst &Inst : BB) {
          if (BC.MIB->isPseudo(Inst))
            continue;
          // Give every final ARM instruction a private emission label.  The
          // pre-emission report is the last pass before lowering and therefore
          // also sees instructions synthesized by ICP, branch repair and
          // layout.  These labels do not occupy firmware bytes; they let the
          // post-link validator distinguish duplicated original identities
          // and late-relaxed encodings exactly.
          if (BC.isARM())
            BC.MIB->getOrCreateInstLabel(Inst, "FastDynFinalInst",
                                         BC.Ctx.get());
          const uint64_t Size = reportInstructionSize(BC, Inst);
          const std::optional<uint32_t> OriginalOffset = BC.MIB->getOffset(Inst);
          if (BF.requiresAddressStableLayout() && OriginalOffset)
            EstimatedOffset = *OriginalOffset;
          const bool HasExplicitOriginal =
              BC.MIB->hasAnnotation(Inst, OriginAnnotationIndex);
          const std::optional<uint64_t> CallPromotionFrom =
              BC.MIB->hasAnnotation(Inst,
                                    CallPromotionFromAnnotationIndex)
                  ? std::optional<uint64_t>(BC.MIB->getAnnotationAs<uint64_t>(
                        Inst, CallPromotionFromAnnotationIndex))
                  : std::nullopt;
          const std::optional<uint64_t> JumpTablePromotionFrom =
              BC.MIB->hasAnnotation(Inst,
                                    JumpTablePromotionFromAnnotationIndex)
                  ? std::optional<uint64_t>(BC.MIB->getAnnotationAs<uint64_t>(
                        Inst, JumpTablePromotionFromAnnotationIndex))
                  : std::nullopt;
          std::optional<uint64_t> OriginalAddress;
          if (HasExplicitOriginal)
            OriginalAddress = BC.MIB->getAnnotationAs<uint64_t>(
                Inst, OriginAnnotationIndex);
          else if (OriginalOffset && !CallPromotionFrom &&
                   !JumpTablePromotionFrom)
            OriginalAddress = BF.getAddress() + *OriginalOffset;
          const std::optional<uint64_t> SynthesizedFrom =
              BC.MIB->hasAnnotation(Inst, SynthesizedFromAnnotationIndex)
                  ? std::optional<uint64_t>(BC.MIB->getAnnotationAs<uint64_t>(
                        Inst, SynthesizedFromAnnotationIndex))
                  : std::nullopt;
          const std::optional<uint64_t> TransformedFrom =
              BC.MIB->hasAnnotation(Inst, TransformedFromAnnotationIndex)
                  ? std::optional<uint64_t>(BC.MIB->getAnnotationAs<uint64_t>(
                        Inst, TransformedFromAnnotationIndex))
                  : std::nullopt;
          InstructionsWithOriginalOffset += OriginalAddress.has_value();
          const bool IsClonedOriginal = BC.MIB->hasAnnotation(
              Inst, ClonedOriginalAnnotationIndex);
          ClonedOriginalInstructions += IsClonedOriginal;
          std::string Text;
          raw_string_ostream TextOS(Text);
          MCInst PrintableInst = Inst;
          BC.MIB->stripAnnotations(PrintableInst);
          BC.InstPrinter->printInst(&PrintableInst, 0, "", *BC.STI, TextOS);
          TextOS.flush();

          const MCInstrDesc &Desc = BC.MII->get(Inst.getOpcode());
          const bool IsExclusiveLoad =
              BC.isARM() && BC.MIB->isExclusiveLoad(Inst);
          const bool IsExclusiveStore =
              BC.isARM() && BC.MIB->isExclusiveStore(Inst);
          const bool IsExclusiveClear =
              BC.isARM() && BC.MIB->isExclusiveClear(Inst);
          ExclusiveLoadInstructions += IsExclusiveLoad;
          ExclusiveStoreInstructions += IsExclusiveStore;
          ExclusiveClearInstructions += IsExclusiveClear;
          json::Array SymbolReferences;
          for (int OperandIndex = 0,
                   OperandEnd = MCPlus::getNumPrimeOperands(Inst);
               OperandIndex != OperandEnd; ++OperandIndex) {
            const MCOperand &Operand = Inst.getOperand(OperandIndex);
            if (!Operand.isExpr())
              continue;
            const auto [Symbol, Addend] =
                BC.MIB->getTargetSymbolInfo(Operand.getExpr());
            if (!Symbol)
              continue;
            ErrorOr<uint64_t> SymbolValue = BC.getSymbolValue(*Symbol);
            if (!SymbolValue)
              continue;
            json::Object Reference;
            Reference["operand_index"] = static_cast<int64_t>(OperandIndex);
            Reference["symbol"] = Symbol->getName();
            Reference["addend"] = static_cast<int64_t>(Addend);
            Reference["target_original_address"] =
                static_cast<int64_t>(*SymbolValue + Addend);
            if (BC.MIB->isCall(Inst) || BC.MIB->isBranch(Inst))
              Reference["kind"] = "control";
            else if (Text.find("%rip") != std::string::npos)
              Reference["kind"] = "pc_relative";
            else
              Reference["kind"] = "absolute_immediate";
            SymbolReferences.push_back(std::move(Reference));
          }
          json::Object Instruction;
          Instruction["ordinal"] = static_cast<int64_t>(Ordinal++);
          Instruction["estimated_output_offset"] =
              static_cast<int64_t>(EstimatedOffset);
          Instruction["estimated_size"] = static_cast<int64_t>(Size);
          Instruction["text"] = Text;
          Instruction["symbol_references"] = std::move(SymbolReferences);
          Instruction["may_load"] = Desc.mayLoad();
          Instruction["may_store"] = Desc.mayStore();
          Instruction["is_exclusive_load"] = IsExclusiveLoad;
          Instruction["is_exclusive_store"] = IsExclusiveStore;
          Instruction["is_exclusive_clear"] = IsExclusiveClear;
          Instruction["is_call"] = BC.MIB->isCall(Inst);
          Instruction["is_branch"] = BC.MIB->isBranch(Inst);
          Instruction["is_unconditional_branch"] =
              BC.MIB->isUnconditionalBranch(Inst);
          Instruction["is_return"] = BC.MIB->isReturn(Inst);
          Instruction["source_block_original_address"] =
              SourceBlockAddress
                  ? json::Value(static_cast<int64_t>(*SourceBlockAddress))
                  : json::Value(nullptr);
          Instruction["block_known_execution_count"] =
              static_cast<int64_t>(BB.getKnownExecutionCount());
          std::optional<uint64_t> OwnedBlocksEndOffset;
          if (const InlineData *Data = BC.MIB->getInlineData(Inst)) {
            json::Object Inline;
            Inline["label"] = Data->Label->getName();
            Inline["size"] = static_cast<int64_t>(Data->Size);
            Inline["alignment"] = static_cast<int64_t>(Data->Alignment);
            const auto [Target, Addend] =
                BC.MIB->getTargetSymbolInfo(Data->Value);
            if (Target) {
              Inline["target_symbol"] = Target->getName();
              Inline["addend"] = static_cast<int64_t>(Addend);
              if (ErrorOr<uint64_t> TargetValue = BC.getSymbolValue(*Target))
                Inline["target_original_address"] =
                    static_cast<int64_t>(*TargetValue + Addend);
              else
                Inline["target_original_address"] = nullptr;
            } else {
              Inline["target_symbol"] = nullptr;
              Inline["addend"] = nullptr;
              Inline["target_original_address"] = nullptr;
            }
            Instruction["inline_data"] = std::move(Inline);
            SynthesizedInlineDataBytes += Data->Size;
          }
          if (const InlineDataBlocks *Blocks =
                  BC.MIB->getInlineDataBlocks(Inst)) {
            json::Array PoolRanges;
            uint64_t BlockCursor = EstimatedOffset + Size;
            for (const InlineDataBlock &Block : *Blocks) {
              BlockCursor += offsetToAlignment(
                  BF.getAddress() + BlockCursor,
                  Align(Block.Alignment));
              json::Object Pool;
              Pool["original_address"] =
                  static_cast<int64_t>(Block.OriginalAddress);
              Pool["estimated_output_offset"] =
                  static_cast<int64_t>(BlockCursor);
              Pool["size"] =
                  static_cast<int64_t>(Block.Bytes.size());
              Pool["alignment"] =
                  static_cast<int64_t>(Block.Alignment);
              Pool["data_start_offset"] =
                  static_cast<int64_t>(Block.DataStartOffset);
              Pool["estimated_alignment_padding"] =
                  static_cast<int64_t>(Block.EstimatedAlignmentPadding);
              Pool["alignment_fill_value"] =
                  static_cast<int64_t>(Block.AlignmentFillValue);
              Pool["alignment_fill_size"] =
                  static_cast<int64_t>(Block.AlignmentFillValueSize);
              Pool["label_count"] =
                  static_cast<int64_t>(Block.Labels.size());
              Pool["relocation_count"] =
                  static_cast<int64_t>(Block.Relocations.size());
              PoolRanges.push_back(std::move(Pool));
              BlockCursor += Block.Bytes.size();
              SynthesizedInlineDataBytes += Block.Bytes.size();
            }
            OwnedBlocksEndOffset = BlockCursor;
            Instruction["owned_literal_pool_ranges"] =
                std::move(PoolRanges);
          }
          const MCSymbol *TargetSymbol =
              BC.MIB->isBranch(Inst) ? BC.MIB->getTargetSymbol(Inst) : nullptr;
          const BinaryBasicBlock *TargetBB =
              TargetSymbol ? BF.getBasicBlockForLabel(TargetSymbol) : nullptr;
          const std::optional<uint64_t> TargetBlockAddress =
              TargetBB &&
                      TargetBB->getOffset() != BinaryBasicBlock::INVALID_OFFSET
                  ? std::optional<uint64_t>(BF.getAddress() +
                                            TargetBB->getOffset())
                  : std::nullopt;
          std::optional<uint64_t> EdgeProvenanceSource;
          if (SourceBlockAddress && TargetBlockAddress &&
              OriginalControlEdges.count(
                  {*SourceBlockAddress, *TargetBlockAddress})) {
            EdgeProvenanceSource = *SourceBlockAddress;
          } else if (TargetBlockAddress &&
                     BC.MIB->isUnconditionalBranch(Inst)) {
            // LongJmp may insert a single-predecessor trampoline between an
            // original source and target.  Prove that synthesized outgoing
            // edge by walking only a unique predecessor chain and requiring
            // the resulting exact source/target pair in the pre-pass CFG.
            // This is provenance recovery, not nearest-address matching.
            const BinaryBasicBlock *Cursor = &BB;
            DenseSet<const BinaryBasicBlock *> Seen;
            while (Cursor->pred_size() == 1 && Seen.insert(Cursor).second) {
              Cursor = *Cursor->pred_begin();
              if (Cursor->getOffset() == BinaryBasicBlock::INVALID_OFFSET)
                continue;
              const uint64_t CandidateSource =
                  BF.getAddress() + Cursor->getOffset();
              if (OriginalControlEdges.count(
                      {CandidateSource, *TargetBlockAddress})) {
                EdgeProvenanceSource = CandidateSource;
                break;
              }
            }
          }
          if (TargetBlockAddress)
            Instruction["target_block_original_address"] =
                static_cast<int64_t>(*TargetBlockAddress);
          else
            Instruction["target_block_original_address"] = nullptr;
          Instruction["control_edge_was_original"] =
              EdgeProvenanceSource.has_value();
          if (EdgeProvenanceSource)
            Instruction["edge_provenance_source_block_original_address"] =
                static_cast<int64_t>(*EdgeProvenanceSource);
          else
            Instruction["edge_provenance_source_block_original_address"] =
                nullptr;
          if (OriginalAddress) {
            Instruction["provenance_kind"] =
                IsClonedOriginal ? "cloned_original_instruction"
                                 : "original_instruction";
            Instruction["original_offset"] =
                IsClonedOriginal
                    ? json::Value(nullptr)
                    : json::Value(static_cast<int64_t>(
                          *OriginalAddress - BF.getAddress()));
            Instruction["original_address"] = static_cast<int64_t>(*OriginalAddress);
          } else {
            Instruction["original_offset"] = nullptr;
            Instruction["original_address"] = nullptr;
            if (CallPromotionFrom) {
              Instruction["provenance_kind"] =
                  "synthesized_call_promotion";
              Instruction["source_original_address"] =
                  static_cast<int64_t>(*CallPromotionFrom);
              ++SynthesizedCallPromotionInstructions;
            } else if (JumpTablePromotionFrom) {
              Instruction["provenance_kind"] =
                  "synthesized_jump_table_promotion";
              Instruction["source_original_address"] =
                  static_cast<int64_t>(*JumpTablePromotionFrom);
              ++SynthesizedJumpTablePromotionInstructions;
            } else if (TransformedFrom) {
              Instruction["provenance_kind"] =
                  "synthesized_from_original_instruction";
              Instruction["source_original_address"] =
                  static_cast<int64_t>(*TransformedFrom);
              ++SynthesizedTransformedInstructions;
            } else if (SynthesizedFrom) {
              Instruction["provenance_kind"] = "synthesized_call_veneer";
              Instruction["source_original_address"] =
                  static_cast<int64_t>(*SynthesizedFrom);
              ++SynthesizedCallVeneerInstructions;
            } else if (TargetBlockAddress &&
                       BC.MIB->isUnconditionalBranch(Inst)) {
              Instruction["provenance_kind"] = "synthesized_control_edge";
              ++SynthesizedControlEdges;
              if (EdgeProvenanceSource) {
                ++VerifiedSynthesizedControlEdges;
                if (!BC.MIB->hasAnnotation(
                        Inst, SynthesizedControlEdgeFromAnnotationIndex))
                  BC.MIB->addAnnotation(
                      Inst, SynthesizedControlEdgeFromAnnotationIndex,
                      *EdgeProvenanceSource);
              }
            } else {
              Instruction["provenance_kind"] = "unmapped";
              ++UnmappedInstructions;
            }
          }
          Instructions.push_back(std::move(Instruction));
          EstimatedOffset += Size;
          if (const InlineData *Data = BC.MIB->getInlineData(Inst))
            EstimatedOffset += Data->Size;
          if (OwnedBlocksEndOffset)
            EstimatedOffset = *OwnedBlocksEndOffset;
          if (const JumpTable *JT = BF.getJumpTable(Inst);
              JT && (JT->isThumbEmbedded() ||
                     JT->Type == JumpTable::JTT_THUMB_ABS32)) {
            json::Object Table;
            Table["dispatch_original_address"] =
                OriginalAddress ? json::Value(static_cast<int64_t>(
                                      *OriginalAddress))
                                : json::Value(nullptr);
            Table["estimated_output_offset"] =
                static_cast<int64_t>(
                    BF.requiresAddressStableLayout()
                        ? JT->getAddress() - BF.getAddress()
                        : EstimatedOffset);
            Table["original_table_address"] =
                static_cast<int64_t>(JT->getAddress());
            Table["kind"] =
                JT->Type == JumpTable::JTT_THUMB_TBB
                    ? "thumb_tbb"
                    : JT->Type == JumpTable::JTT_THUMB_TBH
                          ? "thumb_tbh"
                          : "thumb_abs32";
            Table["entry_size"] = static_cast<int64_t>(JT->EntrySize);
            Table["entry_count"] = static_cast<int64_t>(JT->Entries.size());
            Table["trailing_byte_count"] =
                static_cast<int64_t>(JT->InlineTrailingBytes.size());
            json::Array Targets;
            for (const MCSymbol *Target : JT->Entries) {
              const BinaryBasicBlock *TargetBB =
                  BF.getBasicBlockForLabel(Target);
              if (TargetBB)
                Targets.push_back(static_cast<int64_t>(
                    BF.getAddress() + TargetBB->getOffset()));
              else
                Targets.push_back(nullptr);
            }
            Table["target_block_original_addresses"] = std::move(Targets);
            InlineJumpTables.push_back(std::move(Table));
            EstimatedOffset +=
                JT->getSize() + JT->InlineTrailingBytes.size();
          }
        }
      }
    }

    json::Object Function;
    Function["name"] = BF.getPrintName();
    Function["original_address"] = static_cast<int64_t>(BF.getAddress());
    Function["original_size"] = static_cast<int64_t>(OriginalSize);
    Function["estimated_post_optimization_size"] =
        static_cast<int64_t>(EstimatedSize);
    Function["post_optimization_instruction_count"] =
        static_cast<int64_t>(NumInstructions);
    Function["known_execution_count"] =
        static_cast<int64_t>(BF.getKnownExecutionCount());
    Function["state"] = static_cast<int64_t>(BF.getState());
    Function["simple"] = BF.isSimple();
    Function["address_stable_layout"] =
        BF.requiresAddressStableLayout();
    Function["cortex_m_vector_entry"] = BF.isCortexMVectorEntry();
    Function["firmware_veneer_slot"] = BF.isFirmwareVeneerSlot();
    Function["firmware_modified"] = BF.isFirmwareModified();
    Function["firmware_modification_attempted"] =
        BF.hasFirmwareModificationAttempt();
    Function["firmware_modification_reasons"] =
        firmwareModificationReasons(BF);
    Function["firmware_publication_rejections"] =
        firmwarePublicationRejections(BF);
    Function["ignored"] = BF.isIgnored();
    Function["multi_entry"] = BF.isMultiEntry();
    Function["split"] = BF.isSplit();
    Function["injected"] = BF.isInjected();
    Function["original_instructions"] =
        std::move(OriginalInstructionInventory);
    Function["original_control_edges"] =
        std::move(OriginalControlEdgeInventory);
    Function["post_control_edges"] = std::move(PostControlEdgeInventory);
    Function["inline_jump_tables"] = std::move(InlineJumpTables);
    Function["control_edge_set_preserved"] = ControlEdgeSetPreserved;
    Function["instructions"] = std::move(Instructions);
    Functions.push_back(std::move(Function));
  }

  json::Object Summary;
  Summary["function_count"] =
      static_cast<int64_t>(BC.getBinaryFunctions().size());
  Summary["ignored_function_count"] = static_cast<int64_t>(IgnoredFunctions);
  Summary["address_stable_function_count"] =
      static_cast<int64_t>(AddressStableFunctions);
  Summary["firmware_modified_function_count"] =
      static_cast<int64_t>(FirmwareModifiedFunctions);
  Summary["firmware_modification_attempt_function_count"] =
      static_cast<int64_t>(FirmwareModificationAttemptFunctions);
  Summary["firmware_publication_rejected_function_count"] =
      static_cast<int64_t>(FirmwarePublicationRejectedFunctions);
  json::Object FirmwareModificationReasonCounts;
  for (size_t Index = 0; Index < FirmwareModificationNames.size(); ++Index)
    FirmwareModificationReasonCounts[FirmwareModificationNames[Index].second] =
        static_cast<int64_t>(FirmwareModificationCounts[Index]);
  Summary["firmware_modification_reason_counts"] =
      std::move(FirmwareModificationReasonCounts);
  Summary["size_changed_function_count"] =
      static_cast<int64_t>(SizeChangedFunctions);
  Summary["original_function_bytes"] = static_cast<int64_t>(OriginalBytes);
  Summary["estimated_post_optimization_function_bytes"] =
      static_cast<int64_t>(EstimatedBytes);
  Summary["post_optimization_instruction_count"] =
      static_cast<int64_t>(InstructionCount);
  Summary["instructions_with_original_offset"] =
      static_cast<int64_t>(InstructionsWithOriginalOffset);
  Summary["synthesized_control_edge_instruction_count"] =
      static_cast<int64_t>(SynthesizedControlEdges);
  Summary["verified_synthesized_control_edge_instruction_count"] =
      static_cast<int64_t>(VerifiedSynthesizedControlEdges);
  Summary["synthesized_call_veneer_instruction_count"] =
      static_cast<int64_t>(SynthesizedCallVeneerInstructions);
  Summary["synthesized_from_original_instruction_count"] =
      static_cast<int64_t>(SynthesizedTransformedInstructions);
  Summary["synthesized_call_promotion_instruction_count"] =
      static_cast<int64_t>(SynthesizedCallPromotionInstructions);
  Summary["synthesized_jump_table_promotion_instruction_count"] =
      static_cast<int64_t>(SynthesizedJumpTablePromotionInstructions);
  Summary["synthesized_inline_data_bytes"] =
      static_cast<int64_t>(SynthesizedInlineDataBytes);
  Summary["unmapped_instruction_count"] =
      static_cast<int64_t>(UnmappedInstructions);
  Summary["exclusive_load_instruction_count"] =
      static_cast<int64_t>(ExclusiveLoadInstructions);
  Summary["exclusive_store_instruction_count"] =
      static_cast<int64_t>(ExclusiveStoreInstructions);
  Summary["exclusive_clear_instruction_count"] =
      static_cast<int64_t>(ExclusiveClearInstructions);
  Summary["cloned_original_instruction_count"] =
      static_cast<int64_t>(ClonedOriginalInstructions);

  json::Object Root;
  Root["schema_version"] = 1;
  Root["producer"] = "BOLT FastDynFirmwareReport";
  Root["phase"] = "post-frame-optimizer-and-alloc-combiner";
  Root["target"] = BC.isARM() ? "arm_thumb" :
                     BC.isX86() ? "x86_64" : "unsupported";
  Root["target_cpu"] = BC.STI->getCPU();
  Root["target_features"] = BC.STI->getFeatureString();
  Root["target_triple"] = BC.TripleName;
  Root["summary"] = std::move(Summary);
  json::Array Forwardings;
  for (const auto &[LoadAddress, Record] : FrameForwardings) {
    json::Object Forwarding;
    Forwarding["load_original_address"] =
        static_cast<int64_t>(LoadAddress);
    Forwarding["store_original_address"] =
        static_cast<int64_t>(Record.StoreAddress);
    Forwarding["replacement_deleted"] = Record.Deleted;
    Forwardings.push_back(std::move(Forwarding));
  }
  Root["frame_forwardings"] = std::move(Forwardings);
  json::Array Fusions;
  for (const ArithmeticFusionRecord &Record : ArithmeticFusions) {
    json::Object Fusion;
    Fusion["kind"] = Record.Kind;
    Fusion["function_original_address"] =
        static_cast<int64_t>(Record.FunctionAddress);
    Fusion["replacement_original_address"] =
        static_cast<int64_t>(Record.ReplacementOriginalAddress);
    Fusion["known_execution_count"] =
        static_cast<int64_t>(Record.KnownExecutionCount);
    json::Array Consumed;
    for (uint64_t Address : Record.ConsumedOriginalAddresses)
      Consumed.push_back(static_cast<int64_t>(Address));
    Fusion["consumed_original_addresses"] = std::move(Consumed);
    json::Array Replacements;
    for (uint64_t Address : Record.ReplacementOriginalAddresses)
      Replacements.push_back(static_cast<int64_t>(Address));
    Fusion["replacement_original_addresses"] = std::move(Replacements);
    Fusions.push_back(std::move(Fusion));
  }
  Root["arithmetic_fusions"] = std::move(Fusions);
  json::Array Cleanups;
  for (const CopyCleanupRecord &Record : CopyCleanups) {
    json::Object Cleanup;
    Cleanup["kind"] = Record.Kind;
    Cleanup["function_original_address"] =
        static_cast<int64_t>(Record.FunctionAddress);
    Cleanup["retained_original_address"] =
        static_cast<int64_t>(Record.RetainedOriginalAddress);
    Cleanup["removed_original_address"] =
        static_cast<int64_t>(Record.RemovedOriginalAddress);
    Cleanup["known_execution_count"] =
        static_cast<int64_t>(Record.KnownExecutionCount);
    Cleanups.push_back(std::move(Cleanup));
  }
  Root["copy_cleanups"] = std::move(Cleanups);
  Root["functions"] = std::move(Functions);

  std::error_code EC;
  raw_fd_ostream OS(FirmwareReportFilename, EC, sys::fs::OF_None);
  if (EC)
    return createStringError(EC, "cannot write %s",
                             FirmwareReportFilename.c_str());
  OS << formatv("{0:2}\n", json::Value(std::move(Root)));
  OS.close();
  if (OS.has_error())
    return createStringError(OS.error(), "failed writing %s",
                             FirmwareReportFilename.c_str());

  BC.outs() << "BOLT-INFO: FastDyn wrote post-optimization firmware report "
            << FirmwareReportFilename << " for "
            << BC.getBinaryFunctions().size() << " function(s)\n";
  return Error::success();
}

Error llvm::bolt::recordFastDynFirmwareRelocationTranslations(
    BinaryContext &BC) {
  if (!BC.isARM())
    return Error::success();

  const std::optional<unsigned> OriginAnnotationIndex =
      BC.MIB->getAnnotationIndex(FastDynOriginalAddressAnnotation);
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    BF.clearFirmwareDataRelocationTranslations();
    if (!BF.hasCFG() || !BF.isEmitted() ||
        (!BC.HasRelocations && !BF.isFirmwareModified()))
      continue;

    for (const BinaryBasicBlock &BB : BF) {
      uint64_t NextOutput = BB.getOutputStartAddress();
      if (BC.hasIOAddressMap())
        if (std::optional<uint64_t> Linked =
                BC.getIOAddressMap().lookup(BB.getLabel()))
          NextOutput = *Linked;
      if (NextOutput < BF.getOutputAddress())
        NextOutput += BF.getOutputAddress();

      for (const MCInst &Inst : BB) {
        if (BC.MIB->isPseudo(Inst))
          continue;
        uint64_t Output = NextOutput;
        if (OriginAnnotationIndex &&
            BC.MIB->hasAnnotation(Inst, *OriginAnnotationIndex)) {
          const uint64_t Original = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, *OriginAnnotationIndex);
          if (const uint64_t Translated =
                  BF.translateInputToOutputAddress(Original))
            Output = Translated;
        }
        NextOutput = Output + reportInstructionSize(BC, Inst);
        if (const InlineData *Data = BC.MIB->getInlineData(Inst))
          NextOutput += Data->Size;
        const InlineDataBlocks *Blocks = BC.MIB->getInlineDataBlocks(Inst);
        if (!Blocks)
          continue;
        for (const InlineDataBlock &Block : *Blocks) {
          NextOutput = alignTo(NextOutput, Block.Alignment);
          for (const InlineDataBlockRelocation &Reloc : Block.Relocations) {
            const uint64_t InputAddress =
                Block.OriginalAddress + Reloc.Offset;
            const uint64_t OutputAddress = NextOutput + Reloc.Offset;
            BF.addFirmwareDataRelocationTranslation(InputAddress,
                                                    OutputAddress);
          }
          NextOutput += Block.Bytes.size();
        }
      }
    }

    // Recovered ABS32 switches keep their pointer word in the owning
    // function's constant island even when the table itself is detached.
    // That word is data, so it has no instruction-origin translation. Carry
    // its exact island label through the final linker map so the retained
    // input SHT_REL record follows the live emitted word.
    if (BC.hasIOAddressMap() && BF.hasIslandsInfo())
      for (const auto &[TableAddress, JT] : BF.jumpTables()) {
        (void)TableAddress;
        if (!JT || JT->Type != JumpTable::JTT_THUMB_ABS32 ||
            !JT->ThumbTablePointerAddress)
          continue;
        const uint64_t PointerOffset =
            JT->ThumbTablePointerAddress - BF.getAddress();
        const auto Island = BF.getIslandInfo().Offsets.find(PointerOffset);
        if (Island == BF.getIslandInfo().Offsets.end()) {
          if (opts::Verbosity >= 2)
            BC.outs() << "BOLT-DEBUG: no final island label for ABS32 pointer "
                      << formatv("{0:x} in {1}\n",
                                 JT->ThumbTablePointerAddress, BF);
          continue;
        }
        std::optional<uint64_t> Linked =
            BC.getIOAddressMap().lookup(Island->second);
        if (!Linked)
          if (BinaryData *PointerData = BC.getBinaryDataAtAddress(
                  JT->ThumbTablePointerAddress))
            if (PointerData->getOutputAddress())
              Linked = PointerData->getOutputAddress();
        if (Linked) {
          BF.addFirmwareDataRelocationTranslation(
              JT->ThumbTablePointerAddress, *Linked);
          if (opts::Verbosity >= 2)
            BC.outs() << "BOLT-DEBUG: mapped ABS32 pointer relocation source "
                      << formatv("{0:x} -> {1:x} in {2}\n",
                                 JT->ThumbTablePointerAddress, *Linked, BF);
        } else if (opts::Verbosity >= 2) {
          BC.outs() << "BOLT-DEBUG: no linked address for ABS32 pointer "
                    << formatv("{0:x} in {1}\n",
                               JT->ThumbTablePointerAddress, BF);
        }
      }
  }
  return Error::success();
}

Error llvm::bolt::finalizeFastDynFirmwareLayout(BinaryContext &BC) {
  if (!FastDynFirmwareReport::isEnabled())
    return Error::success();
  json::Array Functions;
  uint64_t InlineTableCount = 0;
  uint64_t InlineTableEntryCount = 0;
  uint64_t AddressStableFunctionCount = 0;
  uint64_t SplitFunctionCount = 0;
  const unsigned OriginAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynOriginalAddressAnnotation);
  const unsigned SynthesizedFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynSynthesizedFromAnnotation);
  const unsigned TransformedFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynTransformedFromAnnotation);
  const unsigned CallPromotionFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynCallPromotionFromAnnotation);
  const unsigned JumpTablePromotionFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynJumpTablePromotionFromAnnotation);
  const unsigned SynthesizedControlEdgeFromAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(
          FastDynSynthesizedControlEdgeFromAnnotation);
  const unsigned ClonedOriginalAnnotationIndex =
      BC.MIB->getOrCreateAnnotationIndex(FastDynClonedOriginalAnnotation);
  uint64_t SynthesizedCallVeneerInstructionCount = 0;
  uint64_t SynthesizedTransformedInstructionCount = 0;
  uint64_t SynthesizedCallPromotionInstructionCount = 0;
  uint64_t SynthesizedJumpTablePromotionInstructionCount = 0;
  uint64_t SynthesizedControlEdgeInstructionCount = 0;
  uint64_t SynthesizedInlineDataBytes = 0;
  for (auto &BFI : BC.getBinaryFunctions()) {
    BinaryFunction &BF = BFI.second;
    if (!BF.hasCFG())
      continue;
    const bool PublishedOriginal =
        BC.isARM() && !BC.HasRelocations && !BF.isFirmwareModified();
    if (!PublishedOriginal && BF.isSplit())
      ++SplitFunctionCount;
    if (BF.requiresAddressStableLayout()) {
      ++AddressStableFunctionCount;
      if (!PublishedOriginal && BF.isEmitted() &&
          BF.getOutputSize() != BF.getSize())
        return createStringError(
            inconvertibleErrorCode(),
            "address-stable function %s changed occupied size from 0x%" PRIx64
            " to 0x%" PRIx64,
            BF.getPrintName().c_str(), BF.getSize(), BF.getOutputSize());
    }
    auto AbsoluteBlockAddress = [&](const BinaryBasicBlock &BB,
                                    bool End) {
      if (PublishedOriginal)
        return BF.getAddress() + BB.getOffset() +
               (End ? BB.getOriginalSize() : 0);
      // Overwritten in-place functions retain CFG state and the size-estimator
      // stores block positions as function-relative offsets.  Prefer the
      // linker-resolved label map for block starts; it includes inline data,
      // branch relaxation and alignment that the early estimator can miss.
      uint64_t Address = End ? BB.getOutputEndAddress()
                             : BB.getOutputStartAddress();
      if (!End && BC.hasIOAddressMap())
        if (std::optional<uint64_t> Linked =
                BC.getIOAddressMap().lookup(BB.getLabel()))
          Address = *Linked;
      // Non-relocation-mode labels are function-relative, including the
      // perfectly valid zero offset of the first block. Do not use zero as an
      // unresolved sentinel here: doing so previously reported synthesized
      // instructions in the first block at addresses 0x4, 0x6, ... instead of
      // at their firmware PCs.
      return Address < BF.getOutputAddress() ? BF.getOutputAddress() + Address
                                             : Address;
    };
    json::Array Blocks;
    for (const BinaryBasicBlock &BB : BF) {
      json::Object Block;
      Block["original_address"] =
          BB.getOffset() == BinaryBasicBlock::INVALID_OFFSET
              ? json::Value(nullptr)
              : json::Value(static_cast<int64_t>(BF.getAddress() +
                                                 BB.getOffset()));
      Block["output_start_address"] =
          static_cast<int64_t>(
              AbsoluteBlockAddress(BB, /*End=*/false));
      Block["output_end_address"] =
          static_cast<int64_t>(
              AbsoluteBlockAddress(BB, /*End=*/true));
      Blocks.push_back(std::move(Block));
    }

    json::Array InstructionOrigins;
    json::Array SynthesizedInlineData;
    json::Array OwnedLiteralPools;
    if (PublishedOriginal) {
      auto OriginalIt = OriginalInstructions.lower_bound(BF.getAddress());
      const uint64_t OriginalEnd = BF.getAddress() + BF.getSize();
      while (OriginalIt != OriginalInstructions.end() &&
             OriginalIt->first < OriginalEnd) {
        const OriginalInstructionRecord &Record = OriginalIt->second;
        json::Object Origin;
        Origin["origin_kind"] = "original_instruction";
        Origin["original_address"] = static_cast<int64_t>(Record.Address);
        Origin["output_address"] = static_cast<int64_t>(Record.Address);
        Origin["output_size"] = static_cast<int64_t>(Record.Size);
        Origin["may_load"] = Record.MayLoad;
        Origin["may_store"] = Record.MayStore;
        Origin["is_call"] = Record.IsCall;
        Origin["is_branch"] = Record.IsBranch;
        Origin["is_return"] = Record.IsReturn;
        InstructionOrigins.push_back(std::move(Origin));
        ++OriginalIt;
      }
    }
    DenseMap<const MCSymbol *, uint64_t> OwnedLiteralLabelOutputs;
    DenseMap<const MCSymbol *, uint64_t> OwnedLiteralLabelOriginals;
    for (const BinaryBasicBlock &BB : BF) {
      const FragmentNum Fragment = BB.getFragmentNum();
      const FunctionFragment &FF = BF.getLayout().getFragment(Fragment);
      const uint64_t FragmentOutputAddress =
          Fragment == FragmentNum::main() ? BF.getOutputAddress()
                                          : FF.getAddress();
      const uint64_t FragmentOutputSize =
          Fragment == FragmentNum::main() ? BF.getOutputSize()
                                          : FF.getImageSize();
      uint64_t NextOutput = AbsoluteBlockAddress(BB, /*End=*/false);
      for (const MCInst &Inst : BB) {
        if (BC.MIB->isPseudo(Inst))
          continue;
        // Candidate CFG state may no longer describe a function whose
        // publication was rejected.  The writer retained its immutable input
        // bytes, so report those exact pre-pass identities and effects from
        // OriginalInstructions above instead of mixing candidate and
        // published origins.
        if (PublishedOriginal)
          continue;
        uint64_t Size = reportInstructionSize(BC, Inst);
        const bool HasOriginal =
            BC.MIB->hasAnnotation(Inst, OriginAnnotationIndex);
        const bool HasSynthesizedFrom =
            BC.MIB->hasAnnotation(Inst, SynthesizedFromAnnotationIndex);
        const bool HasTransformedFrom =
            BC.MIB->hasAnnotation(Inst, TransformedFromAnnotationIndex);
        const bool HasCallPromotionFrom = BC.MIB->hasAnnotation(
            Inst, CallPromotionFromAnnotationIndex);
        const bool HasJumpTablePromotionFrom = BC.MIB->hasAnnotation(
            Inst, JumpTablePromotionFromAnnotationIndex);
        const bool HasSynthesizedControlEdgeFrom = BC.MIB->hasAnnotation(
            Inst, SynthesizedControlEdgeFromAnnotationIndex);
        const bool IsClonedOriginal = BC.MIB->hasAnnotation(
            Inst, ClonedOriginalAnnotationIndex);
        uint64_t Output = NextOutput;
        if (!PublishedOriginal && BF.isEmitted()) {
          const MCSymbol *InstructionLabel = BC.MIB->getInstLabel(Inst);
          const std::optional<uint64_t> Linked =
              InstructionLabel && BC.hasIOAddressMap()
                  ? BC.getIOAddressMap().lookup(InstructionLabel)
                  : std::nullopt;
          // InstLowering can replace a late pseudo after the report pass has
          // attached labels. Such a replacement has no private label, but its
          // block start and the byte-accurate cursor from every preceding
          // linked instruction still determine its exact PC. Prefer the
          // private linker label whenever it survives lowering.
          if (Linked) {
            Output = *Linked;
            if (Output < FragmentOutputAddress)
              Output += FragmentOutputAddress;
          }
          const uint8_t *ImageAddress = reinterpret_cast<const uint8_t *>(
              FF.getImageAddress());
          uint64_t ImageSize = FF.getImageSize();
          if (BC.HasRelocations) {
            ErrorOr<BinarySection &> CodeSection =
                BF.getCodeSection(Fragment);
            if (!CodeSection || !CodeSection->getAllocAddress() ||
                FragmentOutputAddress < CodeSection->getOutputAddress() ||
                FragmentOutputAddress + FragmentOutputSize >
                    CodeSection->getOutputAddress() +
                        CodeSection->getOutputSize())
              return createStringError(
                  inconvertibleErrorCode(),
                  "relocated ARM fragment %u of function %s is outside its "
                  "linked code section",
                  Fragment.get(), BF.getPrintName().c_str());
            const uint64_t SectionOffset =
                FragmentOutputAddress - CodeSection->getOutputAddress();
            ImageAddress = reinterpret_cast<const uint8_t *>(
                               CodeSection->getAllocAddress()) +
                           SectionOffset;
            ImageSize = FragmentOutputSize;
          }
          if (!ImageAddress || !ImageSize ||
              Output < FragmentOutputAddress ||
              Output - FragmentOutputAddress >= ImageSize)
            return createStringError(
                inconvertibleErrorCode(),
                "linked ARM instruction in fragment %u of %s has invalid "
                "output address 0x%" PRIx64,
                Fragment.get(), BF.getPrintName().c_str(), Output);
          const uint64_t ImageOffset = Output - FragmentOutputAddress;
          ArrayRef<uint8_t> Image(ImageAddress, ImageSize);
          if (Image.size() - ImageOffset < 2)
            return createStringError(
                inconvertibleErrorCode(),
                "truncated linked Thumb instruction in %s at 0x%" PRIx64,
                BF.getPrintName().c_str(), Output);
          const uint16_t FirstHalfword =
              BC.AsmInfo->isLittleEndian()
                  ? support::endian::read16le(Image.data() + ImageOffset)
                  : support::endian::read16be(Image.data() + ImageOffset);
          const uint16_t Prefix = FirstHalfword & 0xf800;
          Size = Prefix == 0xe800 || Prefix == 0xf000 || Prefix == 0xf800
                     ? 4
                     : 2;
          if (Image.size() - ImageOffset < Size)
            return createStringError(
                inconvertibleErrorCode(),
                "truncated linked Thumb-%" PRIu64 " instruction in %s at "
                "0x%" PRIx64,
                Size * 8, BF.getPrintName().c_str(), Output);
        }
        if (!HasOriginal && !HasSynthesizedFrom && !HasTransformedFrom &&
            !HasCallPromotionFrom &&
            !HasJumpTablePromotionFrom &&
            !HasSynthesizedControlEdgeFrom) {
          NextOutput = Output + Size;
          continue;
        }

        json::Object Origin;
        if (HasOriginal) {
          const uint64_t Original = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, OriginAnnotationIndex);
          // We are walking the final emitted block/instruction order from a
          // linker-resolved block label. That cursor is the unambiguous
          // output PC even when ICP retains the original indirect call and
          // also creates a direct-call clone with the same input identity.
          // translateInputToOutputAddress() is intentionally one-to-one and
          // cannot distinguish those duplicates.
          Output = PublishedOriginal ? Original : Output;
          if (BF.isEmitted() && !Output)
            return createStringError(
                inconvertibleErrorCode(),
                "cannot translate original ARM instruction 0x%" PRIx64
                " in %s after final relaxation",
                Original, BF.getPrintName().c_str());
          if (!IsClonedOriginal && BF.isEmitted() &&
              BF.requiresAddressStableLayout()) {
            const uint64_t Expected =
                BF.getOutputAddress() + (Original - BF.getAddress());
            if (Output != Expected)
              return createStringError(
                  inconvertibleErrorCode(),
                  "address-stable instruction 0x%" PRIx64
                  " in %s moved to 0x%" PRIx64 " instead of 0x%" PRIx64,
                  Original, BF.getPrintName().c_str(), Output, Expected);
          }
          Origin["origin_kind"] =
              IsClonedOriginal ? "cloned_original_instruction"
                               : "original_instruction";
          Origin["original_address"] = static_cast<int64_t>(Original);
        } else if (HasCallPromotionFrom) {
          const uint64_t SourceOriginal = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, CallPromotionFromAnnotationIndex);
          Origin["origin_kind"] = "synthesized_call_promotion";
          Origin["source_original_address"] =
              static_cast<int64_t>(SourceOriginal);
          ++SynthesizedCallPromotionInstructionCount;
        } else if (HasJumpTablePromotionFrom) {
          const uint64_t SourceOriginal = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, JumpTablePromotionFromAnnotationIndex);
          Origin["origin_kind"] = "synthesized_jump_table_promotion";
          Origin["source_original_address"] =
              static_cast<int64_t>(SourceOriginal);
          ++SynthesizedJumpTablePromotionInstructionCount;
        } else if (HasTransformedFrom) {
          const uint64_t SourceOriginal = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, TransformedFromAnnotationIndex);
          Origin["origin_kind"] =
              "synthesized_from_original_instruction";
          Origin["source_original_address"] =
              static_cast<int64_t>(SourceOriginal);
          ++SynthesizedTransformedInstructionCount;
        } else if (HasSynthesizedFrom) {
          const uint64_t SourceOriginal = BC.MIB->getAnnotationAs<uint64_t>(
              Inst, SynthesizedFromAnnotationIndex);
          Origin["origin_kind"] = "synthesized_call_veneer";
          Origin["source_original_address"] =
              static_cast<int64_t>(SourceOriginal);
          ++SynthesizedCallVeneerInstructionCount;
        } else {
          const uint64_t SourceOriginalBlock =
              BC.MIB->getAnnotationAs<uint64_t>(
                  Inst, SynthesizedControlEdgeFromAnnotationIndex);
          Origin["origin_kind"] = "synthesized_control_edge";
          Origin["source_original_block_address"] =
              static_cast<int64_t>(SourceOriginalBlock);
          ++SynthesizedControlEdgeInstructionCount;
        }
        Origin["output_address"] = static_cast<int64_t>(Output);
        Origin["output_size"] = static_cast<int64_t>(Size);
        Origin["may_load"] = BC.MII->get(Inst.getOpcode()).mayLoad();
        Origin["may_store"] = BC.MII->get(Inst.getOpcode()).mayStore();
        Origin["is_call"] = BC.MIB->isCall(Inst);
        Origin["is_branch"] = BC.MIB->isBranch(Inst);
        Origin["is_return"] = BC.MIB->isReturn(Inst);
        InstructionOrigins.push_back(std::move(Origin));
        NextOutput = Output + Size;
        if (const InlineData *Data = BC.MIB->getInlineData(Inst)) {
          // BinaryEmitter owns this label and emits it immediately after the
          // annotated instruction. JITLink's IO map intentionally contains
          // address-translation labels, not every local data label, so derive
          // the exact address from the linker-resolved owner instruction.
          // If a future linker does expose the label, require agreement.
          const uint64_t LiteralOutput = NextOutput;
          if (BC.hasIOAddressMap())
            if (std::optional<uint64_t> LinkedLiteral =
                    BC.getIOAddressMap().lookup(Data->Label)) {
              uint64_t Linked = *LinkedLiteral;
              if (Linked < BF.getOutputAddress())
                Linked += BF.getOutputAddress();
              if (Linked != LiteralOutput)
                return createStringError(
                    inconvertibleErrorCode(),
                    "synthesized inline data in %s was linked at 0x%" PRIx64
                    " instead of owner end 0x%" PRIx64,
                    BF.getPrintName().c_str(), Linked, LiteralOutput);
            }
          if (!isAligned(Align(Data->Alignment), LiteralOutput))
            return createStringError(
                inconvertibleErrorCode(),
                "synthesized inline data in %s is misaligned at 0x%" PRIx64,
                BF.getPrintName().c_str(), LiteralOutput);

          const auto [Target, Addend] =
              BC.MIB->getTargetSymbolInfo(Data->Value);
          if (!Target)
            return createStringError(
                inconvertibleErrorCode(),
                "synthesized inline data in %s has no symbolic target",
                BF.getPrintName().c_str());
          uint64_t TargetOutput = 0;
          if (std::optional<uint64_t> LinkedTarget =
                  BC.getIOAddressMap().lookup(Target)) {
            TargetOutput = *LinkedTarget;
            if (TargetOutput < BF.getOutputAddress())
              TargetOutput += BF.getOutputAddress();
          } else if (ErrorOr<uint64_t> TargetValue =
                         BC.getSymbolValue(*Target)) {
            TargetOutput = *TargetValue;
          } else {
            return createStringError(
                inconvertibleErrorCode(),
                "synthesized inline-data target %s has no final address",
                Target->getName().str().c_str());
          }
          // The layout manifest records the scalar actually stored in the
          // image, not merely the canonical instruction byte address. ELF32
          // ARM contributes bit zero through a Thumb STT_FUNC symbol even
          // when the MC expression has no explicit arithmetic addend.
          if (BC.isARM() && BC.getFunctionForSymbol(Target))
            TargetOutput |= 1;

          json::Object DataRecord;
          DataRecord["owner_instruction_output_address"] =
              static_cast<int64_t>(Output);
          DataRecord["output_address"] =
              static_cast<int64_t>(LiteralOutput);
          DataRecord["output_address_source"] = "owner_instruction_end";
          DataRecord["size"] = static_cast<int64_t>(Data->Size);
          DataRecord["alignment"] =
              static_cast<int64_t>(Data->Alignment);
          DataRecord["target_symbol"] = Target->getName();
          DataRecord["target_output_address"] =
              static_cast<int64_t>(TargetOutput + Addend);
          if (HasSynthesizedFrom)
            DataRecord["source_original_address"] = static_cast<int64_t>(
                BC.MIB->getAnnotationAs<uint64_t>(
                    Inst, SynthesizedFromAnnotationIndex));
          else
            DataRecord["source_original_address"] = nullptr;
          SynthesizedInlineData.push_back(std::move(DataRecord));
          SynthesizedInlineDataBytes += Data->Size;
          NextOutput += Data->Size;
        }
        if (const InlineDataBlocks *DataBlocks =
                BC.MIB->getInlineDataBlocks(Inst)) {
          if (!BC.MIB->isUnconditionalBranch(Inst) &&
              !BC.MIB->isReturn(Inst))
            return createStringError(
                inconvertibleErrorCode(),
                "instruction-owned literal pool in %s has no proven "
                "non-fallthrough owner",
                BF.getPrintName().c_str());
          for (const InlineDataBlock &Block : *DataBlocks) {
            NextOutput = alignTo(NextOutput, Block.Alignment);
            const uint64_t BlockOutput = NextOutput;
            json::Array Labels;
            for (const auto &[Offset, Label] : Block.Labels) {
              if (Offset > Block.Bytes.size())
                return createStringError(
                    inconvertibleErrorCode(),
                    "instruction-owned literal label in %s lies outside its "
                    "block",
                    BF.getPrintName().c_str());
              const uint64_t LabelOutput = BlockOutput + Offset;
              if (!OwnedLiteralLabelOutputs.try_emplace(Label, LabelOutput)
                       .second)
                return createStringError(
                    inconvertibleErrorCode(),
                    "instruction-owned literal label in %s is duplicated",
                    BF.getPrintName().c_str());
              OwnedLiteralLabelOriginals.try_emplace(
                  Label, Block.OriginalAddress + Offset);
              if (BC.hasIOAddressMap())
                if (std::optional<uint64_t> Linked =
                        BC.getIOAddressMap().lookup(Label)) {
                  uint64_t LinkedOutput = *Linked;
                  if (LinkedOutput < BF.getOutputAddress())
                    LinkedOutput += BF.getOutputAddress();
                  if (LinkedOutput != LabelOutput)
                    return createStringError(
                        inconvertibleErrorCode(),
                        "instruction-owned literal label in %s linked at "
                        "0x%" PRIx64 " instead of 0x%" PRIx64,
                        BF.getPrintName().c_str(), LinkedOutput, LabelOutput);
                }
              json::Object LabelRecord;
              LabelRecord["symbol"] = Label->getName();
              LabelRecord["block_offset"] =
                  static_cast<int64_t>(Offset);
              LabelRecord["output_address"] =
                  static_cast<int64_t>(LabelOutput);
              Labels.push_back(std::move(LabelRecord));
            }
            json::Object Pool;
            Pool["owner_instruction_output_address"] =
                static_cast<int64_t>(Output);
            Pool["original_address"] =
                static_cast<int64_t>(Block.OriginalAddress);
            Pool["output_address"] =
                static_cast<int64_t>(BlockOutput);
            Pool["size"] =
                static_cast<int64_t>(Block.Bytes.size());
            Pool["alignment"] =
                static_cast<int64_t>(Block.Alignment);
            Pool["data_start_offset"] =
                static_cast<int64_t>(Block.DataStartOffset);
            Pool["estimated_alignment_padding"] =
                static_cast<int64_t>(Block.EstimatedAlignmentPadding);
            Pool["alignment_fill_value"] =
                static_cast<int64_t>(Block.AlignmentFillValue);
            Pool["alignment_fill_size"] =
                static_cast<int64_t>(Block.AlignmentFillValueSize);
            Pool["data_output_address"] = static_cast<int64_t>(
                BlockOutput + Block.DataStartOffset);
            Pool["relocation_count"] =
                static_cast<int64_t>(Block.Relocations.size());
            Pool["labels"] = std::move(Labels);
            OwnedLiteralPools.push_back(std::move(Pool));
            SynthesizedInlineDataBytes += Block.Bytes.size();
            NextOutput += Block.Bytes.size();
          }
        }
      }
    }

    // Recheck every rebound Thumb literal load and ADR against the final
    // linked addresses. Thumb-1 uses an unsigned 0..1020 byte word-scaled
    // offset, while Thumb-2 LDR.W uses a signed-magnitude 12-bit byte offset.
    // These are final linked addresses, not pre-link block-order estimates.
    for (const BinaryBasicBlock &BB : BF)
      for (const MCInst &Inst : BB) {
        const StringRef OpcodeName = BC.MII->getName(Inst.getOpcode());
        if ((OpcodeName != StringRef("tLDRpci") &&
             OpcodeName != StringRef("t2LDRpci") &&
             OpcodeName != StringRef("tADR")) ||
            Inst.getNumOperands() < 2 || !Inst.getOperand(1).isExpr())
          continue;
        const auto [Target, Addend] =
            BC.MIB->getTargetSymbolInfo(Inst.getOperand(1).getExpr());
        auto TargetIt = OwnedLiteralLabelOutputs.find(Target);
        if (TargetIt == OwnedLiteralLabelOutputs.end())
          continue;
        if (!BC.MIB->hasAnnotation(Inst, OriginAnnotationIndex))
          return createStringError(
              inconvertibleErrorCode(),
              "rebound Thumb PC-relative reference in %s lost its original "
              "identity",
              BF.getPrintName().c_str());
        const uint64_t Original = BC.MIB->getAnnotationAs<uint64_t>(
            Inst, OriginAnnotationIndex);
        const uint64_t LoadOutput =
            BF.translateInputToOutputAddress(Original);
        const uint64_t LiteralOutput = TargetIt->second + Addend;
        if (OpcodeName == StringRef("tADR") ||
            !AllowLiteralAddressChanges) {
          auto OriginalIt = OwnedLiteralLabelOriginals.find(Target);
          if (OriginalIt == OwnedLiteralLabelOriginals.end() ||
              LiteralOutput != OriginalIt->second + Addend)
            return createStringError(
                inconvertibleErrorCode(),
                "rebound Thumb PC-relative reference in %s changed the "
                "observable address "
                "from 0x%" PRIx64 " to 0x%" PRIx64,
                BF.getPrintName().c_str(),
                OriginalIt == OwnedLiteralLabelOriginals.end()
                    ? uint64_t(0)
                    : OriginalIt->second + Addend,
                LiteralOutput);
        }
        const uint64_t PC = alignDown(LoadOutput + 4, uint64_t(4));
        const int64_t Delta = int64_t(LiteralOutput) - int64_t(PC);
        const bool InRange = OpcodeName == StringRef("t2LDRpci")
                                 ? Delta >= -4095 && Delta <= 4095
                                 : Delta >= 0 && Delta <= 1020 &&
                                       !(Delta & 3);
        if (!LoadOutput || !InRange)
          return createStringError(
              inconvertibleErrorCode(),
              "rebound Thumb PC-relative reference in %s is out of range: "
              "instruction=0x%" PRIx64 ", pc=0x%" PRIx64
              ", target=0x%" PRIx64,
              BF.getPrintName().c_str(), LoadOutput, PC, LiteralOutput);
      }

    json::Array Tables;
    for (const BinaryBasicBlock &BB : BF) {
      for (const MCInst &Inst : BB) {
        const JumpTable *JT = BF.getJumpTable(Inst);
        if (!JT ||
            (!JT->isThumbEmbedded() &&
             JT->Type != JumpTable::JTT_THUMB_ABS32))
          continue;
        if (!BC.hasIOAddressMap() && BF.isEmitted())
          return createStringError(
              inconvertibleErrorCode(),
              "FastDyn final firmware layout is missing the precise address "
              "map required by an embedded Thumb jump table");
        if (!BC.MIB->hasAnnotation(Inst, OriginAnnotationIndex))
          return createStringError(
              inconvertibleErrorCode(),
              "embedded Thumb jump-table dispatch in %s lost its original "
              "instruction identity",
              BF.getPrintName().c_str());
        const uint64_t DispatchOriginal = BC.MIB->getAnnotationAs<uint64_t>(
            Inst, OriginAnnotationIndex);
        const uint64_t DispatchOutput =
            PublishedOriginal
                ? DispatchOriginal
                : BF.translateInputToOutputAddress(DispatchOriginal);
        if (!DispatchOutput)
          return createStringError(
              inconvertibleErrorCode(),
              "cannot translate embedded Thumb jump-table dispatch 0x%" PRIx64,
              DispatchOriginal);

        json::Array TargetOriginals;
        json::Array TargetOutputs;
        for (const MCSymbol *Target : JT->Entries) {
          const BinaryBasicBlock *TargetBB = BF.getBasicBlockForLabel(Target);
          if (!TargetBB ||
              (!PublishedOriginal && !TargetBB->getOutputStartAddress()))
            return createStringError(
                inconvertibleErrorCode(),
                "embedded Thumb jump table in %s has an unresolved final target",
                BF.getPrintName().c_str());
          TargetOriginals.push_back(static_cast<int64_t>(
              BF.getAddress() + TargetBB->getOffset()));
          uint64_t TargetOutput = BF.getAddress() + TargetBB->getOffset();
          if (!PublishedOriginal && BC.hasIOAddressMap()) {
            TargetOutput = AbsoluteBlockAddress(*TargetBB, /*End=*/false);
            if (std::optional<uint64_t> LinkedTarget =
                    BC.getIOAddressMap().lookup(Target)) {
              TargetOutput = *LinkedTarget;
              if (TargetOutput < BF.getOutputAddress())
                TargetOutput += BF.getOutputAddress();
            }
          }
          TargetOutputs.push_back(static_cast<int64_t>(TargetOutput));
        }
        json::Object Table;
        Table["kind"] =
            JT->Type == JumpTable::JTT_THUMB_TBB
                ? "thumb_tbb"
                : JT->Type == JumpTable::JTT_THUMB_TBH
                      ? "thumb_tbh"
                      : "thumb_abs32";
        Table["dispatch_original_address"] =
            static_cast<int64_t>(DispatchOriginal);
        Table["dispatch_output_address"] =
            static_cast<int64_t>(DispatchOutput);
        uint64_t BranchBaseOutput =
            JT->isThumbInline()
                ? (PublishedOriginal
                       ? JT->ThumbBranchBaseAddress
                       : DispatchOutput + reportInstructionSize(BC, Inst))
                : 0;
        uint64_t TableOutput = JT->getAddress();
        if (!PublishedOriginal && JT->isThumbTableAtDispatch() &&
            BF.isEmitted())
          TableOutput = BranchBaseOutput;
        if (!PublishedOriginal && BC.hasIOAddressMap()) {
          if (JT->ThumbBranchBaseLabel)
            if (std::optional<uint64_t> LinkedBase =
                    BC.getIOAddressMap().lookup(JT->ThumbBranchBaseLabel))
              BranchBaseOutput = *LinkedBase;
          if (std::optional<uint64_t> LinkedTable =
                  BC.getIOAddressMap().lookup(JT->getFirstLabel()))
            TableOutput = *LinkedTable;
          if (JT->Type == JumpTable::JTT_THUMB_ABS32 &&
              JT->ThumbTableInFunctionIsland && BF.hasIslandsInfo()) {
            const uint64_t TableOffset = JT->getAddress() - BF.getAddress();
            const auto Island = BF.getIslandInfo().Offsets.find(TableOffset);
            if (Island != BF.getIslandInfo().Offsets.end())
              if (std::optional<uint64_t> LinkedIsland =
                      BC.getIOAddressMap().lookup(Island->second))
                TableOutput = *LinkedIsland;
          }
        }
        Table["branch_base_original_address"] =
            JT->isThumbInline()
                ? json::Value(static_cast<int64_t>(JT->ThumbBranchBaseAddress))
                : json::Value(nullptr);
        Table["branch_base_output_address"] =
            JT->isThumbInline()
                ? json::Value(static_cast<int64_t>(BranchBaseOutput))
                : json::Value(nullptr);
        Table["original_table_address"] =
            static_cast<int64_t>(JT->getAddress());
        Table["table_output_address"] = static_cast<int64_t>(TableOutput);
        uint64_t TablePointerOutput = JT->ThumbTablePointerAddress;
        if (JT->Type == JumpTable::JTT_THUMB_ABS32 &&
            JT->ThumbTablePointerAddress) {
          ArrayRef<uint64_t> PointerTranslations =
              BF.translateFirmwareDataRelocationAddresses(
                  JT->ThumbTablePointerAddress);
          if (PointerTranslations.size() == 1)
            TablePointerOutput = PointerTranslations.front();
        }
        Table["table_pointer_original_address"] =
            JT->Type == JumpTable::JTT_THUMB_ABS32 &&
                    JT->ThumbTablePointerAddress
                ? json::Value(static_cast<int64_t>(
                      JT->ThumbTablePointerAddress))
                : json::Value(nullptr);
        Table["table_pointer_output_address"] =
            JT->Type == JumpTable::JTT_THUMB_ABS32 &&
                    JT->ThumbTablePointerAddress
                ? json::Value(static_cast<int64_t>(TablePointerOutput))
                : json::Value(nullptr);
        Table["entry_size"] = static_cast<int64_t>(JT->EntrySize);
        Table["entry_count"] = static_cast<int64_t>(JT->Entries.size());
        Table["reference_relocations_complete"] =
            JT->Type == JumpTable::JTT_THUMB_ABS32
                ? json::Value(JT->ThumbReferenceRelocationsComplete)
                : json::Value(nullptr);
        Table["trailing_byte_count"] =
            static_cast<int64_t>(JT->InlineTrailingBytes.size());
        Table["target_block_original_addresses"] =
            std::move(TargetOriginals);
        Table["target_block_output_addresses"] = std::move(TargetOutputs);
        Tables.push_back(std::move(Table));
        ++InlineTableCount;
        InlineTableEntryCount += JT->Entries.size();
      }
    }
    if (Tables.empty() && InstructionOrigins.empty())
      continue;
    json::Object Function;
    Function["name"] = BF.getPrintName();
    Function["original_address"] = static_cast<int64_t>(BF.getAddress());
    Function["original_size"] = static_cast<int64_t>(BF.getSize());
    Function["output_address"] =
        static_cast<int64_t>(PublishedOriginal ? BF.getAddress()
                                              : BF.getOutputAddress());
    Function["output_size"] = static_cast<int64_t>(
        PublishedOriginal ? BF.getSize() : BF.getOutputSize());
    Function["firmware_modified"] = BF.isFirmwareModified();
    Function["firmware_modification_attempted"] =
        BF.hasFirmwareModificationAttempt();
    Function["firmware_modification_reasons"] =
        firmwareModificationReasons(BF);
    Function["firmware_publication_rejections"] =
        firmwarePublicationRejections(BF);
    Function["address_stable_layout"] =
        BF.requiresAddressStableLayout();
    Function["cortex_m_vector_entry"] = BF.isCortexMVectorEntry();
    Function["firmware_veneer_slot"] = BF.isFirmwareVeneerSlot();
    json::Array Fragments;
    if (PublishedOriginal) {
      json::Object Fragment;
      Fragment["fragment_number"] = 0;
      Fragment["kind"] = "main";
      Fragment["output_address"] = static_cast<int64_t>(BF.getAddress());
      Fragment["output_size"] = static_cast<int64_t>(BF.getSize());
      Fragment["block_count"] =
          static_cast<int64_t>(BF.getLayout().block_size());
      Fragments.push_back(std::move(Fragment));
    } else {
      for (const FunctionFragment &FF : BF.getLayout().fragments()) {
        if (FF.empty())
          continue;
        const FragmentNum Num = FF.getFragmentNum();
        const uint64_t OutputAddress =
            Num == FragmentNum::main() ? BF.getOutputAddress()
                                       : FF.getAddress();
        const uint64_t OutputSize =
            Num == FragmentNum::main() ? BF.getOutputSize()
                                       : FF.getImageSize();
        json::Object Fragment;
        Fragment["fragment_number"] = static_cast<int64_t>(Num.get());
        Fragment["kind"] =
            Num == FragmentNum::main()
                ? "main"
                : Num == FragmentNum::cold() ? "cold" : "warm_or_auxiliary";
        Fragment["output_address"] = static_cast<int64_t>(OutputAddress);
        Fragment["output_size"] = static_cast<int64_t>(OutputSize);
        Fragment["block_count"] = static_cast<int64_t>(FF.size());
        json::Array BlockOriginalAddresses;
        json::Array BlockOutputAddresses;
        for (const BinaryBasicBlock *BB : FF) {
          BlockOriginalAddresses.push_back(
              BB->getOffset() == BinaryBasicBlock::INVALID_OFFSET
                  ? json::Value(nullptr)
                  : json::Value(static_cast<int64_t>(BF.getAddress() +
                                                     BB->getOffset())));
          BlockOutputAddresses.push_back(static_cast<int64_t>(
              AbsoluteBlockAddress(*BB, /*End=*/false)));
        }
        Fragment["block_original_addresses"] =
            std::move(BlockOriginalAddresses);
        Fragment["block_output_addresses"] =
            std::move(BlockOutputAddresses);
        Fragments.push_back(std::move(Fragment));
      }
    }
    Function["fragments"] = std::move(Fragments);
    json::Array LiteralPoolRanges;
    if (BF.requiresAddressStableLayout()) {
      const BinaryFunction::IslandInfo &Islands = BF.getIslandInfo();
      for (auto Data = Islands.DataOffsets.begin();
           Data != Islands.DataOffsets.end(); ++Data) {
        auto NextData = std::next(Data);
        auto Code = Islands.CodeOffsets.lower_bound(*Data);
        uint64_t End = BF.getSize();
        if (Code != Islands.CodeOffsets.end())
          End = std::min(End, *Code);
        if (NextData != Islands.DataOffsets.end())
          End = std::min(End, *NextData);
        json::Object Range;
        Range["original_start_address"] =
            static_cast<int64_t>(BF.getAddress() + *Data);
        Range["original_end_address"] =
            static_cast<int64_t>(BF.getAddress() + End);
        Range["output_start_address"] =
            static_cast<int64_t>(BF.getOutputAddress() + *Data);
        Range["output_end_address"] =
            static_cast<int64_t>(BF.getOutputAddress() + End);
        Range["size"] = static_cast<int64_t>(End - *Data);
        LiteralPoolRanges.push_back(std::move(Range));
      }
    }
    Function["literal_pool_ranges"] = std::move(LiteralPoolRanges);
    Function["blocks"] = std::move(Blocks);
    Function["instruction_origins"] = std::move(InstructionOrigins);
    Function["synthesized_inline_data"] =
        std::move(SynthesizedInlineData);
    Function["owned_literal_pools"] = std::move(OwnedLiteralPools);
    Function["inline_jump_tables"] = std::move(Tables);
    Functions.push_back(std::move(Function));
  }

  json::Object Root;
  Root["schema_version"] = 1;
  Root["producer"] = "BOLT FastDyn final firmware layout";
  Root["inline_jump_table_count"] =
      static_cast<int64_t>(InlineTableCount);
  Root["inline_jump_table_entry_count"] =
      static_cast<int64_t>(InlineTableEntryCount);
  Root["address_stable_function_count"] =
      static_cast<int64_t>(AddressStableFunctionCount);
  Root["split_function_count"] =
      static_cast<int64_t>(SplitFunctionCount);
  Root["synthesized_call_veneer_instruction_count"] =
      static_cast<int64_t>(SynthesizedCallVeneerInstructionCount);
  Root["synthesized_from_original_instruction_count"] =
      static_cast<int64_t>(SynthesizedTransformedInstructionCount);
  Root["synthesized_call_promotion_instruction_count"] =
      static_cast<int64_t>(SynthesizedCallPromotionInstructionCount);
  Root["synthesized_jump_table_promotion_instruction_count"] =
      static_cast<int64_t>(SynthesizedJumpTablePromotionInstructionCount);
  Root["synthesized_control_edge_instruction_count"] =
      static_cast<int64_t>(SynthesizedControlEdgeInstructionCount);
  Root["synthesized_inline_data_bytes"] =
      static_cast<int64_t>(SynthesizedInlineDataBytes);
  Root["target"] = BC.isARM() ? "arm_thumb" :
                     BC.isX86() ? "x86_64" : "unsupported";
  Root["target_cpu"] = BC.STI->getCPU();
  Root["target_features"] = BC.STI->getFeatureString();
  Root["target_triple"] = BC.TripleName;
  Root["functions"] = std::move(Functions);

  const std::string Filename = FirmwareReportFilename + ".layout.json";
  std::error_code EC;
  raw_fd_ostream OS(Filename, EC, sys::fs::OF_None);
  if (EC)
    return createStringError(EC, "cannot write %s", Filename.c_str());
  OS << formatv("{0:2}\n", json::Value(std::move(Root)));
  OS.close();
  if (OS.has_error())
    return createStringError(OS.error(), "failed writing %s", Filename.c_str());
  BC.outs() << "BOLT-INFO: FastDyn wrote linker-resolved firmware layout "
            << Filename << '\n';
  return Error::success();
}
