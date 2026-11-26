"""The pipeline-entry key ``enabled``: a pipeline opts a default-disabled pass in.

``enabled = true`` is equivalent to ``--enable`` for that entry; ``--disable`` and
``--only`` still win.  The key is written into the pipeline id only when true, so
every pipeline that does not use it keeps the id it was archived and timed with.
"""

import pytest

from scopt.manager import (
    DEFAULT_DISABLED_PASSES, PIPELINES_DIR, PassManager, PipelineError, R, load_pipeline,
    pipeline_from_mapping,
)
from scopt.manager_b3 import B3Env
from scopt.passes.base import Outcome
from tests.synth.fake_snapshot import fake_snapshot

_B3_OPT_IN = ("specialize-returns", "chain-returns", "chain-static")

#: pipeline_id of every pipeline shipped before the key existed (fd-int 5ff088c,
#: computed before the key was added).  Archived artifacts and timed arms name
#: these ids, so the key must leave every one of them unchanged.
_IDS_BEFORE_ENABLED = {
    "activation-layout": "sha256:affe0ec6936c77093210d6b7faf5950c9fdcecc5302e2c9bc50c0a7c8408cc1a",
    "b2-arena-slide": "sha256:57e1e648baab12ebd7845bd49d77e9ab278cacc4178c6c59838555cbb796b28b",
    "b2-layout": "sha256:42c7dd322aa7a0b1b0507c762c105b245094d74c57bbd7e00a59a3f435c93ac3",
    "b2-llvm-layout": "sha256:8e4610ea975be247b8f895b401ff18bbd8751056c56d10122443c6e56f6edeb6",
    "b2-page-local": "sha256:7ce03cf901f348e92fc246b0fe106a3d28dbc3784089389531d0131d71382b90",
    "b3-llvm": "sha256:c35af56f4bdce94c3df63a0d77b682706afaab8ce504f2c7cb10c8b257c7ab36",
    "b3-ph": "sha256:0bd2e4dfc8fedf691195a7eb575940ad6bf582bd939a12ba7939a97f8584376c",
    "b3": "sha256:2c9b4e8d9b3bd398859b2b86afbf494d3303c0c7a79bcf429feb095fedd68587",
    "b4": "sha256:d4af79820aec6b09980eaa5883765a7a983629ac2f7f73e5f5bea4a3b326fe71",
    "dominant-return": "sha256:acf6686ec234f7a50d4ebbcf4d8ab9dc66faeadcc149bda6f15a0c638f5dbc21",
    "exit-pc-fold": "sha256:9e2751c81249a204d9ed09f1b7fb9e332b2b9b3095ab4625416813cec8912132",
    "firmware-inline": "sha256:c34113e5398db78d79dca0a91e68d03d05382abaf1cd051490c0e418ab45bbab",
    "global-region-llvm": "sha256:f7bc0115ea5bb299f4c66e6337a4ae8ef32681deb49036002b8bb940c119413f",
    "global-region": "sha256:cc7adffc3692fe1d98fbb8408ff1c3eda7512ebaa308e1f159fb82e241766967",
    "global-specialization": "sha256:4b26d681d5550c7cdbbf1e62cf04112c278c14cf17a34aa466201fad8fa5c16f",
    "host-inline-extreme": "sha256:f602e078495732c7df834df3a69f8ea2008be77d2d922be40619902c8696ff29",
    "host-inline-maximal": "sha256:531eccac2d89856a5327c775f4ba0357daf7ab41161fc873646f534548386aac",
    "host-inline-splice": "sha256:edb7eaccd0a83976d115f0b891fcb67af66a9ecc38cc39e2e39218a2edc36223",
    "host-inline": "sha256:2dddb8c74e16b7ab15aa48f80c7840bb3cec5714b3c3976fc290d130ad582acc",
    "hot-loop-firmware-inline": "sha256:6527b7ee2b80cb1ff4a09980f8a75cfc59384abffd5f18bf289aac077026c9d8",
    "hot-loop-inline": "sha256:e4cd9f87b3812f198d5df12747ed6733e61ca35e69927f685d17806de9fb3dad",
    "identity": "sha256:2d92093660d2da94695a0e4579b7711d86c65725d4257278798bf2aefa209c7f",
    "irq-entry": "sha256:96ef943f9e627cb893441f7b16a86d2847608898be81d281f0d057d07ceacb43",
    "local-scalar": "sha256:44210905bf8e7656282fb8adfb3ef5e99aa41dfa736be4955517613434a68179",
    "native-program": "sha256:db78251039e1cb711b4bc5181bfe72e3b8839196686f2f5ab6ce44c3bd73a63b",
    "ordinary-exit": "sha256:bfd0b7ea7b5a7a4356d180db5159cb6887f0e99e1c845f8bd62a4eb7ad8ce645",
    "sealed-firmlooper-b3": "sha256:eb837e14435dc0885997b3bb6cfe44aa2ce9067ee15e10466269e136b937b319",
    "sealed-firmlooper-v2-b3": "sha256:b7f4ca38cdee0ce41f70f1cb621c3fc7409fd4b94f88b143b69a0a779a73fd10",
    "sealed-firmlooper-v2": "sha256:013b89fe94d364f9c7078c5864f203bcfdf9b69152611963c12ffe1d42ef1988",
    "sealed-firmlooper-v3": "sha256:8c1430fb6d3e1d6fe075130e795f5a5ea4816e072e0bd069adc02ff21c2b616d",
    "sealed-firmlooper": "sha256:1800c67743ba26505e417c8ec7818fbaba4fdfd4cdf1e9a12812497bd44ada09",
    "sealed-pgo-7-b3": "sha256:f6e0bf88fb1c34aca14bfa4aca405f3b74d4af300bf1f0e8ef9bd5dfabaae5bf",
    "sealed-pgo-7": "sha256:27e5c3c17fc79da5c66ee2fe38981cef4053a99d78f1a14cbf2ffa5e5a704052",
    "sealed-whopr-7-b3": "sha256:b3db04d53fd267ceaf09426c2fa7df2581286278c1eac2bad976d4c58ec5d2b5",
    "sealed-whopr-7-bounded-b3": "sha256:c5c25c80de8bc4429376376512c52313f711763dd38dffb471f2ab70325a6383",
    "sealed-whopr-7-bounded": "sha256:6a9b166a1b09c8717cb8683758c7f62701d6d507b22f44ee70969ac74f2b7bb7",
    "sealed-whopr-7": "sha256:217d45302c84488b6a0a0cc2e390ed83dfdc4aa27d2d9e2fb91e126d36bc5f8d",
    "tcg-entry-sfb": "sha256:295ea6f3ad27c482005b09878aa762e6916037f8426ebaf6d7ecdadde2d2e718",
    "trusted-helper-inline": "sha256:0456afe6538274bfb573a7db9d474ca6c1d49e009c056d1dfec481210b3b3d46",
}


