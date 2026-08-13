from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from polytrading.ai.cli import build_parser
from polytrading.ai.corpus import (
    ContractImport,
    CorpusContract,
    canonicalize_rule_text,
    freeze_manifest,
    hash_raw_text,
    import_contract_rows,
    item_input_hash,
    preregister_corpus,
    validate_corpus,
    validate_split_integrity,
    write_imported_contracts,
)
from polytrading.ai.models import GoldRelationship
from polytrading.ai.review import ReviewRecord, resolve_reviews, validate_review_append

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def contract(contract_id: str, split: str, **overrides: object) -> CorpusContract:
    values: dict[str, object] = {
        "schema_version": 1,
        "contract_id": contract_id,
        "source_url": f"https://example.test/{contract_id}",
        "source_retrieved_at": NOW,
        "information_cutoff": NOW,
        "raw_text": f"Rules for {contract_id}",
        "raw_text_hash": hashlib.sha256(f"Rules for {contract_id}".encode()).hexdigest(),
        "canonical_text": f"Rules for {contract_id}",
        "canonical_text_hash": hashlib.sha256(f"Rules for {contract_id}".encode()).hexdigest(),
        "event_family": contract_id,
        "sampling_stratum": "synthetic",
        "split": split,
        "rule_template": "binary_threshold",
        "provenance": ("synthetic unit-test fixture",),
        "revision_of": None,
        "derivative_of": None,
    }
    values.update(overrides)
    return CorpusContract(**values)


def review(
    review_id: str,
    reviewer_id: str,
    proposed_label_hash: str,
    *,
    role: str = "reviewer",
    item_type: str = "contract",
    item_id: str = "contract-001",
    input_hash: str = HASH_A,
) -> ReviewRecord:
    return ReviewRecord(
        schema_version=1,
        review_id=review_id,
        item_type=item_type,
        item_id=item_id,
        reviewer_id=reviewer_id,
        reviewer_role=role,
        input_hash=input_hash,
        proposed_label_hash=proposed_label_hash,
        decision="accept",
        corrections_json=None,
        reviewed_at=NOW,
    )


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def test_import_requires_source_times_exact_rule_template_and_provenance() -> None:
    complete = {
        "schema_version": 1,
        "contract_id": "contract-001",
        "source_url": "https://example.test/contract-001",
        "source_retrieved_at": NOW,
        "information_cutoff": NOW,
        "raw_text": "Will BTC close above 100?",
        "event_family": "btc-close-2026-08-12",
        "sampling_stratum": "threshold",
        "split": "train",
        "rule_template": "binary_threshold",
        "provenance": ("public rules page",),
    }

    assert ContractImport(**complete).revision_of is None

    for required in (
        "source_url",
        "source_retrieved_at",
        "information_cutoff",
        "raw_text",
        "event_family",
        "rule_template",
        "provenance",
    ):
        with pytest.raises(ValidationError):
            ContractImport(**{key: value for key, value in complete.items() if key != required})


def test_import_allows_retrieval_at_or_before_information_cutoff() -> None:
    common = {
        "schema_version": 1,
        "contract_id": "contract-001",
        "source_url": "https://example.test/contract-001",
        "information_cutoff": NOW,
        "raw_text": "Rule text",
        "event_family": "event-001",
        "sampling_stratum": "threshold",
        "split": "train",
        "rule_template": "binary_threshold",
        "provenance": ("public rules page",),
    }

    assert ContractImport(**(common | {"source_retrieved_at": NOW})).source_retrieved_at == NOW
    before = NOW.replace(hour=11)
    imported = ContractImport(**(common | {"source_retrieved_at": before}))
    assert imported.source_retrieved_at == before


def test_import_rejects_source_retrieved_after_information_cutoff() -> None:
    with pytest.raises(
        ValidationError, match="source retrieval must not follow information cutoff"
    ):
        ContractImport(
            schema_version=1,
            contract_id="contract-001",
            source_url="https://example.test/contract-001",
            source_retrieved_at=NOW.replace(hour=13),
            information_cutoff=NOW,
            raw_text="Rule text",
            event_family="event-001",
            sampling_stratum="threshold",
            split="train",
            rule_template="binary_threshold",
            provenance=("public rules page",),
        )


