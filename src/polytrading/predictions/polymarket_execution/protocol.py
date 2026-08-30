from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StringConstraints, model_validator

POLYMARKET_PROTOCOL_VERSION = "polymarket-clob-2026-08-25-v1"
# The fresh checkpoint the local live pilot must run against (spec section 5.2). The v1 snapshot
# stays loadable and byte-identical so historical conformance evidence keeps its meaning.
POLYMARKET_PILOT_PROTOCOL_VERSION = "polymarket-clob-2026-08-29-v2"
ProtocolVersion = Literal[
    "polymarket-clob-2026-08-25-v1",
    "polymarket-clob-2026-08-29-v2",
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Iso8601Utc = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
]


@dataclass(frozen=True, slots=True)
class _SnapshotSpec:
    fixture_name: str
    fixture_sha256: str
    source_manifest_path: str


_SNAPSHOT_SPECS: dict[str, _SnapshotSpec] = {
    POLYMARKET_PROTOCOL_VERSION: _SnapshotSpec(
        "protocol_v1.json",
        "de7409eb9956cefe1d546adf611767ec2fecda11ff496172f90ddcd3525e751c",
        "sources_v1.json",
    ),
    POLYMARKET_PILOT_PROTOCOL_VERSION: _SnapshotSpec(
        "protocol_v2.json",
        "e0f41e5980ba1f4bb93e96423926abca84e39e138ec07ac9863820452fbe343f",
        "sources_v2.json",
    ),
}