def _shipped(name):
    return load_pipeline(PIPELINES_DIR / f"{name}.toml")


def _b3_doc(opt_in=_B3_OPT_IN, value=True):
    """b3.toml as a mapping, with ``enabled = value`` on the ``opt_in`` entries."""
    doc = _shipped("b3").to_json()
    for row in doc["pass"]:
        if row["name"] in opt_in:
            row["enabled"] = value
    return doc


@pytest.mark.parametrize("name", sorted(_IDS_BEFORE_ENABLED))
def test_existing_pipeline_ids_are_unchanged(name):
    pipeline = _shipped(name)
    assert pipeline.pipeline_id == _IDS_BEFORE_ENABLED[name]
    assert not any(entry.enabled for entry in pipeline.passes)
    assert all("enabled" not in row for row in pipeline.to_json()["pass"])


def test_enabled_false_is_the_default_and_keeps_the_id():
    plain = _shipped("b3")
    explicit = pipeline_from_mapping(_b3_doc(value=False))
    assert explicit == plain and explicit.pipeline_id == plain.pipeline_id


def test_enabled_true_is_hashed_and_round_trips():
    pipeline = pipeline_from_mapping(_b3_doc())
    assert pipeline.pipeline_id != _shipped("b3").pipeline_id
    assert {e.name for e in pipeline.passes if e.enabled} == set(_B3_OPT_IN)
    rows = {row["name"]: row for row in pipeline.to_json()["pass"]}
    assert {n for n, row in rows.items() if row.get("enabled") is True} == set(_B3_OPT_IN)
    assert all(row.get("enabled", True) is True for row in rows.values())
    again = pipeline_from_mapping(pipeline.to_json())
    assert again == pipeline and again.pipeline_id == pipeline.pipeline_id


