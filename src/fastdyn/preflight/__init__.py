"""Firmware preflight plus an explicitly separate FastDyn integration layer."""

from __future__ import annotations

from .artifacts import (
    BundleValidationError,
    ExecutionBundle,
    MemoryRange,
    OriginInstruction,
    OriginRange,
    PatchBinding,
    TargetDescription,
    load_execution_bundle,
    publish_execution_bundle,
)
from .candidate import (
    FirmwareCandidate,
    load_firmware_candidate,
    publish_firmware_candidate,
)
from .bolt_policy import (
    BoltDeploymentArtifact,
    load_bolt_deployment,
    publish_bolt_deployment,
    publish_fixed_layout_firmware_candidate,
    publish_hot_fop_fallback_deployment,
    publish_relocatable_firmware_candidate,
)
from .cost import evaluate_generated_host_inventory
from .driver import create_identity_bundle
from .firmware import (
    TargetQualification,
    create_identity_firmware_candidate,
    qualify_firmware,
)
from .validation import (
    ObservationTrace,
    TraceComparison,
    compare_observation_traces,
    load_observation_trace,
)
from .emission import EmitInPlace
from .passes import (
    ExperimentalStoreRemovalPass,
    OrdinaryStackForwardingPass,
    ShortAccessorPass,
    ThumbFallthroughBranchPass,
    ThumbHotExitLayoutPass,
    ThumbStackForwardingPass,
)
from .references import (
    AdmittedMachineState,
    BoundedStateConstructor,
    ELFReferenceResolver,
    StateConstructionResult,
)
from .region import (
    EffectSummary,
    FirmwareUnitView,
    InstructionIdentity,
    MemoryEffect,
    OriginStateMapping,
    PatchContract,
    ProtectedEntry,
    ReferenceEdge,
    TypedRange,
)
from .sdk import (
    Decision,
    PassDescriptor,
    PassRegistry,
    PassResult,
    PreflightContext,
    PreflightPipeline,
    ProfitabilityChecker,
    ReplacementEdit,
    RewriteBuilder,
    StageLedger,
)

__all__ = [
    "BundleValidationError",
    "AdmittedMachineState",
    "BoundedStateConstructor",
    "BoltDeploymentArtifact",
    "Decision",
    "ELFReferenceResolver",
    "EffectSummary",
    "EmitInPlace",
    "ExecutionBundle",
    "ExperimentalStoreRemovalPass",
    "FirmwareCandidate",
    "FirmwareUnitView",
    "InstructionIdentity",
    "MemoryRange",
    "MemoryEffect",
    "ObservationTrace",
    "OrdinaryStackForwardingPass",
    "OriginRange",
    "OriginInstruction",
    "OriginStateMapping",
    "PatchBinding",
    "PatchContract",
    "PassDescriptor",
    "PassRegistry",
    "PassResult",
    "PreflightContext",
    "PreflightPipeline",
    "ProfitabilityChecker",
    "ProtectedEntry",
    "ReferenceEdge",
    "ReplacementEdit",
    "RewriteBuilder",
    "ShortAccessorPass",
    "StageLedger",
    "StateConstructionResult",
    "TargetDescription",
    "ThumbFallthroughBranchPass",
    "ThumbHotExitLayoutPass",
    "ThumbStackForwardingPass",
    "TargetQualification",
    "TraceComparison",
    "TypedRange",
    "compare_observation_traces",
    "create_identity_bundle",
    "create_identity_firmware_candidate",
    "evaluate_generated_host_inventory",
    "load_execution_bundle",
    "load_bolt_deployment",
    "load_firmware_candidate",
    "load_observation_trace",
    "publish_execution_bundle",
    "publish_bolt_deployment",
    "publish_fixed_layout_firmware_candidate",
    "publish_hot_fop_fallback_deployment",
    "publish_relocatable_firmware_candidate",
    "publish_firmware_candidate",
    "qualify_firmware",
]
