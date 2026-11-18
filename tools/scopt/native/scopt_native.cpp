// LLVM/BOLT APIs used here are pinned to llvmorg-20.1.2.
// The backend emits a candidate. The independent scopt checker certifies it.

#include "bolt/Core/BinaryBasicBlock.h"
#include "bolt/Core/BinaryContext.h"
#include "bolt/Core/BinaryFunction.h"
#include "bolt/Core/MCPlusBuilder.h"
#include "bolt/Passes/Inliner.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallString.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/DebugInfo/DWARF/DWARFContext.h"
#include "llvm/ExecutionEngine/Orc/SymbolStringPool.h"
#include "llvm/MC/MCAsmBackend.h"
#include "llvm/MC/MCCodeEmitter.h"
#include "llvm/MC/MCDisassembler/MCDisassembler.h"
#include "llvm/MC/MCExpr.h"
#include "llvm/MC/MCFixup.h"
#include "llvm/MC/MCFixupKindInfo.h"
#include "llvm/MC/MCInstrAnalysis.h"
#include "llvm/MC/MCInstrInfo.h"
#include "llvm/MC/MCRegisterInfo.h"
#include "llvm/Object/ObjectFile.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Regex.h"
#include "llvm/Support/SHA256.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdint>
#include <limits>
#include <optional>
#include <string>
#include <vector>

using namespace llvm;
using namespace llvm::bolt;
using namespace llvm::object;