def test_enabled_entry_runs_without_enable():
    snapshot = fake_snapshot(2)
    keyed = PassManager(snapshot, _b3_doc(), b3=B3Env())
    flagged = PassManager(snapshot, _shipped("b3"), b3=B3Env(), enable=_B3_OPT_IN)
    plain = PassManager(snapshot, _shipped("b3"), b3=B3Env())
    assert keyed.disabled_by_default == frozenset()
    assert plain.disabled_by_default == frozenset(_B3_OPT_IN)
    # Opted-in B3 passes are calibrated exactly as with --enable.
    assert keyed._b3_costs == flagged._b3_costs and keyed._b3_costs
    assert not plain._b3_costs
    keyed_run, flagged_run = keyed.execute(), flagged.execute()
    assert keyed_run.ablation == flagged_run.ablation == {"disable": [], "only": None}
    for name in _B3_OPT_IN:
        assert keyed_run.outcome(name).reason != R.DISABLED_BY_DEFAULT
        assert (keyed_run.outcome(name).outcome, keyed_run.outcome(name).reason) == \
            (flagged_run.outcome(name).outcome, flagged_run.outcome(name).reason)


def test_enabled_entry_alone_opts_in_only_that_pass():
    snapshot = fake_snapshot(2)
    run = PassManager(snapshot, _b3_doc(opt_in=("chain-static",))).execute()
    assert run.outcome("chain-static").reason != R.DISABLED_BY_DEFAULT
    for name in ("specialize-returns", "chain-returns"):
        assert run.outcome(name).reason == R.DISABLED_BY_DEFAULT
    assert run.ablation["disable"] == ["chain-returns", "specialize-returns"]


def test_disable_and_only_still_win_over_enabled():
    snapshot = fake_snapshot(2)
    run = PassManager(snapshot, _b3_doc(), disable=["chain-static"]).execute()
    skipped = run.outcome("chain-static")
    assert (skipped.outcome, skipped.reason) == (Outcome.SKIPPED_UNSUPPORTED, R.ABLATION_DISABLED)
    assert run.outcome("chain-returns").reason != R.DISABLED_BY_DEFAULT
    assert run.ablation["disable"] == ["chain-static"]
    run = PassManager(snapshot, _b3_doc(), only="chain-returns").execute()
    assert run.outcome("chain-returns").reason != R.DISABLED_BY_DEFAULT
    assert run.outcome("chain-static").reason == R.ABLATION_DISABLED
    # --enable on an opted-in entry is redundant and harmless.
    run = PassManager(snapshot, _b3_doc(), enable=["chain-static"]).execute()
    assert run.ablation["disable"] == []


@pytest.mark.parametrize("name", ["constant-propagation", "host-inline", "firmware-inline"])
@pytest.mark.parametrize("value", [True, False])
def test_enabled_on_a_default_on_pass_is_an_error(name, value):
    assert name not in DEFAULT_DISABLED_PASSES
    doc = {"name": "custom", "pass": [{"name": name, "version": 1, "enabled": value}]}
    with pytest.raises(PipelineError, match="runs by default"):
        pipeline_from_mapping(doc)


@pytest.mark.parametrize("value", [1, 0, "true", None, [True]])
def test_enabled_must_be_a_boolean(value):
    doc = {"name": "custom", "pass": [{"name": "chain-static", "version": 1, "enabled": value}]}
    with pytest.raises(PipelineError, match="'enabled' must be a boolean"):
        pipeline_from_mapping(doc)


def test_enabled_toml_entry_loads(tmp_path):
    path = tmp_path / "opt-in.toml"
    path.write_text('name = "opt-in"\n'
                    '[[pass]]\nname = "chain-static"\nversion = 1\nenabled = true\n'
                    'params = { targets_per_exit = 2 }\n')
    entry, = load_pipeline(path).passes
    assert (entry.name, entry.enabled, dict(entry.params)) == \
        ("chain-static", True, {"targets_per_exit": 2})
