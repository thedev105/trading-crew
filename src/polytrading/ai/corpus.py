from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import StringConstraints, field_validator, model_validator

from polytrading.ai.models import (
    AIRecord,
    GoldContract,
    GoldContractLabel,
    GoldRelationship,
    GoldRelationshipLabel,
)
from polytrading.ai.review import CorpusReviewAssignment, ReviewRecord, resolve_reviews
from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

# `directory`/`path` arguments throughout this module come directly from
# operator-supplied CLI flags (e.g. --corpus, --output), not from any
# network-facing or automated caller, so no root-containment check is
# applied here (contrast corpus_intake/artifacts.py, which quarantines
# writes from an automated pipeline beneath a fixed project root). If a
# less-trusted caller ever reaches these, add the same containment check.

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
Split = Literal["train", "validation", "test"]
WarningKind = Literal[
    "active_content_removed",
    "active_attribute_removed",
    "format_control_removed",
    "confusable_unicode",
]
_ACTIVE_ELEMENTS = frozenset({"script", "style", "template"})
_ACTIVE_ATTRIBUTES = frozenset({"srcdoc", "style"})


def hash_raw_text(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


class CanonicalizationWarning(StrictRecord):
    kind: WarningKind
    raw_text_hash: str
    offset: int
    detail: NonEmptyString
    code_point: str | None = None


class CanonicalizedText(StrictRecord):
    raw_text_hash: str
    text: str
    text_hash: str
    warnings: tuple[CanonicalizationWarning, ...]


class _RuleTextParser(HTMLParser):
    def __init__(self, raw_hash: str) -> None:
        super().__init__(convert_charrefs=False)
        self.raw_hash = raw_hash
        self.parts: list[tuple[str, int]] = []
        self.warnings: list[CanonicalizationWarning] = []
        self._excluded: list[str] = []
        self._line_offsets: list[int] = [0]
        self._source = ""

    def parse(self, source: str) -> None:
        self._source = source
        self._line_offsets = [0]
        for index, character in enumerate(source):
            if character == "\n":
                self._line_offsets.append(index + 1)
        self.feed(source)
        self.close()

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in _ACTIVE_ELEMENTS:
            self._excluded.append(normalized)
            self.warnings.append(
                CanonicalizationWarning(
                    kind="active_content_removed",
                    raw_text_hash=self.raw_hash,
                    offset=self._offset(),
                    detail=f"removed {normalized} element contents",
                )
            )
            return
        for name, _ in attrs:
            attribute = name.casefold()
            if attribute.startswith("on") or attribute in _ACTIVE_ATTRIBUTES:
                self.warnings.append(
                    CanonicalizationWarning(
                        kind="active_attribute_removed",
                        raw_text_hash=self.raw_hash,
                        offset=self._offset(),
                        detail=f"removed active attribute {name}",
                    )
                )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in _ACTIVE_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._excluded:
            reverse_index = self._excluded[::-1].index(normalized)
            del self._excluded[len(self._excluded) - reverse_index - 1 :]

    def handle_data(self, data: str) -> None:
        if not self._excluded:
            self.parts.append((data, self._offset()))

    def handle_entityref(self, name: str) -> None:
        if not self._excluded:
            self.parts.append((f"&{name};", self._offset()))

    def handle_charref(self, name: str) -> None:
        if not self._excluded:
            self.parts.append((f"&#{name};", self._offset()))


def canonicalize_rule_text(raw_text: str) -> CanonicalizedText:
    raw_hash = hash_raw_text(raw_text)
    parser = _RuleTextParser(raw_hash)
    parser.parse(raw_text)
    output: list[str] = []
    warnings = list(parser.warnings)
    for text, base_offset in parser.parts:
        for relative_offset, character in enumerate(text):
            category = unicodedata.category(character)
            offset = base_offset + relative_offset
            if category == "Cf":
                warnings.append(
                    CanonicalizationWarning(
                        kind="format_control_removed",
                        raw_text_hash=raw_hash,
                        offset=offset,
                        code_point=f"U+{ord(character):04X}",
                        detail=(
                            "removed Unicode format control "
                            f"{unicodedata.name(character, 'UNKNOWN')}"
                        ),
                    )
                )
                continue
            name = unicodedata.name(character, "")
            if category.startswith("L") and ("CYRILLIC" in name or "GREEK" in name):
                warnings.append(
                    CanonicalizationWarning(
                        kind="confusable_unicode",
                        raw_text_hash=raw_hash,
                        offset=offset,
                        code_point=f"U+{ord(character):04X}",
                        detail=f"retained suspicious Unicode letter {name}",
                    )
                )
            output.append(character)
    canonical_text = "".join(output).replace("\r\n", "\n").replace("\r", "\n")
    canonical_text = unicodedata.normalize("NFC", canonical_text)
    return CanonicalizedText(
        raw_text_hash=raw_hash,
        text=canonical_text,
        text_hash=hash_raw_text(canonical_text),
        warnings=tuple(sorted(warnings, key=lambda warning: (warning.offset, warning.kind))),
    )


class ContractImport(AIRecord):
    schema_version: Literal[1]
    contract_id: NonEmptyString
    source_url: NonEmptyString
    source_retrieved_at: datetime
    information_cutoff: datetime
    raw_text: str
    event_family: NonEmptyString
    sampling_stratum: NonEmptyString
    split: Split
    rule_template: NonEmptyString
    provenance: tuple[NonEmptyString, ...]
    revision_of: NonEmptyString | None = None
    derivative_of: NonEmptyString | None = None

    @field_validator("provenance")
    @classmethod
    def require_provenance(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("import provenance must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("import provenance entries must be unique")
        return value

    @model_validator(mode="after")
    def require_lineage_not_self_referential(self) -> ContractImport:
        if self.revision_of == self.contract_id or self.derivative_of == self.contract_id:
            raise ValueError("a contract cannot derive from or revise itself")
        if self.source_retrieved_at > self.information_cutoff:
            raise ValueError("source retrieval must not follow information cutoff")
        return self


class CorpusContract(GoldContract):
    rule_template: NonEmptyString
    provenance: tuple[NonEmptyString, ...]
    revision_of: NonEmptyString | None = None
    derivative_of: NonEmptyString | None = None

    @model_validator(mode="after")
    def require_derived_text_integrity(self) -> CorpusContract:
        if self.source_retrieved_at > self.information_cutoff:
            raise ValueError("source retrieval must not follow information cutoff")
        canonical = canonicalize_rule_text(self.raw_text)
        if self.raw_text_hash != canonical.raw_text_hash:
            raise ValueError("raw text hash does not match raw text")
        if self.canonical_text != canonical.text:
            raise ValueError("canonical text does not match deterministic canonicalization")
        if self.canonical_text_hash != canonical.text_hash:
            raise ValueError("canonical text hash does not match canonical text")
        return self


def item_input_hash(item: CorpusContract | GoldRelationship) -> str:
    item_type = "contract" if isinstance(item, CorpusContract) else "relationship"
    payload = {"item_type": item_type, "item": item.model_dump(mode="json")}
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


class ImportedContract(StrictRecord):
    contract: CorpusContract
    warnings: tuple[CanonicalizationWarning, ...]


def import_contract_rows(rows: Sequence[ContractImport]) -> tuple[ImportedContract, ...]:
    imported: list[ImportedContract] = []
    seen: set[str] = set()
    for row in rows:
        if row.contract_id in seen:
            raise ValueError(f"duplicate immutable contract ID {row.contract_id!r}")
        seen.add(row.contract_id)
        canonical = canonicalize_rule_text(row.raw_text)
        imported.append(
            ImportedContract(
                contract=CorpusContract(
                    schema_version=1,
                    contract_id=row.contract_id,
                    source_url=row.source_url,
                    source_retrieved_at=row.source_retrieved_at,
                    information_cutoff=row.information_cutoff,
                    raw_text=row.raw_text,
                    raw_text_hash=canonical.raw_text_hash,
                    canonical_text=canonical.text,
                    canonical_text_hash=canonical.text_hash,
                    event_family=row.event_family,
                    sampling_stratum=row.sampling_stratum,
                    split=row.split,
                    rule_template=row.rule_template,
                    provenance=row.provenance,
                    revision_of=row.revision_of,
                    derivative_of=row.derivative_of,
                ),
                warnings=canonical.warnings,
            )
        )
    validate_split_integrity(tuple(item.contract for item in imported), ())
    return tuple(imported)


def validate_split_integrity(
    contracts: Sequence[CorpusContract], relationships: Sequence[GoldRelationship]
) -> None:
    contracts_by_id: dict[str, CorpusContract] = {}
    raw_splits: dict[str, str] = {}
    family_splits: dict[str, str] = {}
    for contract in contracts:
        if contract.contract_id in contracts_by_id:
            raise ValueError(f"duplicate immutable contract ID {contract.contract_id!r}")
        contracts_by_id[contract.contract_id] = contract
        prior_raw_split = raw_splits.setdefault(contract.raw_text_hash, contract.split)
        if prior_raw_split != contract.split:
            raise ValueError("raw duplicate contracts cannot cross splits")
        prior_family_split = family_splits.setdefault(contract.event_family, contract.split)
        if prior_family_split != contract.split:
            raise ValueError("contracts in one event family cannot cross splits")
    for contract in contracts:
        for lineage_kind, parent_id in (
            ("revision", contract.revision_of),
            ("derivative", contract.derivative_of),
        ):
            if parent_id is None:
                continue
            parent = contracts_by_id.get(parent_id)
            if parent is None:
                raise ValueError(f"{lineage_kind} references missing contract {parent_id!r}")
            if parent.split != contract.split:
                raise ValueError(f"{lineage_kind} contracts cannot cross splits")
    relationship_ids: set[str] = set()
    for relationship in relationships:
        if relationship.relationship_id in relationship_ids:
            raise ValueError(
                f"duplicate immutable relationship ID {relationship.relationship_id!r}"
            )
        relationship_ids.add(relationship.relationship_id)
        missing = set(relationship.member_contract_ids).difference(contracts_by_id)
        if missing:
            raise ValueError(
                "relationship references missing contract IDs: " + ", ".join(sorted(missing))
            )
        member_splits = {contracts_by_id[item].split for item in relationship.member_contract_ids}
        if member_splits != {relationship.split}:
            raise ValueError("relationship members must all share the relationship split")


class CorpusPolicy(AIRecord):
    schema_version: Literal[1]
    dataset_prefix: NonEmptyString
    information_cutoff: datetime
    sampling_counts: dict[Literal["contracts", "relationships"], int]
    split_policy: dict[Split, int]
    reviewer_roles: dict[Literal["reviewers_per_item", "adjudicators_per_disagreement"], int]
    template_taxonomy: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def require_complete_policy(self) -> CorpusPolicy:
        if set(self.sampling_counts) != {"contracts", "relationships"}:
            raise ValueError("sampling counts must define contracts and relationships")
        if any(isinstance(count, bool) or count < 0 for count in self.sampling_counts.values()):
            raise ValueError("sampling counts must be nonnegative integers")
        if set(self.split_policy) != {"train", "validation", "test"}:
            raise ValueError("split policy must define train, validation, and test")
        if any(isinstance(count, bool) or count < 0 for count in self.split_policy.values()):
            raise ValueError("split policy counts must be nonnegative integers")
        if sum(self.split_policy.values()) != self.sampling_counts["contracts"]:
            raise ValueError("split policy counts must sum to the contract sampling count")
        if set(self.reviewer_roles) != {
            "reviewers_per_item",
            "adjudicators_per_disagreement",
        }:
            raise ValueError("reviewer roles must define reviewer and adjudicator requirements")
        if self.reviewer_roles != {
            "reviewers_per_item": 2,
            "adjudicators_per_disagreement": 1,
        }:
            raise ValueError("policy requires two reviewers and one disagreement adjudicator")
        if not self.template_taxonomy or len(self.template_taxonomy) != len(
            set(self.template_taxonomy)
        ):
            raise ValueError("template taxonomy must be nonempty and unique")
        return self


class ProgressRow(StrictRecord):
    schema_version: Literal[1]
    unit_id: NonEmptyString
    unit_type: Literal["contract", "relationship", "review", "adjudication_queue"]
    item_type: Literal["contract", "relationship"]
    item_id: NonEmptyString
    review_slot: int | None
    status: Literal["pending"]


def preregister_corpus(policy_path: Path, directory: Path) -> tuple[ProgressRow, ...]:
    policy = CorpusPolicy.model_validate_json(policy_path.read_bytes())
    rows: list[ProgressRow] = []
    item_specs = (
        ("contract", policy.sampling_counts["contracts"]),
        ("relationship", policy.sampling_counts["relationships"]),
    )
    for item_type, count in item_specs:
        for number in range(1, count + 1):
            item_id = f"{item_type}-{number:04d}"
            rows.append(
                ProgressRow(
                    schema_version=1,
                    unit_id=item_id,
                    unit_type=item_type,
                    item_type=item_type,
                    item_id=item_id,
                    review_slot=None,
                    status="pending",
                )
            )
            for slot in range(1, 3):
                rows.append(
                    ProgressRow(
                        schema_version=1,
                        unit_id=f"review:{item_type}:{number:04d}:{slot}",
                        unit_type="review",
                        item_type=item_type,
                        item_id=item_id,
                        review_slot=slot,
                        status="pending",
                    )
                )
            rows.append(
                ProgressRow(
                    schema_version=1,
                    unit_id=f"adjudication:{item_type}:{number:04d}",
                    unit_type="adjudication_queue",
                    item_type=item_type,
                    item_id=item_id,
                    review_slot=None,
                    status="pending",
                )
            )
    directory.mkdir(parents=True, exist_ok=True)
    policy_bytes = _canonical_json(policy.model_dump(mode="json"))
    policy_destination = directory / "policy.json"
    if policy_path.resolve() != policy_destination.resolve():
        _atomic_write_immutable(policy_destination, policy_bytes)
    progress_path = directory / "progress.jsonl"
    progress_bytes = _jsonl_bytes(rows)
    if progress_path.exists() and not progress_path.read_bytes():
        atomic_write(progress_path, progress_bytes)
    else:
        _atomic_write_immutable(progress_path, progress_bytes)
    return tuple(rows)


class CorpusManifest(AIRecord):
    schema_version: Literal[1]
    dataset_id: NonEmptyString
    created_at: datetime
    information_cutoff: datetime
    file_hashes: dict[Literal["contracts", "relationships", "labels", "reviews"], str]
    split_family_hashes: dict[Split, str]
    counts: dict[str, int]
    rule_template_counts: dict[str, int]
    adversarial_tag_counts: dict[str, int]
    review_completion: dict[Literal["complete", "unresolved"], int]
    frozen: bool

    @field_validator("created_at")
    @classmethod
    def require_created_at_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)


class FrozenCorpus(StrictRecord):
    manifest: CorpusManifest
    contracts: tuple[CorpusContract, ...]
    relationships: tuple[GoldRelationship, ...]
    labels: tuple[GoldContractLabel | GoldRelationshipLabel, ...]


def load_frozen_corpus(directory: Path) -> FrozenCorpus:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise ValueError("frozen corpus manifest does not exist")
    manifest = CorpusManifest.model_validate_json(manifest_path.read_bytes())
    if not manifest.frozen:
        raise ValueError("corpus manifest is not frozen")
    paths = {
        name: directory / f"{name}.jsonl"
        for name in ("contracts", "relationships", "labels", "reviews")
    }
    for path in paths.values():
        if not path.exists():
            raise ValueError(f"required frozen corpus file does not exist: {path}")
    contract_rows = _read_jsonl(paths["contracts"])
    relationship_rows = _read_jsonl(paths["relationships"])
    label_rows = _read_jsonl(paths["labels"])
    review_rows = _read_jsonl(paths["reviews"])
    contracts = tuple(_validate_json_record(CorpusContract, row) for row in contract_rows)
    relationships = tuple(_validate_json_record(GoldRelationship, row) for row in relationship_rows)
    labels = tuple(
        _validate_json_record(
            GoldContractLabel if "contract_id" in row else GoldRelationshipLabel,
            row,
        )
        for row in label_rows
    )
    reviews = tuple(_validate_json_record(ReviewRecord, row) for row in review_rows)
    validate_split_integrity(contracts, relationships)
    completion = _review_completion(contracts, relationships, reviews)
    file_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
    }
    split_family_hashes = {
        split: hashlib.sha256(
            _canonical_json(
                sorted({contract.event_family for contract in contracts if contract.split == split})
            )
        ).hexdigest()
        for split in ("train", "validation", "test")
    }
    counts = {
        "contracts": len(contracts),
        "relationships": len(relationships),
        "labels": len(labels),
        "reviews": len(reviews),
    }
    rule_template_counts = dict(
        sorted(Counter(contract.rule_template for contract in contracts).items())
    )
    adversarial_tag_counts = dict(
        sorted(Counter(tag for label in labels for tag in label.adversarial_tags).items())
    )
    cutoff = max(
        (contract.information_cutoff for contract in contracts),
        default=_policy_cutoff(directory),
    )
    identity = {
        "schema_version": 1,
        "file_hashes": file_hashes,
        "split_family_hashes": split_family_hashes,
        "counts": counts,
        "rule_template_counts": rule_template_counts,
        "adversarial_tag_counts": adversarial_tag_counts,
        "review_completion": completion,
        "information_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
    }
    expected_dataset_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
    expected = {
        "dataset_id": expected_dataset_id,
        "information_cutoff": cutoff,
        "file_hashes": file_hashes,
        "split_family_hashes": split_family_hashes,
        "counts": counts,
        "rule_template_counts": rule_template_counts,
        "adversarial_tag_counts": adversarial_tag_counts,
        "review_completion": completion,
    }
    for field, value in expected.items():
        if getattr(manifest, field) != value:
            raise ValueError(f"frozen corpus manifest {field} does not match corpus files")
    return FrozenCorpus(
        manifest=manifest,
        contracts=contracts,
        relationships=relationships,
        labels=labels,
    )


def freeze_manifest(
    directory: Path,
    *,
    created_at: datetime | None = None,
    require_reviews: bool = True,
) -> CorpusManifest:
    paths = {
        name: directory / f"{name}.jsonl"
        for name in ("contracts", "relationships", "labels", "reviews")
    }
    for path in paths.values():
        if not path.exists():
            raise ValueError(f"required corpus file does not exist: {path}")
    contract_rows = _read_jsonl(paths["contracts"])
    relationship_rows = _read_jsonl(paths["relationships"])
    label_rows = _read_jsonl(paths["labels"])
    review_rows = _read_jsonl(paths["reviews"])
    contracts = tuple(_validate_json_record(CorpusContract, row) for row in contract_rows)
    relationships = tuple(_validate_json_record(GoldRelationship, row) for row in relationship_rows)
    labels = tuple(
        _validate_json_record(
            GoldContractLabel if "contract_id" in row else GoldRelationshipLabel,
            row,
        )
        for row in label_rows
    )
    reviews = tuple(_validate_json_record(ReviewRecord, row) for row in review_rows)
    validate_split_integrity(contracts, relationships)
    completion = _review_completion(contracts, relationships, reviews)
    if require_reviews and completion["unresolved"]:
        raise ValueError(f"cannot freeze corpus with {completion['unresolved']} unresolved reviews")
    file_hashes = {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
    }
    split_family_hashes = {}
    for split in ("train", "validation", "test"):
        families = sorted(
            {contract.event_family for contract in contracts if contract.split == split}
        )
        split_family_hashes[split] = hashlib.sha256(_canonical_json(families)).hexdigest()
    counts = {
        "contracts": len(contracts),
        "relationships": len(relationships),
        "labels": len(labels),
        "reviews": len(reviews),
    }
    rule_templates = Counter(contract.rule_template for contract in contracts)
    adversarial_tags = Counter(tag for label in labels for tag in label.adversarial_tags)
    cutoff = max(
        (contract.information_cutoff for contract in contracts),
        default=_policy_cutoff(directory),
    )
    identity = {
        "schema_version": 1,
        "file_hashes": file_hashes,
        "split_family_hashes": split_family_hashes,
        "counts": counts,
        "rule_template_counts": dict(sorted(rule_templates.items())),
        "adversarial_tag_counts": dict(sorted(adversarial_tags.items())),
        "review_completion": completion,
        "information_cutoff": cutoff.isoformat().replace("+00:00", "Z"),
    }
    dataset_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
    manifest_path = directory / "manifests" / f"{dataset_id}.json"
    if manifest_path.exists():
        existing_manifest = CorpusManifest.model_validate_json(manifest_path.read_bytes())
        _activate_manifest(directory, existing_manifest)
        return existing_manifest
    manifest = CorpusManifest(
        schema_version=1,
        dataset_id=dataset_id,
        created_at=created_at or datetime.now(UTC),
        information_cutoff=cutoff,
        file_hashes=file_hashes,
        split_family_hashes=split_family_hashes,
        counts=counts,
        rule_template_counts=dict(sorted(rule_templates.items())),
        adversarial_tag_counts=dict(sorted(adversarial_tags.items())),
        review_completion=completion,
        frozen=True,
    )
    _atomic_write_immutable(manifest_path, _canonical_json(manifest.model_dump(mode="json")))
    _activate_manifest(directory, manifest)
    return manifest


def _activate_manifest(directory: Path, manifest: CorpusManifest) -> None:
    placeholder = directory / "manifest.json"
    if not placeholder.exists() or not placeholder.read_bytes().strip():
        _atomic_write_immutable(placeholder, _canonical_json(manifest.model_dump(mode="json")))
    else:
        active_manifest = json.loads(placeholder.read_bytes())
        if not isinstance(active_manifest, dict) or "frozen" not in active_manifest:
            raise ValueError("existing manifest placeholder is malformed")
        if active_manifest["frozen"] is False:
            atomic_write(placeholder, _canonical_json(manifest.model_dump(mode="json")))


def validate_corpus(directory: Path, *, require_reviews: bool = False) -> dict[str, int]:
    contracts = tuple(
        _validate_json_record(CorpusContract, row)
        for row in _read_jsonl(directory / "contracts.jsonl")
    )
    relationships = tuple(
        _validate_json_record(GoldRelationship, row)
        for row in _read_jsonl(directory / "relationships.jsonl")
    )
    reviews = tuple(
        _validate_json_record(ReviewRecord, row) for row in _read_jsonl(directory / "reviews.jsonl")
    )
    validate_split_integrity(contracts, relationships)
    completion = _review_completion(contracts, relationships, reviews)
    if require_reviews and completion["unresolved"]:
        raise ValueError(f"corpus has {completion['unresolved']} unresolved reviews")
    return completion


def _review_completion(
    contracts: Sequence[CorpusContract],
    relationships: Sequence[GoldRelationship],
    reviews: Sequence[ReviewRecord],
) -> dict[Literal["complete", "unresolved"], int]:
    items: dict[tuple[str, str], CorpusContract | GoldRelationship] = {
        **{("contract", item.contract_id): item for item in contracts},
        **{("relationship", item.relationship_id): item for item in relationships},
    }
    grouped: defaultdict[tuple[str, str], list[ReviewRecord]] = defaultdict(list)
    for record in reviews:
        key = (record.item_type, record.item_id)
        item = items.get(key)
        if item is None:
            raise ValueError(f"review references unknown {record.item_type} {record.item_id!r}")
        if record.input_hash != item_input_hash(item):
            raise ValueError(
                f"review input hash does not match {record.item_type} {record.item_id!r}"
            )
        grouped[key].append(record)
    complete = 0
    unresolved = 0
    for item_type, item_id in [
        *(("contract", item.contract_id) for item in contracts),
        *(("relationship", item.relationship_id) for item in relationships),
    ]:
        resolution = resolve_reviews(tuple(grouped[(item_type, item_id)]))
        if resolution.complete:
            complete += 1
        else:
            unresolved += 1
    return {"complete": complete, "unresolved": unresolved}


def _policy_cutoff(directory: Path) -> datetime:
    policy_path = directory / "policy.json"
    if policy_path.exists() and policy_path.read_bytes().strip():
        return CorpusPolicy.model_validate_json(policy_path.read_bytes()).information_cutoff
    return datetime(1970, 1, 1, tzinfo=UTC)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON in {path} line {line_number}") from error
        if not isinstance(row, dict):
            raise ValueError(f"JSONL row in {path} line {line_number} must be an object")
        rows.append(row)
    return rows


def _canonical_json(value: Any) -> bytes:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (serialized + "\n").encode()


def _validate_json_record[RecordT: StrictRecord](
    record_type: type[RecordT], row: dict[str, Any]
) -> RecordT:
    return record_type.model_validate_json(json.dumps(row, ensure_ascii=False))


def _jsonl_bytes(records: Sequence[StrictRecord]) -> bytes:
    return b"".join(_canonical_json(record.model_dump(mode="json")) for record in records)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _atomic_write_immutable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"immutable file already exists with different content: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            if path.read_bytes() != data:
                raise ValueError(
                    f"immutable file already exists with different content: {path}"
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


def load_contract_imports(path: Path) -> tuple[ContractImport, ...]:
    return tuple(_validate_json_record(ContractImport, row) for row in _read_jsonl(path))


def write_imported_contracts(path: Path, imported: Sequence[ImportedContract]) -> None:
    candidates = tuple(item.contract for item in imported)
    existing: tuple[CorpusContract, ...] = ()
    existing_bytes = b""
    if path.exists() and path.read_bytes().strip():
        existing_bytes = path.read_bytes()
        existing = tuple(_validate_json_record(CorpusContract, row) for row in _read_jsonl(path))
    by_id = {contract.contract_id: contract for contract in existing}
    additions: list[CorpusContract] = []
    for candidate in candidates:
        prior = by_id.get(candidate.contract_id)
        if prior is not None:
            if prior != candidate:
                raise ValueError(f"immutable contract ID {candidate.contract_id!r} already exists")
            continue
        by_id[candidate.contract_id] = candidate
        additions.append(candidate)
    validate_split_integrity((*existing, *additions), ())
    if not additions:
        return
    separator = b"\n" if existing_bytes and not existing_bytes.endswith(b"\n") else b""
    atomic_write(path, existing_bytes + separator + _jsonl_bytes(additions))


def append_review(path: Path, candidate: ReviewRecord) -> None:
    from polytrading.ai.review import validate_review_append

    existing = (
        tuple(_validate_json_record(ReviewRecord, row) for row in _read_jsonl(path))
        if path.exists()
        else ()
    )
    validate_review_append(existing, candidate)
    if candidate in existing:
        return
    atomic_write(path, _jsonl_bytes((*existing, candidate)))


def append_corpus_review(
    directory: Path,
    candidate: ReviewRecord,
    *,
    assignment: CorpusReviewAssignment | None = None,
) -> None:
    manifest_path = directory / "manifest.json"
    if manifest_path.exists() and manifest_path.read_bytes().strip():
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except json.JSONDecodeError as error:
            raise ValueError("corpus manifest is malformed") from error
        if not isinstance(manifest, dict):
            raise ValueError("corpus manifest must contain an object")
        if manifest.get("frozen") is True:
            raise ValueError("cannot append reviews to a frozen corpus")

    contracts = tuple(
        _validate_json_record(CorpusContract, row)
        for row in _read_jsonl(directory / "contracts.jsonl")
    )
    relationships = tuple(
        _validate_json_record(GoldRelationship, row)
        for row in _read_jsonl(directory / "relationships.jsonl")
    )
    validate_split_integrity(contracts, relationships)
    items: dict[tuple[str, str], CorpusContract | GoldRelationship] = {
        **{("contract", item.contract_id): item for item in contracts},
        **{("relationship", item.relationship_id): item for item in relationships},
    }
    item = items.get((candidate.item_type, candidate.item_id))
    if item is None:
        raise ValueError(f"review references unknown {candidate.item_type} {candidate.item_id!r}")
    expected_hash = item_input_hash(item)
    if candidate.input_hash != expected_hash:
        raise ValueError(
            f"review input hash does not match {candidate.item_type} {candidate.item_id!r}"
        )
    if assignment is not None and (
        assignment.item_type != candidate.item_type
        or assignment.item_id != candidate.item_id
        or assignment.reviewer_id != candidate.reviewer_id
        or assignment.input_hash != candidate.input_hash
    ):
        raise ValueError("review does not match supplied immutable assignment")
    append_review(directory / "reviews.jsonl", candidate)
