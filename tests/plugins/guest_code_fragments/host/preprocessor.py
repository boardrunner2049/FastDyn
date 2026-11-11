from fastdyn.virtual_preprocessing import (
    ConfigurationHelp, RunContext, RunDefinition, RunPrepareResult,
    VirtualPreparationError, register_run_preprocessor,
)


class GuestCodeFragmentsPreprocessor:
    def prepare(self, ctx: RunContext) -> RunPrepareResult:
        profile = ctx.settings.get("profile")
        pc = ctx.settings.get("pc")
        valid = {"a32", "thumb", "a64", "x86_16", "x86_32", "x86_64", "rv32", "rv64"}
        if profile not in valid or not isinstance(pc, int) or pc < 0:
            raise VirtualPreparationError(
                "guest_code_fragments requires a valid profile and nonnegative integer pc"
            )
        settings = ctx.plugin_artifact_path("settings.txt")
        settings.write_text(f"{profile} {pc:x}\n", encoding="utf-8")
        return RunPrepareResult(artifacts=[settings])


register_run_preprocessor(RunDefinition(
    name="guest_code_fragments",
    prepare=GuestCodeFragmentsPreprocessor(),
    enabled=lambda ctx: bool(ctx.settings.get("enabled", False)),
    help=ConfigurationHelp(
        "Insert a target instruction fragment before one guest PC.",
        '[CPU.cpu0.plugins.guest_code_fragments]\nenabled = true\nprofile = "thumb"\npc = 0x08000100',
        "docs/GuestInstructionFragments.md",
    ),
))
