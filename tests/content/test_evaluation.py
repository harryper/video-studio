"""Offline evaluation harness tests.

Three concerns are covered here:

1. ``_one_draft_distance`` returns the correct 0-1 distance for
   identical vs. structurally-different drafts (regression guard for
   the inverted-math bug found in Task 13 review).
2. ``_score_metric`` consumes ``auto_fields`` and varies per dimension
   (regression guard for the stub scoring that ignored signals).
3. ``_check_thresholds`` flips when scores cross the §11.3 acceptance
   boundaries and the CLI exit code reflects the aggregate pass rate.

All tests use the recorded fixtures under
``tests/fixtures/provider_responses/`` — no synthetic text.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from studio.evaluation import (
    DEFAULT_OUTPUT_DIR,
    _NoopSearchProvider,
    _canned_phrase_ratio,
    _check_thresholds,
    _load_search_seed,
    _one_draft_distance,
    _recent_structure_similarity,
    _score_metric,
    _score_topic,
    evaluate,
    main,
)
from studio.schemas import SourceDocument

FIXTURES_DIR = Path("tests/fixtures/provider_responses")


def _load_fixture(stem: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{stem}.json").read_text(encoding="utf-8"))


def _load_rubric() -> dict:
    """Read the rubric YAML through the same loader the harness uses."""

    from studio.evaluation import _load_yaml

    return _load_yaml(Path("evaluation/rubric.yaml"))


def _draft_dict(stem: str) -> dict:
    """Turn a draft fixture into the dict shape the harness uses."""

    response = _load_fixture(stem)["responses"][0]
    paragraphs = response["paragraphs"]
    return {
        "paragraphs": paragraphs,
        "editorial_text": "\n\n".join(p["text"] for p in paragraphs),
    }


# ---------------------------------------------------------------------------
# Fix #1 — _one_draft_distance math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem",
    ["draft_valid", "draft_invalid"],
)
def test_distance_identical_drafts_is_zero(stem: str) -> None:
    """Identical drafts → distance 0.0 (per docstring contract)."""

    draft = _draft_dict(stem)
    assert _one_draft_distance(draft, draft) == 0.0


def test_distance_different_drafts_exceeds_identical() -> None:
    """A draft with different structure must outrank an identical draft."""

    draft_valid = _draft_dict("draft_valid")
    draft_invalid = _draft_dict("draft_invalid")
    identical = _one_draft_distance(draft_valid, draft_valid)
    different = _one_draft_distance(draft_valid, draft_invalid)
    assert different > identical
    assert different >= 0.5
    assert identical <= 0.1


def test_distance_uses_recorded_fixtures_not_synthesized_text() -> None:
    """Sanity check: both fixtures really do differ on at least one feature."""

    draft_valid = _draft_dict("draft_valid")
    draft_invalid = _draft_dict("draft_invalid")
    assert draft_valid != draft_invalid


# ---------------------------------------------------------------------------
# _recent_structure_similarity behaves like a distance
# ---------------------------------------------------------------------------


def test_recent_structure_similarity_returns_distance_for_identical() -> None:
    """Recent drafts identical to current → similarity (distance) near 0.0."""

    draft = _draft_dict("draft_valid")
    similarity = _recent_structure_similarity(draft, [draft, draft])
    assert similarity == 0.0


def test_recent_structure_similarity_returns_one_for_no_recent() -> None:
    """No recent drafts → no duplication risk → 1.0 (best, per docstring)."""

    draft = _draft_dict("draft_valid")
    assert _recent_structure_similarity(draft, []) == 1.0


# ---------------------------------------------------------------------------
# Fix #2 — _score_metric consumes auto_fields
# ---------------------------------------------------------------------------


def test_score_metric_default_is_neutral() -> None:
    """Unknown metric_id returns the neutral 3.0 baseline."""

    assert _score_metric("unknown_metric", {}) == 3.0


def test_score_metric_willing_to_continue_combines_hooks() -> None:
    """Both hooks → top score; one hook → mid; neither → low."""

    full = {"ho_ok_in_first_paragraph": 1.0, "tail_leaves_hook": 1.0}
    head_only = {"ho_ok_in_first_paragraph": 1.0, "tail_leaves_hook": 0.0}
    tail_only = {"ho_ok_in_first_paragraph": 0.0, "tail_leaves_hook": 1.0}
    neither = {"ho_ok_in_first_paragraph": 0.0, "tail_leaves_hook": 0.0}
    auto = {"willing_to_continue": full}
    assert _score_metric("willing_to_continue", auto) == 5.0
    auto["willing_to_continue"] = head_only
    assert _score_metric("willing_to_continue", auto) == 3.0
    auto["willing_to_continue"] = tail_only
    assert _score_metric("willing_to_continue", auto) == 3.0
    auto["willing_to_continue"] = neither
    assert _score_metric("willing_to_continue", auto) == 2.0


def test_score_metric_investigation_question_clear_combines_signals() -> None:
    """Both signals → 5; one → 4; neither → 2."""

    auto = {"investigation_question_clear": {
        "investigation_question_appears_in_first_paragraph": 1.0,
        "consistency_with_pitch_set": 1.0,
    }}
    assert _score_metric("investigation_question_clear", auto) == 5.0
    auto["investigation_question_clear"] = {
        "investigation_question_appears_in_first_paragraph": 1.0,
        "consistency_with_pitch_set": 0.0,
    }
    assert _score_metric("investigation_question_clear", auto) == 4.0
    auto["investigation_question_clear"] = {
        "investigation_question_appears_in_first_paragraph": 0.0,
        "consistency_with_pitch_set": 0.0,
    }
    assert _score_metric("investigation_question_clear", auto) == 2.0


def test_score_metric_each_paragraph_advances_uses_density() -> None:
    """Score climbs with new_information_density and bumps on zero overlap."""

    dense = {"each_paragraph_advances": {
        "paragraph_overlap_ratio": 0.0,
        "new_information_density": 100.0,
    }}
    sparse = {"each_paragraph_advances": {
        "paragraph_overlap_ratio": 0.0,
        "new_information_density": 10.0,
    }}
    dense_score = _score_metric("each_paragraph_advances", dense)
    sparse_score = _score_metric("each_paragraph_advances", sparse)
    assert dense_score > sparse_score
    assert dense_score == 5.0
    assert sparse_score <= 3.0


def test_score_metric_explains_mechanism_combines_causal_and_coverage() -> None:
    """Causal chain + fact-card coverage both strong → top score."""

    strong = {"explains_mechanism": {
        "causal_chain_token_count": 5.0,
        "fact_card_anchoring": 1.0,
    }}
    weak = {"explains_mechanism": {
        "causal_chain_token_count": 0.0,
        "fact_card_anchoring": 0.0,
    }}
    assert _score_metric("explains_mechanism", strong) == 5.0
    assert _score_metric("explains_mechanism", weak) == 2.0


def test_score_metric_canned_phrases_buckets_hits() -> None:
    """0 hits → 5; 1–2 hits → 3; ≥3 hits → 1."""

    auto = {"canned_phrases_or_reversals": {"canned_phrase_hits": 0.0}}
    assert _score_metric("canned_phrases_or_reversals", auto) == 5.0
    auto["canned_phrases_or_reversals"] = {"canned_phrase_hits": 2.0}
    assert _score_metric("canned_phrases_or_reversals", auto) == 3.0
    auto["canned_phrases_or_reversals"] = {"canned_phrase_hits": 4.0}
    assert _score_metric("canned_phrases_or_reversals", auto) == 1.0


def test_score_metric_voice_combines_hedge_and_judgement() -> None:
    """Low hedge + judgement phrase → top score."""

    healthy = {"voice_of_person_with_judgment": {
        "hedge_word_ratio": 0.001,
        "judgement_phrase_count": 2.0,
    }}
    hedgy = {"voice_of_person_with_judgment": {
        "hedge_word_ratio": 0.05,
        "judgement_phrase_count": 0.0,
    }}
    assert _score_metric("voice_of_person_with_judgment", healthy) == 5.0
    assert _score_metric("voice_of_person_with_judgment", hedgy) == 2.0


def test_score_metric_core_facts_credible_uses_coverage_and_unverified() -> None:
    """Full coverage + zero unverified high-risk → top score."""

    pristine = {"core_facts_credible": {
        "claim_verification_coverage": 1.0,
        "high_risk_unverified_count": 0.0,
    }}
    leaky = {"core_facts_credible": {
        "claim_verification_coverage": 0.0,
        "high_risk_unverified_count": 5.0,
    }}
    assert _score_metric("core_facts_credible", pristine) == 5.0
    assert _score_metric("core_facts_credible", leaky) == 2.0


def test_score_metric_structure_novelty_maps_similarity_to_score() -> None:
    """identical→1, different→5 (rubric anchors), formula unchanged."""

    # similarity=0 means identical to recent drafts → low novelty.
    assert _score_metric("structure_novelty", {}, similarity=0.0) == 1.0
    # similarity=1 means totally different from recent → high novelty.
    assert _score_metric("structure_novelty", {}, similarity=1.0) == 5.0


# ---------------------------------------------------------------------------
# _canned_phrase_ratio
# ---------------------------------------------------------------------------


def test_canned_phrase_ratio_normalises_by_paragraph_count() -> None:
    assert _canned_phrase_ratio(0, 5) == 0.0
    assert _canned_phrase_ratio(1, 5) == pytest.approx(0.2)
    assert _canned_phrase_ratio(0, 0) == 0.0  # guard against zero division


# ---------------------------------------------------------------------------
# Fix #3 — _check_thresholds flips across boundaries
# ---------------------------------------------------------------------------


THRESHOLDS = {
    "min_average_score": 3.5,
    "max_canned_phrase_ratio": 0.10,
    "min_three_pitch_difference_rate": 0.90,
    "min_claim_verification_coverage": 1.0,
    "min_protected_span_preservation_rate": 1.0,
    "max_speech_plan_mutation_rate": 0.0,
}


def _passing_kwargs() -> dict:
    return {
        "average_score": 4.0,
        "canned_phrase_ratio": 0.0,
        "pitch_difference_rate": 1.0,
        "claim_verification_coverage": 1.0,
        "protected_span_preserved": True,
        "speech_plan_mutation": False,
    }


def test_check_thresholds_passes_when_all_gates_met() -> None:
    assert _check_thresholds(thresholds=THRESHOLDS, **_passing_kwargs()) is True


@pytest.mark.parametrize(
    "kwarg, value",
    [
        ("average_score", 3.0),  # below min_average_score 3.5
        ("canned_phrase_ratio", 0.5),  # above max_canned_phrase_ratio 0.10
        ("pitch_difference_rate", 0.5),  # below min_three_pitch_difference_rate 0.90
        ("claim_verification_coverage", 0.5),  # below min_claim_verification_coverage 1.0
        ("protected_span_preserved", False),  # below min_protected_span_preservation_rate 1.0
        ("speech_plan_mutation", True),  # above max_speech_plan_mutation_rate 0.0
    ],
)
def test_check_thresholds_flips_on_each_gate(kwarg: str, value: object) -> None:
    """Each gate independently flips passes_thresholds."""

    kwargs = _passing_kwargs()
    kwargs[kwarg] = value
    assert _check_thresholds(thresholds=THRESHOLDS, **kwargs) is False


# ---------------------------------------------------------------------------
# _score_topic wires auto_fields into per-topic passes_thresholds
# ---------------------------------------------------------------------------


def _pipeline_result_from_fixture(stem: str, draft_stem: str = "draft_valid") -> object:
    from studio.evaluation import PipelineResult

    research = _load_fixture("research_valid")["responses"][1]
    pitch_set = {
        "pitches": _load_fixture("pitches_valid")["responses"],
    }
    narrative = _load_fixture("narrative_valid")["responses"][0]
    draft = _draft_dict(draft_stem)
    approved = {"editorial_text": draft["editorial_text"]}
    return PipelineResult(
        topic_id=stem,
        topic="topic",
        category="",
        project_id="proj",
        diagnosis=_load_fixture("diagnosis_valid")["responses"][0],
        research=research,
        pitch_set=pitch_set,
        narrative=narrative,
        draft=draft,
        approved=approved,
    )


def test_score_topic_includes_passes_thresholds_and_canned_phrase_ratio() -> None:
    """End-to-end: scored result carries the §11.3 gate fields."""

    import random

    result = _pipeline_result_from_fixture("topic-pass")
    real_rubric = _load_rubric()
    rubric = {
        "dimensions": real_rubric["dimensions"],
        "acceptance_thresholds": THRESHOLDS,
        "blind_randomization": {"enabled": False, "replacement_label_pool": ["A"]},
    }
    scored = _score_topic(result, rubric, [], random.Random(0))
    assert "passes_thresholds" in scored
    assert "canned_phrase_ratio" in scored
    assert isinstance(scored["passes_thresholds"], bool)
    assert isinstance(scored["canned_phrase_ratio"], float)
    assert len(scored["dimensions"]) == 8


# ---------------------------------------------------------------------------
# evaluate() → results.json + ballot.csv surface the threshold gate
# ---------------------------------------------------------------------------


@pytest.fixture
def eval_output(tmp_path: Path) -> Path:
    """Run the full offline pipeline once and yield the output directory."""

    rubric_path = Path("evaluation/rubric.yaml")
    # Use a single-topic dataset so this stays fast.
    topics_path_copy = tmp_path / "topics.yaml"
    topics_doc = {"topics": [{"id": "t1", "topic": "西瓜为什么不用来制糖", "category": "自然机制"}]}
    topics_path_copy.write_text(
        json.dumps(topics_doc, ensure_ascii=False), encoding="utf-8"
    )
    out_dir = tmp_path / "out"
    evaluate(
        topics_path=topics_path_copy,
        rubric_path=rubric_path,
        fixtures_dir=FIXTURES_DIR,
        output_dir=out_dir,
    )
    return out_dir


def test_evaluate_writes_aggregate_pass_rate(eval_output: Path) -> None:
    """results.json envelope echoes acceptance_thresholds + aggregate_pass_rate."""

    envelope = json.loads((eval_output / "results.json").read_text(encoding="utf-8"))
    assert "acceptance_thresholds" in envelope
    assert "aggregate_pass_rate" in envelope
    assert envelope["aggregate_pass_rate"] >= 0.0
    assert envelope["acceptance_thresholds"]["min_average_score"] == 3.5
    assert envelope["results"], "expected at least one scored topic"


def test_evaluate_ballot_csv_has_passes_thresholds_column(eval_output: Path) -> None:
    """ballot.csv has the per-topic passes_thresholds column."""

    with (eval_output / "ballot.csv").open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        assert "passes_thresholds" in header
        assert "canned_phrase_ratio" in header
        rows = list(reader)
    assert len(rows) == 1


def test_main_exits_one_when_any_topic_fails_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI gate: exit code reflects the §11.3 aggregate pass rate."""

    topics_doc = {"topics": [{"id": "t1", "topic": "x", "category": ""}]}
    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text(json.dumps(topics_doc, ensure_ascii=False), encoding="utf-8")
    rubric_path = Path("evaluation/rubric.yaml")
    out_dir = tmp_path / "out"
    rc = main(
        [
            "--dataset",
            str(topics_path),
            "--rubric",
            str(rubric_path),
            "--fixtures",
            str(FIXTURES_DIR),
            "--output",
            str(out_dir),
        ]
    )
    envelope = json.loads(
        (out_dir / "results.json").read_text(encoding="utf-8")
    )
    # The CLI exit code must equal whether every topic passed. Shipped
    # fixtures + shipped seed + shipped rubric all pass today, so the
    # CLI exits 0; if a future fixture breaks a gate the assert still
    # captures the regression.
    if envelope["aggregate_pass_rate"] < 1.0:
        assert rc == 1
    else:
        assert rc == 0


