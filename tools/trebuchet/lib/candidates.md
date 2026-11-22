# Parameterized candidate recipes

The materializer accepts the existing `recipes` list and stage-wide `params`.
An optional `nodes` object gives distinct names to parameter variants of the
same optimizer stage. Names are lowercase letters, digits, underscores and
hyphens, beginning with a letter. They cannot shadow a built-in stage or `warm`.
Each node names one built-in `stage` and an optional `params` object.

```json
{
  "nodes": {
    "guarded": {
      "stage": "native",
      "params": {"host-inline": {"udiv_mode": "guarded"}}
    },
    "cleanup": {
      "stage": "native",
      "params": {"host-inline": {"udiv_mode": "cleanup"}}
    },
    "partial": {
      "stage": "native",
      "params": {"host-inline": {"udiv_mode": "partial-cleanup"}}
    }
  },
  "params": {
    "native": {
      "host-inline": {
        "mode": "splice",
        "allow_faulting": true,
        "max_sites": 32,
        "max_total_growth": 8192
      }
    }
  },
  "recipes": [
    "warm", "b3", "sfb", "sfb+b3",
    "guarded", "cleanup", "partial",
    "guarded+sfb+b3", "cleanup+sfb+b3", "partial+sfb+b3"
  ],
  "budget": {"candidate_limit": 20, "offline_seconds": 7200}
}
```

This is the recipe portion of a materialization plan, not a complete executable
plan. Add the admitted real application's `application`, `scenario` and
`configuration` names, absolute `admission` and `contract` file paths, and an
`objective` containing `name`, `unit`, `direction`, `useful_work` and `boundaries`.
Each prepared `images.original` or `images.firmbolt` binding supplies absolute
paths for `firmware`, `config`, `runtime`, `snapshot`, `profile`, `manifest`,
`prep` and `admission_runtime`. The admission record must identify the same
application with `status: "admitted"`; preparation must bind each image's
ordinary snapshot to its exact training profile and firmware.
The example contains ten declared candidates per image and permits both original
and FirmBOLT images. Set the budget before execution according to the available
host and intended experiment. Every declared candidate counts toward the limit,
including aliases and candidates later found equivalent or unsupported.

## Parameters and reuse

`params.native` supplies defaults to every node whose stage is `native`.
Node parameters overlay these defaults one level inside each pass object. In
the example, all three modes retain the same growth/site caps, and each node
changes only `host-inline.udiv_mode`. Scalar package parameters, such as
`sfb-hot.entry_coverage`, replace their corresponding stage defaults. Parameters
fixed by a built-in alias, such as `native-1`'s one-site, non-faulting contract,
remain enforced. Conflicting requests fail that candidate.

A generation is reused only within one materialization invocation, on the same
prepared image, with the same preceding stage sequence and canonical parameter
objects. Node names do not affect reuse: two differently named nodes with the
same stage and parameters share their generated artifact and any identical
successor prefixes. Different modes or budgets get separate generations.
Pipeline and optimizer defaults remain in their existing implementations;
spelling out a default may conservatively create another generation. Reuse does
not assume that different built-in aliases are interchangeable.

The registry's candidate ID preserves the requested recipe, for example
`original/partial+sfb+b3`. Its `recipe` remains the expanded stage sequence
`["native", "sfb", "b3"]`, matching the generation receipts used by the selector.
`recipe_nodes` records the requested node name, stage, effective invocation
parameters and identity at each position. `recipe_identity` identifies the
expanded parameterized sequence independently of its names. Generation receipts
retain the actual stage, parameters, source/output snapshot IDs and command
receipt. Distinct parameterized prefixes have separate digest-named directories;
legacy plans retain their existing directory names.

## Use the result to test improvement over the incumbent

The purpose of these variants is to let the normal selection pipeline compare
useful native rewrites and their composition with the existing SFB/B3 recipe.
Materialization alone does not establish that a mode is applicable, correct or
faster. An unsupported stage remains a failed or unsupported candidate with its
evidence, and a byte-identical result remains an equivalent deployment.

Freeze the real-application input roles, comparison metric, budgets and incumbent
before ranking. Build the parameterized registry, then use the existing
[selection and final-evaluation protocol](joint_selection.md). Bind the exact
previously selected incumbent with `incumbent_id` and `incumbent_identity`, and
use `incumbent_registry` plus `incumbent_image` if it comes from a prior registry.
Retain the common original ordinary reference and the separate incumbent
promotion comparison. For a bounded rebuild of a chosen recipe, retain its node
definitions and stage parameters together with its recipe string.

The native-versus-native and native-plus-SFB comparisons are hypotheses for
performance improvement. Reserve final scenarios and report measured outcomes,
including regressions, before promoting a default. These configuration examples
do not contain timing observations or assert a speedup.