class ProtocolSnapshotError(ValueError):
    """A snapshot selection or account-model binding the protocol layer refuses."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def _snapshot_spec(version: str) -> _SnapshotSpec:
    spec = _SNAPSHOT_SPECS.get(version)
    if spec is None:
        raise ProtocolSnapshotError(
            "PROTOCOL_VERSION_UNKNOWN", f"no frozen snapshot for version {version!r}"
        )
    return spec


class _FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


class TypedField(_FixtureModel):
    name: str
    type: str


class OrderDomain(_FixtureModel):
    name: str
    version: str
    chain_id: int = Field(alias="chainId")
    verifying_contract: str = Field(alias="verifyingContract")


class ClobAuthDomain(_FixtureModel):
    name: str
    version: str
    chain_id: int = Field(alias="chainId")


class ExchangeAddresses(_FixtureModel):
    standard: str
    negative_risk: str


class WalletSignaturePath(_FixtureModel):
    wallet: Literal["EOA", "PROXY", "SAFE", "DEPOSIT"]
    signature_type: int
    maker_address_rule: str
    signer_address_rule: str
    signing_primary_type: Literal["Order", "TypedDataSign"]
    signature_encoding: Literal["standard_eip712", "erc7739_wrapped"]


class DepositWalletMessageDomain(_FixtureModel):
    name: str
    version: str
    chain_id: int = Field(alias="chainId")
    verifying_contract: str = Field(alias="verifyingContract")
    salt: str


class DepositWalletWrapper(_FixtureModel):
    typed_data_primary_type: Literal["TypedDataSign"]
    typed_data_fields: tuple[TypedField, ...]
    message_domain: DepositWalletMessageDomain
    order_type_string: str
    domain_type_string: str
    signature_segments: tuple[str, ...]


class Eip712Contract(_FixtureModel):
    order_domain: OrderDomain
    order_primary_type: Literal["Order"]
    order_fields: tuple[TypedField, ...]
    exchange_addresses: ExchangeAddresses
    wallets: tuple[WalletSignaturePath, ...]
    deposit_wallet_wrapper: DepositWalletWrapper


class ClobAuthContract(_FixtureModel):
    domain: ClobAuthDomain
    primary_type: Literal["ClobAuth"]
    fields: tuple[TypedField, ...]
    message: str
    default_nonce: int
    l1_headers: tuple[str, ...]


class L2AuthenticationContract(_FixtureModel):
    headers: tuple[str, ...]
    preimage_components: tuple[str, ...]
    body_omission_rule: str
    algorithm: Literal["HMAC-SHA256"]
    secret_decoding: Literal["base64"]
    signature_encoding: Literal["urlsafe_base64_with_padding"]


class AuthenticationContract(_FixtureModel):
    clob_auth: ClobAuthContract
    l2: L2AuthenticationContract


class Route(_FixtureModel):
    host: str
    method: Literal["GET", "POST", "DELETE"]
    path: str
    auth_level: Literal["PUBLIC", "L1", "L2", "L2_AND_ORDER_SIGNATURE"]
    request_body_shape: str
    query_fields: tuple[str, ...]
    request_fields: tuple[str, ...]
    compact_body_examples: tuple[str, ...]
    response_body_shape: Literal["object", "array"]
    response_fields: tuple[str, ...]
    response_item_fields: tuple[str, ...]


class BalanceAllowanceRoute(Route):
    allowed_signature_types: tuple[Literal[0], ...]
    allowed_asset_types: tuple[Literal["COLLATERAL", "CONDITIONAL"], ...]
    conditional_token_id_required: Literal[True]
    balance_encoding: Literal["ascii_nonnegative_integer_string"]
    allowances_encoding: Literal["evm_address_to_ascii_nonnegative_integer_string"]


class RouteCatalog(_FixtureModel):
    create_api_key: Route
    derive_api_key: Route
    order_book: Route
    place_order: Route
    place_orders: Route
    get_order: Route
    list_orders: Route
    list_trades: Route
    balance_allowance: BalanceAllowanceRoute
    cancel_order: Route
    cancel_orders: Route
    cancel_market_orders: Route
    cancel_all: Route
    heartbeat: Route
    geoblock: Route


class FeeRateBinding(_FixtureModel):
    state: Literal["ABSENT_SUPERSEDED"]
    signed_order_field: None
    posted_order_field: None
    evidence_location: Literal["preflight_risk_evidence_only"]
    source_id: str


class OrderTypeSemantics(_FixtureModel):
    fak: str = Field(alias="FAK")
    fok: str = Field(alias="FOK")


class SideEncodings(_FixtureModel):
    buy: int = Field(alias="BUY")
    sell: int = Field(alias="SELL")


class OrderSubmissionContract(_FixtureModel):
    allowed_order_types: tuple[Literal["FAK", "FOK"], ...]
    order_type_semantics: OrderTypeSemantics
    side_encodings: SideEncodings
    typed_order_fields: tuple[str, ...]
    posted_order_fields: tuple[str, ...]
    outer_payload_fields: tuple[str, ...]
    market_order_expiration: Literal["0"]
    acknowledgement_states: tuple[Literal["live", "matched", "delayed", "unmatched"], ...]
    fee_rate_binding: FeeRateBinding


class TickSizeRule(_FixtureModel):
    tick_size: str
    price_decimals: int
    size_decimals: int
    amount_decimals: int


class RoundingContract(_FixtureModel):
    token_decimals: int
    tick_size_rules: tuple[TickSizeRule, ...]
    limit_rules: tuple[str, ...]
    market_rules: tuple[str, ...]


class WebsocketContract(_FixtureModel):
    url: str
    subscription_type: Literal["user"]
    authentication_fields: tuple[str, ...]
    optional_market_filter_field: Literal["markets"]
    ping: Literal["PING"]
    pong: Literal["PONG"]
    ping_interval_seconds: int
    event_discriminator: Literal["event_type"]
    order_event_types: tuple[Literal["PLACEMENT", "UPDATE", "CANCELLATION"], ...]


class HeartbeatConflictResolution(_FixtureModel):
    authoritative_route: str
    authoritative_guide_source_id: str
    maintained_client_source_ids: tuple[str, ...]
    stale_generated_route: str
    stale_generated_source_id: str
    resolution: str


class HeartbeatContract(_FixtureModel):
    initial_compact_body: str
    subsequent_body_rule: str
    response_fields: tuple[str, ...]
    cadence_seconds: int
    cancellation_timeout_seconds: int
    cancellation_check_interval_seconds: int
    invalid_id_status: int
    invalid_id_response_fields: tuple[str, ...]
    invalid_id_retry_rule: str
    conflict_resolution: HeartbeatConflictResolution


class GeoblockContract(_FixtureModel):
    response_fields: tuple[Literal["blocked", "ip", "country", "region"], ...]
    blocked_type: Literal["boolean"]
    country_format: Literal["ISO_3166_1_ALPHA_2"]
    region_format: Literal["region_or_state_code"]
    placement_rule: Literal["blocked_true_forbids_order_placement"]


class FixtureHash(_FixtureModel):
    path: str
    sha256: Sha256Digest


class SourceNormalization(_FixtureModel):
    input: Literal["raw_response_body_bytes"]
    encoding: Literal["UTF-8"]
    transformations: tuple[Literal["CRLF_TO_LF"], ...]
    strip_or_reflow: Literal[False]


class ProtocolSource(_FixtureModel):
    source_id: str
    canonical_url: str
    retrieved_at: _Iso8601Utc
    normalized_content_sha256: Sha256Digest
    protocol_fixture_version: ProtocolVersion
    implementation_revision: str
    upstream_revision: str | None
    role: str
    derived_files: tuple[str, ...]


class SourceManifest(_FixtureModel):
    schema_version: Literal[1]
    normalization: SourceNormalization
    sources: tuple[ProtocolSource, ...]


class UnsupportedWallet(_FixtureModel):
    wallet: Literal["DEPOSIT"]
    signature_type: int
    reason: str
    source_id: str


class AccountSignatureModel(_FixtureModel):
    """How the signer must bind one configured wallet before it may sign anything.

    ``default_signature_type`` is deliberately ``None``: an ambiguous or undocumented account
    form blocks activation instead of silently signing as an EOA (spec section 5.2).
    """

    discovery_rule: str
    default_signature_type: None
    allowed_signature_types: tuple[int, ...]
    signature_type_names: dict[str, str]
    signer_field: Literal["signer"]
    funder_field: Literal["maker"]
    requires_explicit_funder: Literal[True]
    chain_id: Literal[137]
    exchange_binding: str
    unsupported_wallets: tuple[UnsupportedWallet, ...]
    credential_routes: tuple[str, ...]
    credential_vectors_path: str


class PolymarketProtocolSnapshot(_FixtureModel):
    schema_version: Literal[1]
    version: ProtocolVersion
    chain_id: Literal[137]
    source_manifest_path: Literal["sources_v1.json", "sources_v2.json"]
    eip712: Eip712Contract
    authentication: AuthenticationContract
    routes: RouteCatalog
    order_submission: OrderSubmissionContract
    rounding: RoundingContract
    websocket: WebsocketContract
    heartbeat: HeartbeatContract
    geoblock: GeoblockContract
    trade_states: tuple[
        Literal[
            "MATCHED_NOT_BROADCASTED",
            "MATCHED",
            "MINED",
            "CONFIRMED",
            "RETRYING",
            "FAILED",
        ],
        ...,
    ]
    fixture_hashes: tuple[FixtureHash, ...]
    account_signature_model: AccountSignatureModel | None = None
    _fixture_root: Path = PrivateAttr()
    _sources: tuple[ProtocolSource, ...] = PrivateAttr()

    @property
    def fixture_root(self) -> Path:
        return self._fixture_root

    @property
    def sources(self) -> tuple[ProtocolSource, ...]:
        return self._sources

    @property
    def allowed_order_types(self) -> tuple[str, ...]:
        return self.order_submission.allowed_order_types

    @model_validator(mode="after")
    def _require_closed_protocol_contract(self) -> PolymarketProtocolSnapshot:
        if self.order_submission.allowed_order_types != ("FAK", "FOK"):
            raise ValueError("only FAK and FOK are allowed for execution")
        if self.trade_states != (
            "MATCHED_NOT_BROADCASTED",
            "MATCHED",
            "MINED",
            "CONFIRMED",
            "RETRYING",
            "FAILED",
        ):
            raise ValueError("trade settlement states do not match the frozen protocol")
        return self


class AccountSignatureBinding(_FixtureModel):
    """One discovered wallet/funder/signature binding, frozen for a single account."""

    signer_address: str
    funder_address: str
    signature_type: int
    chain_id: Literal[137]
    exchange_address: str
    order_domain_verifying_contract: str
    credential_route_hash: Sha256Digest
    protocol_version: ProtocolVersion


@dataclass(frozen=True, slots=True)
class ProtocolReadiness:
    state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    changed_paths: tuple[str, ...]


def bundled_fixture_path() -> Path:
    """Resolve the packaged protocol fixtures without relying on a source checkout."""
    package_root = resources.files("polytrading.predictions.polymarket_execution")
    fixture_root = package_root.joinpath("fixtures")
    if not isinstance(fixture_root, Path):
        raise RuntimeError("Polymarket fixtures must be installed as filesystem resources")
    return fixture_root


def load_protocol_snapshot(
    root: Path | None = None,
    *,
    version: str = POLYMARKET_PROTOCOL_VERSION,
) -> PolymarketProtocolSnapshot:
    """Load one explicitly selected frozen protocol snapshot with strict offline validation."""
    spec = _snapshot_spec(version)
    fixture_root = Path(root) if root is not None else bundled_fixture_path()
    snapshot = PolymarketProtocolSnapshot.model_validate_json(
        (fixture_root / spec.fixture_name).read_bytes(),
        strict=True,
    )
    if snapshot.version != version or snapshot.source_manifest_path != spec.source_manifest_path:
        raise ProtocolSnapshotError(
            "PROTOCOL_VERSION_MISMATCH",
            f"{spec.fixture_name} does not declare the requested checkpoint {version!r}",
        )
    source_manifest = SourceManifest.model_validate_json(
        (fixture_root / snapshot.source_manifest_path).read_bytes(),
        strict=True,
    )
    if any(source.protocol_fixture_version != version for source in source_manifest.sources):
        raise ProtocolSnapshotError(
            "SOURCE_MANIFEST_VERSION_MISMATCH",
            f"{snapshot.source_manifest_path} cites another protocol checkpoint",
        )
    if version == POLYMARKET_PILOT_PROTOCOL_VERSION:
        require_account_signature_model(snapshot)
    object.__setattr__(snapshot, "_fixture_root", fixture_root)
    object.__setattr__(snapshot, "_sources", source_manifest.sources)
    return snapshot


def require_account_signature_model(
    snapshot: PolymarketProtocolSnapshot,
) -> AccountSignatureModel:
    """Require the pilot checkpoint to state its wallet/funder/signature binding explicitly."""
    model = snapshot.account_signature_model
    if model is None:
        raise ProtocolSnapshotError(
            "ACCOUNT_SIGNATURE_MODEL_REQUIRED",
            f"{snapshot.version} must bind an explicit account and signature model",
        )
    documented = {wallet.signature_type for wallet in snapshot.eip712.wallets}
    if not model.allowed_signature_types or set(model.allowed_signature_types) != documented:
        raise ProtocolSnapshotError(
            "ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED",
            "allowed signature types must match the documented wallet paths exactly",
        )
    unsupported = {wallet.signature_type for wallet in model.unsupported_wallets}
    if unsupported & set(model.allowed_signature_types):
        raise ProtocolSnapshotError(
            "ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED",
            "a wallet path cannot be both allowed and unsupported",
        )
    return model


def bind_account_signature(
    snapshot: PolymarketProtocolSnapshot,
    *,
    signer_address: str,
    funder_address: str,
    signature_type: int,
    negative_risk: bool,
    credential_route_hash: str,
) -> AccountSignatureBinding:
    """Bind one discovered account form, refusing anything ambiguous or undocumented."""
    model = require_account_signature_model(snapshot)
    if signature_type not in model.allowed_signature_types:
        raise ProtocolSnapshotError(
            "ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED",
            f"signature type {signature_type} is not a documented pilot wallet path",
        )
    exchange = snapshot.eip712.exchange_addresses
    return AccountSignatureBinding(
        signer_address=signer_address,
        funder_address=funder_address,
        signature_type=signature_type,
        chain_id=snapshot.chain_id,
        exchange_address=exchange.negative_risk if negative_risk else exchange.standard,
        order_domain_verifying_contract=snapshot.eip712.order_domain.verifying_contract,
        credential_route_hash=credential_route_hash,
        protocol_version=snapshot.version,
    )


def verify_protocol_sources(
    snapshot: PolymarketProtocolSnapshot | None = None,
    *,
    root: Path | None = None,
    version: str = POLYMARKET_PROTOCOL_VERSION,
) -> ProtocolReadiness:
    """Return fail-closed readiness without requiring fixture parsing to succeed first."""
    if snapshot is not None and root is not None:
        raise TypeError("pass either snapshot or root, not both")
    fixture_root = (
        snapshot.fixture_root
        if snapshot is not None
        else Path(root)
        if root is not None
        else bundled_fixture_path()
    )
    return _verify_fixture_root(fixture_root, version)


def _verify_fixture_root(fixture_root: Path, version: str) -> ProtocolReadiness:
    spec = _snapshot_spec(version)
    protocol_path = fixture_root / spec.fixture_name
    try:
        protocol_bytes = protocol_path.read_bytes()
    except OSError:
        return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (spec.fixture_name,))
    if sha256(protocol_bytes).hexdigest() != spec.fixture_sha256:
        return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (spec.fixture_name,))

    try:
        snapshot = PolymarketProtocolSnapshot.model_validate_json(protocol_bytes, strict=True)
    except ValueError:
        return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (spec.fixture_name,))
    if snapshot.version != version:
        return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (spec.fixture_name,))
    if version == POLYMARKET_PILOT_PROTOCOL_VERSION:
        try:
            require_account_signature_model(snapshot)
        except ProtocolSnapshotError:
            return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", (spec.fixture_name,))

    changed_paths: list[str] = []
    fixture_bytes: dict[str, bytes] = {}
    for fixture in snapshot.fixture_hashes:
        fixture_path = fixture_root / fixture.path
        try:
            contents = fixture_path.read_bytes()
        except OSError:
            changed_paths.append(fixture.path)
            continue
        fixture_bytes[fixture.path] = contents
        if sha256(contents).hexdigest() != fixture.sha256:
            changed_paths.append(fixture.path)

    if changed_paths:
        return ProtocolReadiness("PROTOCOL_REVIEW_REQUIRED", tuple(changed_paths))

    try:
        SourceManifest.model_validate_json(
            fixture_bytes[snapshot.source_manifest_path],
            strict=True,
        )
    except (KeyError, ValueError):
        return ProtocolReadiness(
            "PROTOCOL_REVIEW_REQUIRED",
            (snapshot.source_manifest_path,),
        )
    return ProtocolReadiness("CURRENT", ())