def test_main_exits_zero_when_aggregate_pass_rate_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI gate: aggregate_pass_rate == 1.0 → exit 0."""

    topics_doc = {"topics": [{"id": "t1", "topic": "x", "category": ""}]}
    topics_path = tmp_path / "topics.yaml"
    topics_path.write_text(json.dumps(topics_doc, ensure_ascii=False), encoding="utf-8")
    rc = main(
        [
            "--dataset",
            str(topics_path),
            "--rubric",
            "evaluation/rubric.yaml",
            "--fixtures",
            str(FIXTURES_DIR),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    envelope = json.loads(
        (tmp_path / "out" / "results.json").read_text(encoding="utf-8")
    )
    assert envelope["aggregate_pass_rate"] == 1.0
    assert rc == 0


# ---------------------------------------------------------------------------
# Fix A/B/C — seeded search provider + shipped-fixtures closure
# ---------------------------------------------------------------------------


def test_noop_search_returns_recorded_sources_for_seeded_claims() -> None:
    """Recorded claims return the seeded sources in order."""

    doc = SourceDocument(
        title="t", url="https://x/y", snippet="s", publisher="p"
    )
    provider = _NoopSearchProvider(recorded={"foo": [doc]})
    assert provider.search("foo") == [doc]


def test_noop_search_returns_empty_for_unseeded_queries() -> None:
    """Unseeded queries stay empty so the harness still exercises the
    'search returned nothing' path on any claim the fixtures missed."""

    provider = _NoopSearchProvider(
        recorded={"foo": [SourceDocument(title="t", url="u", snippet="s", publisher="p")]}
    )
    assert provider.search("bar") == []


def test_noop_search_respects_limit() -> None:
    """``search`` truncates the recorded list to ``limit``."""

    docs = [
        SourceDocument(title=f"t{i}", url=f"https://x/{i}", snippet="s", publisher="p")
        for i in range(5)
    ]
    provider = _NoopSearchProvider(recorded={"foo": docs})
    assert len(provider.search("foo", limit=2)) == 2
    assert len(provider.search("foo", limit=10)) == 5
    assert provider.search("foo", limit=0) == []


def test_load_search_seed_returns_empty_dict_when_file_missing(
    tmp_path: Path,
) -> None:
    """Missing seed file → empty mapping (the gap surfaces as a threshold
    failure, not a hard exception)."""

    assert _load_search_seed(tmp_path) == {}


def test_load_search_seed_parses_fixture_into_source_documents() -> None:
    """Shipped ``search_seed.json`` parses into ``SourceDocument`` instances."""

    seed = _load_search_seed(FIXTURES_DIR)
    # One entry per high_risk_claim from research_valid.json.
    assert "西瓜含糖量约 6-8%" in seed
    assert "甘蔗含糖量约 13-15%" in seed
    assert "西瓜收获周期短，亩产约 3000 公斤" in seed
    for claim, docs in seed.items():
        assert len(docs) >= 2, f"need ≥2 sources per claim, got {len(docs)} for {claim!r}"
        assert all(isinstance(doc, SourceDocument) for doc in docs)


def test_evaluate_minimal_fixtures_dir_yields_full_coverage(
    tmp_path: Path,
) -> None:
    """End-to-end: a fixtures dir built from a minimal ``research_valid``
    + matching ``search_seed`` returns ``claim_verification_coverage=1.0``.

    Copies the shipped fixtures to a tmp dir so every other stage still
    has its answers, then overrides ``research_valid.json`` + adds
    ``search_seed.json``. The minimal research has one high-risk claim
    with two seeded sources, so the coverage denominator is 1 and the
    numerator is 1.
    """

    # Copy shipped fixtures to a tmp dir so the rest of the pipeline
    # (diagnosis, pitches, narrative, draft, rewrite, speech) still has
    # its answers; only research + search are overridden.
    import shutil

    fixtures_dir = tmp_path / "fixtures"
    shutil.copytree(FIXTURES_DIR, fixtures_dir)

    minimal_research = {
        "operation": "research",
        "responses": [
            {
                "candidate_facts": ["x西瓜糖度"],
                "high_risk_claims": ["x西瓜糖度"],
                "mechanisms": ["m"],
                "people_events": [],
                "concrete_scenes": [],
                "visual_details": [],
                "uncertainties": [],
            },
            {
                "classifications": [
                    {
                        "claim": "x西瓜糖度",
                        "risk": "number",
                        "softened_claim": None,
                        "confidence": 0.9,
                        "narrative_value": "v",
                        "payoff_critical": True,
                    }
                ]
            },
        ],
    }
    (fixtures_dir / "research_valid.json").write_text(
        json.dumps(minimal_research, ensure_ascii=False), encoding="utf-8"
    )

    seed = {
        "x西瓜糖度": [
            {"title": "t1", "url": "https://x/1", "snippet": "s", "publisher": "p"},
            {"title": "t2", "url": "https://x/2", "snippet": "s", "publisher": "p"},
        ]
    }
    (fixtures_dir / "search_seed.json").write_text(
        json.dumps(seed, ensure_ascii=False), encoding="utf-8"
    )

    topics = tmp_path / "topics.yaml"
    topics.write_text(
        json.dumps(
            {"topics": [{"id": "t-min", "topic": "西瓜甜度", "category": "自然机制"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    evaluate(
        topics_path=topics,
        rubric_path=Path("evaluation/rubric.yaml"),
        fixtures_dir=fixtures_dir,
        output_dir=out_dir,
    )

    envelope = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    assert envelope["results"][0]["claim_verification_coverage"] == 1.0


def test_cli_exits_zero_on_shipped_fixtures(tmp_path: Path) -> None:
    """Closing test: the shipped fixtures + shipped rubric + shipped
    search_seed produce ``aggregate_pass_rate=1.0`` so the CLI exits 0.

    This is the exact gap flagged in the Task 13 fix report — before
    the seeded search provider, every topic reported coverage 0.0 and
    the CLI exited 1.
    """

    rc = main(
        [
            "--dataset",
            str(Path("evaluation/topics.yaml")),
            "--rubric",
            str(Path("evaluation/rubric.yaml")),
            "--fixtures",
            str(FIXTURES_DIR),
            "--output",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 0
    envelope = json.loads(
        (tmp_path / "out" / "results.json").read_text(encoding="utf-8")
    )
    assert envelope["aggregate_pass_rate"] == 1.0


# Silence unused import warning for the project's reference constant.
_ = DEFAULT_OUTPUT_DIR