namespace {

cl::opt<bool> PrintPass("scopt-print-inline", cl::Hidden);
constexpr unsigned Version = 1;
constexpr unsigned MaxSourceBytes = 1024;
constexpr unsigned MaxBlocks = 128;

json::Object refuse(StringRef Reason, StringRef Detail) {
  return json::Object{{"version", Version}, {"status", "refused"}, {"reason", Reason},
          {"detail", Detail}};
}

bool matchesSHA(ArrayRef<uint8_t> Data, StringRef Expected) {
  Expected.consume_front("sha256:");
  return toHex(SHA256::hash(Data), /*LowerCase=*/true) == Expected;
}

struct Instruction {
  unsigned Offset;
  unsigned Size;
  std::optional<uint64_t> Target;
  bool Return;
};

struct Fixup {
  unsigned Offset;
  unsigned After;
  std::optional<unsigned> TargetBlock;
};

struct Program {
  SmallString<256> Bytes;
  json::Array Origins;
  std::vector<Fixup> Fixups;
  DenseMap<unsigned, unsigned> BlockOffsets;
};

bool isAllowedRegister(StringRef Name) {
  static const Regex GPR(
      "^(R(AX|BX|CX|DX|SI|DI|BP|[89]|1[0-5])|"
      "E(AX|BX|CX|DX|SI|DI|BP)|"
      "(AX|BX|CX|DX|SI|DI|BP|AL|BL|CL|DL|AH|BH|CH|DH|SIL|DIL|BPL)|"
      "R([89]|1[0-5])[DWB])$");
  return GPR.match(Name);
}

bool isAllowedInstruction(StringRef Name) {
  static const Regex Integer(
      "^(ADD|SUB|AND|OR|XOR|CMP|TEST|ADC|SBB|MOV|SHL|SHR|SAR|ROL|ROR)"
      "(8|16|32|64)(rr|ri|ri8|ri16|ri32|ri64|i8|i16|i32|i64|r1|rCL)$");
  static const Regex Cond("^(CMOV(16|32|64)rr|SETCCr)$");
  return Integer.match(Name) || Cond.match(Name) || Name == "ENDBR64" ||
         Name == "BSWAP32r" || Name == "BSWAP64r" || Name == "LEA32r" ||
         Name == "LEA64r" || Name == "LEA64_32r" || Name == "NOOP";
}

std::optional<json::Object>
checkInstructions(BinaryContext &BC, ArrayRef<uint8_t> Body, uint64_t Address,
                  DenseMap<unsigned, Instruction> &Rows) {
  const MCPlusBuilder &MIB = *BC.MIB;
  const MCPhysReg SP = MIB.getStackPointer();
  bool Returns = false;
  for (unsigned Offset = 0; Offset < Body.size();) {
    uint64_t Size = 0;
    MCInst Inst;
    if (BC.DisAsm->getInstruction(Inst, Size, Body.drop_front(Offset),
                                  Address + Offset, nulls()) !=
            MCDisassembler::Success ||
        !Size || Size > Body.size() - Offset)
      return refuse("unsupported_instruction", "invalid source instruction");

    const bool IsReturn = MIB.isReturn(Inst);
    const bool IsBranch = MIB.isBranch(Inst);
    const MCInstrDesc &Desc = BC.MII->get(Inst.getOpcode());
    StringRef Opcode = BC.MII->getName(Inst.getOpcode());
    std::optional<uint64_t> Target;
    if (IsReturn) {
      if (Size != 1 || Body[Offset] != 0xc3)
        return refuse("unsupported_stack", "return has extra stack behavior");
      Returns = true;
    } else {
      if (MIB.isCall(Inst) || MIB.isIndirectBranch(Inst))
        return refuse("unsupported_control_flow", "nested or indirect control transfer");
      if (Desc.isTrap() || Opcode == "INT3" || Opcode == "INT" || Opcode == "UD2")
        return refuse("unsupported_instruction", "source instruction traps");
      if (MIB.hasUseOfPhysReg(Inst, SP) || MIB.hasDefOfPhysReg(Inst, SP))
        return refuse("unsupported_stack", "instruction accesses the stack pointer");
      if (Desc.mayLoad() || Desc.mayStore())
        return refuse("unsupported_memory", "source instruction accesses memory");
      if (IsBranch) {
        const uint8_t First = Body[Offset];
        const bool PlainBranch =
            (Size == 2 && (First == 0xeb || (First & 0xf0) == 0x70)) ||
            (Size == 5 && First == 0xe9) ||
            (Size == 6 && First == 0x0f && (Body[Offset + 1] & 0xf0) == 0x80);
        uint64_t Destination;
        if (!PlainBranch ||
            !(Opcode == "JMP_1" || Opcode == "JMP_4" || Opcode == "JCC_1" ||
              Opcode == "JCC_4") ||
            !BC.MIA->evaluateBranch(Inst, Address + Offset, Size, Destination) ||
            Destination < Address || Destination - Address >= Body.size())
          return refuse("unsupported_control_flow", "unsupported or external branch");
        Target = Destination - Address;
      } else {
        if (!isAllowedInstruction(Opcode))
          return refuse("unsupported_instruction", Opcode);
        for (const MCOperand &Operand : Inst)
          if (Operand.isReg() && Operand.getReg() &&
              !isAllowedRegister(BC.MRI->getName(Operand.getReg())))
            return refuse("unsupported_instruction", "instruction uses a non-GPR operand");
      }
    }
    Rows.try_emplace(Offset, Instruction{Offset, static_cast<unsigned>(Size),
                                       Target, IsReturn});
    Offset += Size;
  }

  if (!Returns)
    return refuse("unsupported_control_flow", "callee has no ordinary return");
  for (const auto &Row : Rows)
    if (Row.second.Target && !Rows.count(*Row.second.Target))
      return refuse("unsupported_control_flow", "branch targets an instruction interior");
  return std::nullopt;
}

std::optional<json::Object>
encode(BinaryContext &BC, MCInst Inst, const Instruction &Origin,
       std::optional<unsigned> TargetBlock, bool HasTarget, Program &Out) {
  SmallString<32> Bytes;
  SmallVector<MCFixup, 2> Fixups;
  // Force the x86 near form for symbolic transfers so no final placement can
  // invalidate an optimistic rel8. LLVM chooses and emits the relaxed opcode.
  if (HasTarget && BC.MAB->mayNeedRelaxation(Inst, *BC.STI))
    BC.MAB->relaxInstruction(Inst, *BC.STI);
  BC.MCE->encodeInstruction(Inst, Bytes, Fixups, *BC.STI);
  if (Bytes.empty())
    return refuse("unsupported_instruction", "LLVM emitted a zero-width instruction");
  const unsigned Start = Out.Bytes.size();
  if (HasTarget) {
    if (Fixups.size() != 1 || Fixups[0].getOffset() + 4 != Bytes.size())
      return refuse("unsupported_control_flow", "unsupported LLVM branch relocation field");
    const auto &Info = BC.MAB->getFixupKindInfo(Fixups[0].getKind());
    if (Info.TargetOffset != 0 || Info.TargetSize != 32 ||
        !(Info.Flags & MCFixupKindInfo::FKF_IsPCRel))
      return refuse("unsupported_control_flow", "LLVM branch relocation is not rel32");
    Out.Fixups.push_back({Start + Fixups[0].getOffset(),
                          Start + static_cast<unsigned>(Bytes.size()), TargetBlock});
  } else if (!Fixups.empty()) {
    return refuse("unsupported_relocation", "source data instruction requests relocation");
  }
  Out.Origins.emplace_back(json::Object{
      {"source_off", Origin.Offset}, {"source_size", Origin.Size},
      {"output_off", Start}, {"output_size", Bytes.size()}});
  Out.Bytes.append(Bytes);
  return std::nullopt;
}

json::Object transplant(const json::Object &Request) {
  const auto Path = Request.getString("image_path");
  const auto Address = Request.getInteger("address");
  const auto Size = Request.getInteger("size");
  const auto ImageSHA = Request.getString("image_sha256");
  const auto CodeSHA = Request.getString("code_sha256");
  if (Request.getInteger("version") != Version || !Path || Path->empty() ||
      !Address || *Address <= 0 || !Size || *Size <= 0 ||
      *Size > MaxSourceBytes || !ImageSHA || !CodeSHA)
    return refuse("bridge_request_invalid", "missing or invalid version-1 request fields");

  const auto Input = MemoryBuffer::getFile(*Path);
  if (!Input)
    return refuse("native_image_unavailable", Input.getError().message());
  // Read a private buffer rather than retaining a file-backed mmap. The
  // checked bytes are the same immutable bytes used for ELF and MC decoding.
  const auto Image = MemoryBuffer::getMemBufferCopy((*Input)->getBuffer(), *Path);
  if (!matchesSHA(arrayRefFromStringRef(Image->getBuffer()), *ImageSHA))
    return refuse("native_image_mismatch", "ELF SHA-256 changed");

  auto ObjOrErr = ObjectFile::createObjectFile(Image->getMemBufferRef());
  if (!ObjOrErr)
    return refuse("native_image_unavailable", toString(ObjOrErr.takeError()));
  ObjectFile &Object = **ObjOrErr;
  if (!Object.isELF() || Object.getArch() != Triple::x86_64 ||
      !Object.isLittleEndian() || Object.isRelocatableObject())
    return refuse("unsupported_host", "expected a linked little-endian x86-64 ELF");

  const uint64_t Start = *Address;
  std::optional<SectionRef> Text;
  for (const SectionRef &Section : Object.sections()) {
    const uint64_t At = Section.getAddress();
    if (Section.isText() && !Section.isVirtual() && Start >= At &&
        Start - At < Section.getSize() &&
        uint64_t(*Size) <= Section.getSize() - (Start - At)) {
      if (Text)
        return refuse("ambiguous_bounds", "source range overlaps multiple text sections");
      Text = Section;
    }
  }
  if (!Text)
    return refuse("ambiguous_bounds", "source range is outside ELF text");
  auto Contents = Text->getContents();
  if (!Contents)
    return refuse("native_image_unavailable", toString(Contents.takeError()));
  ArrayRef<uint8_t> Body = arrayRefFromStringRef(*Contents).slice(
      Start - Text->getAddress(), *Size);
  if (!matchesSHA(Body, *CodeSHA))
    return refuse("native_image_mismatch", "callee SHA-256 changed");

  auto Context = BinaryContext::createBinaryContext(
      Triple("x86_64-unknown-linux-gnu"), std::make_shared<orc::SymbolStringPool>(),
      *Path, nullptr, false,
      DWARFContext::create(Object, DWARFContext::ProcessDebugRelocations::Ignore),
      JournalingStreams{errs(), errs()});
  if (!Context)
    return refuse("bridge_context_error", toString(Context.takeError()));
  BinaryContext &BC = **Context;
  BC.initializeTarget(std::unique_ptr<MCPlusBuilder>(createX86MCPlusBuilder(
      BC.MIA.get(), BC.MII.get(), BC.MRI.get(), BC.STI.get())));
  DenseMap<unsigned, Instruction> Rows;
  if (auto Error = checkInstructions(BC, Body, Start, Rows))
    return std::move(*Error);

  BinarySection &Section = BC.registerSection(*Text);
  BinaryFunction &Callee = *BC.createBinaryFunction("scopt.callee", Section,
                                                     Start, *Size);
  if (Error E = Callee.disassemble())
    return refuse("unsupported_control_flow", toString(std::move(E)));
  if (Error E = Callee.buildCFG(0))
    return refuse("unsupported_control_flow", toString(std::move(E)));
  if (!Callee.hasCFG() || !Callee.size() || Callee.size() > MaxBlocks ||
      !Callee.isSimple() || Callee.hasJumpTables() || Callee.isMultiEntry() ||
      getInliningInfo(Callee).Type != INL_ANY)
    return refuse("bolt_inlining_ineligible", "BOLT refused the source CFG");

  // BOLT's inliner adds new blocks in source iteration order. Keep a separate
  // origin map because BOLT deliberately strips MCInst annotations.
  std::vector<const BinaryBasicBlock *> SourceBlocks;
  for (const BinaryBasicBlock &BB : Callee)
    SourceBlocks.push_back(&BB);
  BinaryFunction &Caller = *BC.createInjectedBinaryFunction("scopt.caller");
  BinaryBasicBlock &First = *Caller.addBasicBlock();
  BinaryBasicBlock &Continuation = *Caller.addBasicBlock();
  First.addSuccessor(&Continuation);
  MCInst Call;
  BC.MIB->createCall(Call, Callee.getSymbol(), BC.Ctx.get());
  First.addInstruction(std::move(Call));
  Inliner Pass(PrintPass);
  Pass.inlineVerifiedCall(First, First.begin(), Callee);

  std::vector<BinaryBasicBlock *> Cloned;
  DenseMap<const MCSymbol *, unsigned> Labels;
  for (BinaryBasicBlock &BB : Caller)
    if (&BB != &Continuation) {
      Labels.try_emplace(BB.getLabel(), Cloned.size());
      Cloned.push_back(&BB);
    }
  if (Cloned.size() != SourceBlocks.size())
    return refuse("bridge_origin_mismatch", "BOLT cloned an unexpected number of blocks");

  Program Out;
  unsigned NumOrigins = 0;
  for (unsigned Index = 0; Index < Cloned.size(); ++Index) {
    const BinaryBasicBlock &Source = *SourceBlocks[Index];
    const BinaryBasicBlock &Clone = *Cloned[Index];
    unsigned SourceOff = Source.getOffset();
    auto It = Clone.begin();
    Out.BlockOffsets.try_emplace(Index, Out.Bytes.size());
    for (const MCInst &SourceInst : Source) {
      if (BC.MIB->isPseudo(SourceInst))
        continue;
      auto Row = Rows.find(SourceOff);
      if (Row == Rows.end())
        return refuse("bridge_origin_mismatch", "missing original instruction offset");
      const Instruction &Origin = Row->second;
      SourceOff += Origin.Size;
      ++NumOrigins;

      MCInst Inst;
      std::optional<unsigned> Target;
      const bool HasTarget = Origin.Return || Origin.Target.has_value();
      if (Origin.Return) {
        if (!BC.MIB->isReturn(SourceInst))
          return refuse("bridge_origin_mismatch", "source CFG lost its ordinary return");
        BC.MIB->createLongUncondBranch(Inst, Continuation.getLabel(), BC.Ctx.get());
      } else {
        if (It == Clone.end() || It->getOpcode() != SourceInst.getOpcode())
          return refuse("bridge_origin_mismatch", "clone instruction order differs");
        Inst = *It++;
        if (Origin.Target) {
          const MCSymbol *Label = BC.MIB->getTargetSymbol(Inst);
          auto Dest = Labels.find(Label);
          if (Dest == Labels.end() || SourceBlocks[Dest->second]->getOffset() != *Origin.Target)
            return refuse("bridge_origin_mismatch", "clone taken edge differs from source");
          Target = Dest->second;
        }
      }
      const unsigned OutputOff = Out.Bytes.size();
      if (auto Error = encode(BC, std::move(Inst), Origin, Target, HasTarget, Out))
        return std::move(*Error);
      // Keep a strict subset. Re-encoding any non-transfer must preserve
      // every original byte, including an instruction's prefix behavior.
      if (!HasTarget && arrayRefFromStringRef(StringRef(Out.Bytes).drop_front(OutputOff)) !=
                            Body.slice(Origin.Offset, Origin.Size))
        return refuse("unsupported_instruction", "LLVM changed a non-transfer encoding");
    }
    if (It != Clone.end())
      return refuse("bridge_origin_mismatch", "clone has extra instructions");
    // Encoding in original block order preserves every conditional and
    // ordinary fallthrough. Refuse reordered or incomplete source tilings.
    if (Index + 1 < SourceBlocks.size() &&
        SourceOff != SourceBlocks[Index + 1]->getOffset())
      return refuse("bridge_origin_mismatch", "source blocks do not tile in order");
    if (Index + 1 == SourceBlocks.size() && SourceOff != Body.size())
      return refuse("bridge_origin_mismatch", "source CFG does not cover the full symbol");
  }
  if (NumOrigins != Rows.size())
    return refuse("bridge_origin_mismatch", "source instruction count differs");

  json::Array Relocations;
  for (const Fixup &Rel : Out.Fixups) {
    if (!Rel.TargetBlock) {
      Relocations.emplace_back(json::Object{
          {"field_off", Rel.Offset}, {"width", 4}, {"target", "continuation"}});
      continue;
    }
    int64_t Disp = int64_t(Out.BlockOffsets.lookup(*Rel.TargetBlock)) - Rel.After;
    if (Disp < std::numeric_limits<int32_t>::min() ||
        Disp > std::numeric_limits<int32_t>::max())
      return refuse("unsupported_control_flow", "internal branch is out of range");
    for (unsigned Byte = 0; Byte != 4; ++Byte)
      Out.Bytes[Rel.Offset + Byte] = char(uint32_t(Disp) >> (Byte * 8));
  }
  return json::Object{{"version", Version}, {"status", "ok"},
          {"code", toHex(arrayRefFromStringRef(Out.Bytes), /*LowerCase=*/true)},
          {"origins", std::move(Out.Origins)}, {"relocations", std::move(Relocations)}};
}

} // namespace

int main(int Argc, char **Argv) {
  if (Argc == 2 && StringRef(Argv[1]) == "--version") {
    outs() << "scopt-native bridge 1 (LLVM/BOLT 20.1.2)\n";
    return 0;
  }
  LLVMInitializeX86TargetInfo();
  LLVMInitializeX86TargetMC();
  LLVMInitializeX86Disassembler();

  auto Buffer = MemoryBuffer::getSTDIN();
  if (!Buffer) {
    outs() << json::Value(refuse("bridge_request_invalid", "cannot read stdin")) << '\n';
    return 0;
  }
  auto Request = json::parse((*Buffer)->getBuffer());
  if (!Request) {
    consumeError(Request.takeError());
    outs() << json::Value(refuse("bridge_request_invalid", "invalid JSON")) << '\n';
    return 0;
  }
  const json::Object *Object = Request->getAsObject();
  outs() << json::Value(Object ? transplant(*Object)
                              : refuse("bridge_request_invalid", "expected JSON object"))
         << '\n';
}