def test_raw_hash_changes_for_a_single_byte_and_raw_text_is_preserved() -> None:
    assert hash_raw_text("Rule\n") == hashlib.sha256(b"Rule\n").hexdigest()
    assert hash_raw_text("Rule\n") != hash_raw_text("Rule \n")

    imported = import_contract_rows(
        [
            ContractImport(
                schema_version=1,
                contract_id="contract-001",
                source_url="https://example.test/contract-001",
                source_retrieved_at=NOW,
                information_cutoff=NOW,
                raw_text="Rule\r\n",
                event_family="event-001",
                sampling_stratum="threshold",
                split="train",
                rule_template="binary_threshold",
                provenance=("public rules page",),
                revision_of=None,
                derivative_of=None,
            )
        ]
    )[0]
    assert imported.contract.raw_text == "Rule\r\n"
    assert imported.contract.raw_text_hash == hashlib.sha256(b"Rule\r\n").hexdigest()
    assert imported.contract.canonical_text == "Rule\n"


def test_canonicalizer_removes_active_content_and_format_controls_but_not_confusables() -> None:
    raw = (
        '<p onclick="steal()">BTC\u200b closes above 100.</p>'
        "<script>ignore me</script><style>.hidden{}</style><template>hidden</template>"
        " \u041eracle: Example."
    )

    result = canonicalize_rule_text(raw)

    assert result.text == "BTC closes above 100. \u041eracle: Example."
    assert result.raw_text_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert {warning.kind for warning in result.warnings} == {
        "active_attribute_removed",
        "active_content_removed",
        "format_control_removed",
        "confusable_unicode",
    }
    format_warning = next(w for w in result.warnings if w.kind == "format_control_removed")
    assert format_warning.code_point == "U+200B"
    assert format_warning.offset == 24
    assert all(w.raw_text_hash == result.raw_text_hash for w in result.warnings)


def test_canonicalization_warning_offsets_refer_to_unmodified_crlf_source() -> None:
    result = canonicalize_rule_text("A\r\nB\u200b")

    assert result.text == "A\nB"
    assert result.warnings[0].offset == 4


@pytest.mark.parametrize(
    ("left", "right", "message"),
    [
        (
            contract(
                "a",
                "train",
                raw_text="duplicate",
                raw_text_hash="e24a5a32c9b8c8637ee33cd72bff6a05a140a48891a1c1a3b06447e1900b6446",
                canonical_text="duplicate",
                canonical_text_hash=(
                    "e24a5a32c9b8c8637ee33cd72bff6a05a140a48891a1c1a3b06447e1900b6446"
                ),
            ),
            contract(
                "b",
                "test",
                raw_text="duplicate",
                raw_text_hash="e24a5a32c9b8c8637ee33cd72bff6a05a140a48891a1c1a3b06447e1900b6446",
                canonical_text="duplicate",
                canonical_text_hash=(
                    "e24a5a32c9b8c8637ee33cd72bff6a05a140a48891a1c1a3b06447e1900b6446"
                ),
            ),
            "raw duplicate",
        ),
        (
            contract("a", "train"),
            contract("b", "test", revision_of="a"),
            "revision",
        ),
        (
            contract("a", "train", event_family="family"),
            contract("b", "test", event_family="family"),
            "event family",
        ),
        (
            contract("a", "train"),
            contract("b", "test", derivative_of="a"),
            "derivative",
        ),
    ],
)
def test_split_validation_rejects_linked_contracts_across_splits(
    left: CorpusContract, right: CorpusContract, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_split_integrity((left, right), ())


def test_relationship_members_must_exist_and_share_the_relationship_split() -> None:
    contracts = (contract("a", "train"), contract("b", "test"))
    cross_split = GoldRelationship(
        schema_version=1,
        relationship_id="rel-001",
        member_contract_ids=("a", "b"),
        split="train",
    )
    with pytest.raises(ValueError, match="relationship members"):
        validate_split_integrity(contracts, (cross_split,))

    missing = GoldRelationship(
        schema_version=1,
        relationship_id="rel-002",
        member_contract_ids=("a", "missing"),
        split="train",
    )
    with pytest.raises(ValueError, match="missing contract"):
        validate_split_integrity(contracts, (missing,))


@pytest.mark.parametrize(
    ("field", "tampered", "message"),
    [
        ("raw_text_hash", HASH_A, "raw text hash"),
        ("canonical_text", "tampered canonical text", "canonical text"),
        ("canonical_text_hash", HASH_B, "canonical text hash"),
    ],
)
def test_corpus_validation_recomputes_derived_text_fields(
    tmp_path: Path, field: str, tampered: str, message: str
) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    row = contract("a", "train").model_dump(mode="json")
    row[field] = tampered
    write_jsonl(gold / "contracts.jsonl", [row])
    write_jsonl(gold / "relationships.jsonl", [])
    write_jsonl(gold / "labels.jsonl", [])
    write_jsonl(gold / "reviews.jsonl", [])

    with pytest.raises(ValueError, match=message):
        validate_corpus(gold)


def test_freeze_rejects_review_hash_for_different_contract_content(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    item = contract("contract-001", "train")
    write_jsonl(gold / "contracts.jsonl", [item.model_dump(mode="json")])
    write_jsonl(gold / "relationships.jsonl", [])
    write_jsonl(gold / "labels.jsonl", [])
    write_jsonl(
        gold / "reviews.jsonl",
        [
            review("review-001", "alice", HASH_B, input_hash=HASH_A).model_dump(mode="json"),
            review("review-002", "bob", HASH_B, input_hash=HASH_A).model_dump(mode="json"),
        ],
    )

    assert item_input_hash(item) != HASH_A
    with pytest.raises(ValueError, match="input hash does not match contract"):
        freeze_manifest(gold, created_at=NOW)


def test_validation_rejects_review_hash_for_different_relationship_membership(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    items = (contract("a", "train"), contract("b", "train"))
    relationship = GoldRelationship(
        schema_version=1,
        relationship_id="relationship-001",
        member_contract_ids=("a", "b"),
        split="train",
    )
    write_jsonl(gold / "contracts.jsonl", [item.model_dump(mode="json") for item in items])
    write_jsonl(gold / "relationships.jsonl", [relationship.model_dump(mode="json")])
    write_jsonl(gold / "labels.jsonl", [])
    write_jsonl(
        gold / "reviews.jsonl",
        [
            review(
                "review-001",
                "alice",
                HASH_B,
                item_type="relationship",
                item_id="relationship-001",
                input_hash=HASH_A,
            ).model_dump(mode="json"),
            review(
                "review-002",
                "bob",
                HASH_B,
                item_type="relationship",
                item_id="relationship-001",
                input_hash=HASH_A,
            ).model_dump(mode="json"),
        ],
    )

    assert item_input_hash(relationship) != HASH_A
    with pytest.raises(ValueError, match="input hash does not match relationship"):
        validate_corpus(gold, require_reviews=True)


def test_reviewer_cannot_review_one_item_under_two_review_ids() -> None:
    prior = review("review-001", "alice", HASH_A)
    with pytest.raises(ValueError, match="already reviewed"):
        validate_review_append((prior,), review("review-002", "alice", HASH_A))


def test_equal_independent_reviews_close_without_adjudication() -> None:
    resolution = resolve_reviews(
        (review("review-001", "alice", HASH_B), review("review-002", "bob", HASH_B))
    )
    assert resolution.complete is True
    assert resolution.proposed_label_hash == HASH_B
    assert resolution.adjudication_id is None


def test_disagreement_requires_distinct_adjudicator() -> None:
    reviews = (review("review-001", "alice", HASH_A), review("review-002", "bob", HASH_B))
    assert resolve_reviews(reviews).complete is False
    with pytest.raises(ValueError, match="distinct from both reviewers"):
        resolve_reviews((*reviews, review("review-003", "alice", HASH_B, role="adjudicator")))

    resolution = resolve_reviews(
        (*reviews, review("review-003", "carol", HASH_B, role="adjudicator"))
    )
    assert resolution.complete is True
    assert resolution.adjudication_id == "review-003"
    assert resolution.proposed_label_hash == HASH_B


def test_preregister_is_deterministic_and_never_claims_human_completion(tmp_path: Path) -> None:
    policy = {
        "schema_version": 1,
        "dataset_prefix": "semantic-scout",
        "information_cutoff": "2026-08-12T12:00:00Z",
        "sampling_counts": {"contracts": 2, "relationships": 1},
        "split_policy": {"train": 1, "validation": 0, "test": 1},
        "reviewer_roles": {"reviewers_per_item": 2, "adjudicators_per_disagreement": 1},
        "template_taxonomy": ["binary_threshold", "complement"],
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))

    first = preregister_corpus(policy_path, tmp_path / "gold-a")
    second = preregister_corpus(policy_path, tmp_path / "gold-b")

    assert [row.model_dump(mode="json") for row in first] == [
        row.model_dump(mode="json") for row in second
    ]
    assert len(first) == 12  # 2 contract + 4 review + 1 relationship + 2 review + 3 queues
    assert all(row.status == "pending" for row in first)


def test_preregister_accepts_policy_in_destination_and_replaces_only_empty_progress(
    tmp_path: Path,
) -> None:
    policy = {
        "schema_version": 1,
        "dataset_prefix": "semantic-scout",
        "information_cutoff": "2026-08-12T12:00:00Z",
        "sampling_counts": {"contracts": 1, "relationships": 0},
        "split_policy": {"train": 1, "validation": 0, "test": 0},
        "reviewer_roles": {"reviewers_per_item": 2, "adjudicators_per_disagreement": 1},
        "template_taxonomy": ["binary_threshold"],
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n")
    (tmp_path / "progress.jsonl").write_bytes(b"")

    first = preregister_corpus(policy_path, tmp_path)
    second = preregister_corpus(policy_path, tmp_path)

    assert first == second
    assert len((tmp_path / "progress.jsonl").read_text().splitlines()) == 4


def test_freeze_is_content_addressed_atomic_and_never_mutates_a_frozen_manifest(
    tmp_path: Path,
) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    rows = [contract("a", "train").model_dump(mode="json")]
    write_jsonl(gold / "contracts.jsonl", rows)
    write_jsonl(gold / "relationships.jsonl", [])
    write_jsonl(gold / "labels.jsonl", [{"item_id": "a", "label_version": 1}])
    write_jsonl(gold / "reviews.jsonl", [])

    first = freeze_manifest(gold, created_at=NOW, require_reviews=False)
    first_path = gold / "manifests" / f"{first.dataset_id}.json"
    first_bytes = first_path.read_bytes()
    assert first.frozen is True
    assert not list(gold.rglob("*.tmp"))
    assert freeze_manifest(gold, created_at=NOW.replace(hour=13), require_reviews=False) == first

    write_jsonl(gold / "labels.jsonl", [{"item_id": "a", "label_version": 2}])
    second = freeze_manifest(gold, created_at=NOW, require_reviews=False)

    assert second.dataset_id != first.dataset_id
    assert first_path.read_bytes() == first_bytes
    assert (gold / "manifests" / f"{second.dataset_id}.json").exists()


def test_freeze_replaces_an_unfrozen_placeholder_once(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    write_jsonl(gold / "contracts.jsonl", [contract("a", "train").model_dump(mode="json")])
    write_jsonl(gold / "relationships.jsonl", [])
    write_jsonl(gold / "labels.jsonl", [])
    write_jsonl(gold / "reviews.jsonl", [])
    (gold / "manifest.json").write_text('{"frozen": false}\n')

    manifest = freeze_manifest(gold, created_at=NOW, require_reviews=False)

    active = json.loads((gold / "manifest.json").read_text())
    assert active["frozen"] is True
    assert active["dataset_id"] == manifest.dataset_id


def test_freeze_rejects_unresolved_reviews_and_split_leakage(tmp_path: Path) -> None:
    gold = tmp_path / "gold"
    gold.mkdir()
    duplicate_text = "exact duplicate source bytes"
    duplicate_hash = hashlib.sha256(duplicate_text.encode()).hexdigest()
    rows = [
        contract(
            "a",
            "train",
            raw_text=duplicate_text,
            raw_text_hash=duplicate_hash,
            canonical_text=duplicate_text,
            canonical_text_hash=duplicate_hash,
        ).model_dump(mode="json"),
        contract(
            "b",
            "test",
            raw_text=duplicate_text,
            raw_text_hash=duplicate_hash,
            canonical_text=duplicate_text,
            canonical_text_hash=duplicate_hash,
        ).model_dump(mode="json"),
    ]
    write_jsonl(gold / "contracts.jsonl", rows)
    write_jsonl(gold / "relationships.jsonl", [])
    write_jsonl(gold / "labels.jsonl", [])
    write_jsonl(gold / "reviews.jsonl", [])

    with pytest.raises(ValueError, match="raw duplicate"):
        freeze_manifest(gold, created_at=NOW)


def test_contract_import_append_preserves_existing_bytes_and_exact_retry_is_noop(
    tmp_path: Path,
) -> None:
    output = tmp_path / "contracts.jsonl"
    first = import_contract_rows(
        (
            ContractImport(
                schema_version=1,
                contract_id="contract-002",
                source_url="https://example.test/contract-002",
                source_retrieved_at=NOW,
                information_cutoff=NOW,
                raw_text="Second contract",
                event_family="event-002",
                sampling_stratum="threshold",
                split="train",
                rule_template="binary_threshold",
                provenance=("public rules page",),
            ),
        )
    )
    second = import_contract_rows(
        (
            ContractImport(
                schema_version=1,
                contract_id="contract-001",
                source_url="https://example.test/contract-001",
                source_retrieved_at=NOW,
                information_cutoff=NOW,
                raw_text="First contract",
                event_family="event-001",
                sampling_stratum="threshold",
                split="train",
                rule_template="binary_threshold",
                provenance=("public rules page",),
            ),
        )
    )

    write_imported_contracts(output, first)
    old_bytes = output.read_bytes()
    write_imported_contracts(output, second)
    appended_bytes = output.read_bytes()
    write_imported_contracts(output, first)

    assert appended_bytes.startswith(old_bytes)
    assert output.read_bytes() == appended_bytes


def test_contract_import_rejects_conflicting_immutable_identity(tmp_path: Path) -> None:
    output = tmp_path / "contracts.jsonl"
    original = import_contract_rows(
        (
            ContractImport(
                schema_version=1,
                contract_id="contract-001",
                source_url="https://example.test/contract-001",
                source_retrieved_at=NOW,
                information_cutoff=NOW,
                raw_text="Original contract",
                event_family="event-001",
                sampling_stratum="threshold",
                split="train",
                rule_template="binary_threshold",
                provenance=("public rules page",),
            ),
        )
    )
    conflicting = import_contract_rows(
        (
            ContractImport(
                schema_version=1,
                contract_id="contract-001",
                source_url="https://example.test/contract-001",
                source_retrieved_at=NOW,
                information_cutoff=NOW,
                raw_text="Changed contract",
                event_family="event-001",
                sampling_stratum="threshold",
                split="train",
                rule_template="binary_threshold",
                provenance=("public rules page",),
            ),
        )
    )
    write_imported_contracts(output, original)
    original_bytes = output.read_bytes()

    with pytest.raises(ValueError, match="immutable contract ID"):
        write_imported_contracts(output, conflicting)

    assert output.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "argv",
    [
        ["ai", "corpus", "preregister", "--policy", "policy.json", "--dir", "gold"],
        ["ai", "corpus", "import", "--input", "input.jsonl", "--output", "contracts.jsonl"],
        [
            "ai",
            "corpus",
            "review",
            "--item-type",
            "contract",
            "--item-id",
            "contract-0001",
            "--review-file",
            "review.json",
        ],
        [
            "ai",
            "corpus",
            "adjudicate",
            "--item-type",
            "relationship",
            "--item-id",
            "relationship-0001",
            "--review-file",
            "review.json",
        ],
        ["ai", "corpus", "validate", "--dir", "gold"],
        ["ai", "corpus", "freeze", "--dir", "gold"],
    ],
)
def test_corpus_cli_accepts_exact_command_contracts(argv: list[str]) -> None:
    parsed = build_parser().parse_args(argv)
    assert parsed.ai_command == "corpus"
