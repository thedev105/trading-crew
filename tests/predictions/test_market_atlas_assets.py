import json
import re
import subprocess
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

ASSET_PACKAGE = "polytrading.predictions.web_assets"
PRIMARY_VIEWS = ["Overview", "Markets", "Execution", "Ledger", "Evidence"]
FORBIDDEN_BROWSER_TRANSPORT = re.compile(
    r"(?:WebSocket|wss?://|XMLHttpRequest|sendBeacon|navigator\.credentials)", re.IGNORECASE
)
FORBIDDEN_MUTATION_FETCH = re.compile(
    r"method\s*:\s*['\"](?:POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)['\"]",
    re.IGNORECASE,
)


class MarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text_by_view: list[str] = []
        self._view_button_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "button" and attributes.get("data-view"):
            self._view_button_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._view_button_depth:
            self._view_button_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._view_button_depth:
            value = data.strip()
            if value:
                self.text_by_view.append(value)


def asset_text(name: str) -> str:
    return resources.files(ASSET_PACKAGE).joinpath(name).read_text(encoding="utf-8")


def all_javascript_assets() -> str:
    root = resources.files(ASSET_PACKAGE)
    return "\n".join(
        child.read_text(encoding="utf-8")
        for child in sorted(root.iterdir(), key=lambda item: item.name)
        if child.name.endswith(".js")
    )


def parsed_index() -> MarkupParser:
    parser = MarkupParser()
    parser.feed(asset_text("index.html"))
    return parser


def run_node_module_test(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def module_uri(tmp_path: Path, name: str) -> str:
    target = tmp_path / f"{Path(name).stem}.mjs"
    target.write_text(asset_text(name), encoding="utf-8")
    return target.as_uri()


def module_bundle_uri(tmp_path: Path, entrypoint: str, *dependencies: str) -> str:
    names = (*dependencies, entrypoint)
    stems = {Path(name).stem for name in names}
    for name in names:
        source = asset_text(name)
        for stem in stems:
            source = source.replace(f'"./{stem}.js"', f'"./{stem}.mjs"')
        (tmp_path / f"{Path(name).stem}.mjs").write_text(source, encoding="utf-8")
    return (tmp_path / f"{Path(entrypoint).stem}.mjs").as_uri()


def valid_snapshot() -> dict[str, object]:
    cutoff = "2026-08-16T12:00:00Z"
    return {
        "schema_version": 1,
        "revision_id": "a" * 64,
        "as_of": cutoff,
        "health": {"schema_version": 1, "as_of": cutoff, "venues": [], "warnings": []},
        "markets": [],
        "books": [],
        "evidence_counts": {"schema_version": 1, "counts": {}},
        "recipes": {"schema_version": 1, "recipes": []},
        "candidates": {
            "schema_version": 1,
            "total": 0,
            "by_relationship_type": {},
            "by_disposition": {},
            "by_provenance_kind": {},
            "latest": [],
        },
        "proofs": {
            "schema_version": 1,
            "total": 0,
            "by_status": {},
            "by_template": {},
            "latest": [],
        },
        "scans": {"schema_version": 1, "total": 0, "by_decision": {}, "latest": []},
        "shadow": {
            "schema_version": 1,
            "proposals_total": 0,
            "by_terminal_state": {},
            "reconciled_count": 0,
            "reconciled_paper_pnl_usd": "0",
            "unreconciled_count": 0,
            "latest": [],
            "experiments_by_family": {},
        },
        "execution_readiness": {
            "schema_version": 1,
            "as_of": cutoff,
            "implementation_state": "LIVE_DISABLED",
            "protocol_state": "CURRENT",
            "conformance_result": "CONFORMANT",
            "conformance_observed_at": cutoff,
            "kill_engaged": True,
            "kill_trigger": "KILL_EVENT_RECORDED",
            "production_capability_available": False,
            "live_action_available": False,
            "unmet_gates": ["EXECUTION_KILL_ENGAGED"],
        },
        "opportunities": [
            {
                "schema_version": 1,
                "as_of": cutoff,
                "candidate_id": "00000000-0000-0000-0000-000000000001",
                "proof_id": None,
                "relationship_type": "logical_implication",
                "decision": "SHADOW_CANDIDATE",
                "conservative_surplus_usd": "12.50",
                "capacity_usd": "250.00",
                "reconciled": True,
                "evidence_hashes": ["b" * 64],
            }
        ],
        "execution_timeline": [
            {
                "schema_version": 1,
                "as_of": cutoff,
                "kind": "intent",
                "record_id": "00000000-0000-0000-0000-000000000002",
                "occurred_at": cutoff,
                "state": "UNKNOWN",
                "reason_code": "RECONCILIATION_ACTION_REQUIRED",
                "reconciled": False,
                "evidence_hashes": ["c" * 64],
            }
        ],
        "live_ledger": {
            "schema_version": 1,
            "as_of": cutoff,
            "posting_count": 4,
            "reconciliation_count": 1,
            "complete_reconciliation_count": 1,
            "incomplete_reconciliation_count": 0,
            "pnl_publishable": True,
            "realized_pnl_usd": "7.75",
        },
        "evidence_status": {
            "schema_version": 1,
            "as_of": cutoff,
            "protocol_version": "polymarket-clob-2026-08-25-v1",
            "protocol_state": "CURRENT",
            "manifest_state": "LIVE_DISABLED",
            "conformance_result": "CONFORMANT",
            "conformance_observed_at": cutoff,
            "account_count": 1,
            "source_hashes": ["d" * 64],
            "unmet_activation_gates": ["EXECUTION_KILL_ENGAGED"],
        },
    }


def test_market_atlas_has_exactly_five_primary_views() -> None:
    parser = parsed_index()

    assert parser.text_by_view == PRIMARY_VIEWS
    view_buttons = [
        attrs for tag, attrs in parser.tags if tag == "button" and attrs.get("data-view")
    ]
    assert [attrs["data-view"] for attrs in view_buttons] == [
        "overview",
        "markets",
        "execution",
        "ledger",
        "evidence",
    ]
    assert all(attrs.get("type") == "button" for attrs in view_buttons)
    tablists = [
        attrs for tag, attrs in parser.tags if tag == "div" and attrs.get("role") == "tablist"
    ]
    assert len(tablists) == 1
    assert "aria-orientation" not in tablists[0]


def test_market_atlas_shell_has_semantic_landmarks_and_persistent_safety_rail() -> None:
    html = asset_text("index.html")
    parser = parsed_index()
    tags = {tag for tag, _attrs in parser.tags}
    ids = {attrs["id"] for _tag, attrs in parser.tags if attrs.get("id")}

    assert {"header", "nav", "main", "aside"}.issubset(tags)
    assert {
        "main",
        "primary-navigation",
        "view-root",
        "safety-rail",
        "connection-state",
        "kill-state",
        "snapshot-cutoff",
        "last-refresh",
        "update-summary",
    }.issubset(ids)
    assert any(
        tag == "a" and attrs.get("class") == "skip-link" and attrs.get("href") == "#main"
        for tag, attrs in parser.tags
    )
    for persistent_copy in (
        "LIVE_DISABLED",
        "READ ONLY",
        "Kill state",
        "Connection",
        "Snapshot cutoff",
        "Last refresh",
    ):
        assert persistent_copy in html


def test_market_atlas_declares_an_inline_favicon() -> None:
    icon_links = [
        attrs for tag, attrs in parsed_index().tags if tag == "link" and attrs.get("rel") == "icon"
    ]

    assert len(icon_links) == 1
    assert icon_links[0]["href"].startswith("data:image/svg+xml,")


def test_market_atlas_shell_uses_the_dark_institutional_visual_language() -> None:
    css = asset_text("app.css")

    for token in (
        "--ink-950: #071018",
        "--slate-900: #0d1824",
        "--slate-800: #142332",
        "--cyan-400: #48d7ef",
        "--amber-400: #f2bd5c",
        "--coral-400: #ff6f70",
        "font-variant-numeric: tabular-nums",
        ".atlas-shell",
        ".primary-nav",
        ".safety-rail",
        '[data-tone="fault"]',
    ):
        assert token in css
    assert "linear-gradient" in css
    assert "system-ui" in css


def test_market_atlas_is_responsive_focus_visible_and_reduced_motion_safe() -> None:
    css = asset_text("app.css")

    assert "@media (min-width: 1100px)" in css
    assert "grid-template-columns: 216px minmax(0, 1fr) 288px" in css
    assert "@media (min-width: 700px) and (max-width: 1099px)" in css
    assert '"nav nav"' in css
    assert '"main rail"' in css
    assert "@media (max-width: 699px)" in css
    assert '"header"\n      "nav"\n      "main"\n      "rail"' in css
    assert ":focus-visible" in css
    assert "outline: 3px solid var(--cyan-400)" in css
    assert "prefers-reduced-motion: reduce" in css
    assert ".table-region" in css
    assert "overflow-x: auto" in css


def test_market_atlas_visual_contract_keeps_dense_state_data_readable() -> None:
    css = asset_text("app.css")
    tablet_css = css.split("@media (min-width: 700px) and (max-width: 1099px)", maxsplit=1)[
        1
    ].split("@media (max-width: 699px)", maxsplit=1)[0]

    for contract in (
        ".metric-card__value--token",
        "white-space: nowrap",
        ".chart__track",
        ".data-table--execution",
        "min-width: 980px",
        ".integrity-notice",
    ):
        assert contract in css
    assert ".timeline__item" in tablet_css
    assert ".timeline__time" in tablet_css
    assert "grid-column: 1 / -1" in tablet_css
    assert "word-break: normal" in tablet_css


def test_browser_assets_are_read_only_same_origin_observers() -> None:
    html = asset_text("index.html")
    source = all_javascript_assets()
    combined = f"{html}\n{source}"
    combined_without_svg_namespace = combined.replace("http://www.w3.org/2000/svg", "")

    assert "EventSource" in source
    assert '"/api/v1/predictions-events"' in source
    assert '"/api/v1/predictions-dashboard"' in source
    assert FORBIDDEN_BROWSER_TRANSPORT.search(source) is None
    assert FORBIDDEN_MUTATION_FETCH.search(source) is None
    assert "innerHTML" not in source
    assert "outerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "http://" not in combined_without_svg_namespace
    assert "https://" not in combined_without_svg_namespace
    assert "<form" not in html.lower()
    for label in (
        "place order",
        "cancel order",
        "activate live",
        "clear kill",
        "import credentials",
        "api key",
        "private key",
        "authorization header",
        "signed body",
        "raw payload",
    ):
        assert label not in combined.lower()


def test_market_atlas_index_loads_only_local_styles_and_the_module_entrypoint() -> None:
    parser = parsed_index()
    resource_urls = {
        value
        for _tag, attrs in parser.tags
        for name, value in attrs.items()
        if name in {"src", "href"} and value and not value.startswith(("#", "data:"))
    }

    assert resource_urls == {"/assets/app.css", "/assets/app.js"}
    assert any(
        tag == "script" and attrs.get("type") == "module" and attrs.get("src") == "/assets/app.js"
        for tag, attrs in parser.tags
    )


def test_api_fetches_only_the_same_origin_snapshot_with_bounded_busy_retry(
    tmp_path: Path,
) -> None:
    uri = module_uri(tmp_path, "api.js")
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ fetchDashboardSnapshot }} from {json.dumps(uri)};

        const calls = [];
        const waits = [];
        const signal = new AbortController().signal;
        const responses = [
          {{ ok: false, json: async () => ({{ error: {{ code: "DATABASE_BUSY" }} }}) }},
          {{ ok: true, json: async () => ({{ schema_version: 1 }}) }},
        ];
        const snapshot = await fetchDashboardSnapshot({{
          signal,
          fetchImpl: async (url, options) => {{
            calls.push([url, options]);
            return responses.shift();
          }},
          wait: async (delay, receivedSignal) => waits.push([delay, receivedSignal]),
        }});
        assert.deepEqual(snapshot, {{ schema_version: 1 }});
        assert.equal(calls.length, 2);
        for (const [url, options] of calls) {{
          assert.equal(url, "/api/v1/predictions-dashboard");
          assert.equal(options.method, "GET");
          assert.deepEqual(options.headers, {{ Accept: "application/json" }});
          assert.equal(options.cache, "no-store");
          assert.equal(options.signal, signal);
        }}
        assert.deepEqual(waits, [[250, signal]]);

        let attempts = 0;
        await assert.rejects(
          fetchDashboardSnapshot({{
            signal,
            fetchImpl: async () => {{
              attempts += 1;
              return {{ ok: false, json: async () => ({{ error: {{ code: "DATABASE_BUSY" }} }}) }};
            }},
            wait: async () => undefined,
          }}),
          /DATABASE_BUSY/,
        );
        assert.equal(attempts, 4);

        attempts = 0;
        await assert.rejects(
          fetchDashboardSnapshot({{
            signal,
            fetchImpl: async () => {{
              attempts += 1;
              return {{
                ok: false,
                json: async () => ({{ error: {{ code: "DATABASE_UNAVAILABLE" }} }}),
              }};
            }},
            wait: async () => undefined,
          }}),
          /DATABASE_UNAVAILABLE/,
        );
        assert.equal(attempts, 1);
        """
    )


def test_store_atomically_detaches_freezes_and_coalesces_valid_replacements(
    tmp_path: Path,
) -> None:
    uri = module_uri(tmp_path, "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{
          CONNECTED,
          DEGRADED,
          STALE,
          DISCONNECTED,
          INCONSISTENT,
          createSnapshotStore,
          replaceSnapshot,
        }} from {json.dumps(uri)};

        assert.deepEqual(
          [CONNECTED, DEGRADED, STALE, DISCONNECTED, INCONSISTENT],
          ["CONNECTED", "DEGRADED", "STALE", "DISCONNECTED", "INCONSISTENT"],
        );
        const scheduled = [];
        let notifications = 0;
        const store = createSnapshotStore({{
          scheduleNotification: (callback) => scheduled.push(callback),
          now: () => Date.parse("2026-08-16T12:00:05Z"),
        }});
        store.subscribe(() => {{ notifications += 1; }});

        const input = {snapshot};
        const accepted = replaceSnapshot(store, input);
        assert.notEqual(accepted, input);
        assert.equal(store.getState().snapshot, accepted);
        assert.equal(Object.isFrozen(accepted), true);
        assert.equal(Object.isFrozen(accepted.execution_readiness), true);
        assert.equal(Object.isFrozen(accepted.opportunities), true);
        assert.equal(Object.isFrozen(accepted.opportunities[0]), true);
        const assertNullPrototypeGraph = (value) => {{
          if (Array.isArray(value)) {{
            assert.equal(Object.isFrozen(value), true);
            value.forEach(assertNullPrototypeGraph);
          }} else if (value !== null && typeof value === "object") {{
            assert.equal(Object.getPrototypeOf(value), null);
            assert.equal(Object.isFrozen(value), true);
            Object.values(value).forEach(assertNullPrototypeGraph);
          }}
        }};
        assertNullPrototypeGraph(accepted);
        assert.equal(Object.isFrozen(input), false);
        input.live_ledger.realized_pnl_usd = "999999";
        assert.equal(accepted.live_ledger.realized_pnl_usd, "7.75");
        assert.equal(scheduled.length, 1);

        const newer = structuredClone(input);
        newer.revision_id = "e".repeat(64);
        newer.live_ledger.realized_pnl_usd = "8.25";
        replaceSnapshot(store, newer);
        assert.equal(scheduled.length, 1);
        scheduled.shift()();
        assert.equal(notifications, 1);
        assert.equal(store.getState().snapshot.revision_id, "e".repeat(64));

        store.setConnectionState(INCONSISTENT, "REVISION_MISMATCH");
        scheduled.shift()();
        const state = store.getState();
        assert.equal(state.snapshot.revision_id, "e".repeat(64));
        assert.equal(state.displaySnapshot.live_ledger.pnl_publishable, false);
        assert.equal(state.displaySnapshot.live_ledger.realized_pnl_usd, null);
        assert.equal(state.displaySnapshot.shadow.reconciled_paper_pnl_usd, null);
        assert.equal(state.displaySnapshot.opportunities[0].conservative_surplus_usd, null);
        assert.equal(state.displaySnapshot.opportunities[0].capacity_usd, null);
        assert.equal(state.financialsHidden, true);
        """
    )


def test_store_rejects_inherited_schema_and_prototype_meta_keys(tmp_path: Path) -> None:
    uri = module_uri(tmp_path, "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ createSnapshotStore, INCONSISTENT }} from {json.dumps(uri)};

        const baseline = {snapshot};
        const store = createSnapshotStore({{ scheduleNotification: (callback) => callback() }});
        const accepted = store.replaceSnapshot(baseline);

        const inheritedSchema = JSON.parse(JSON.stringify(baseline));
        delete inheritedSchema.live_ledger.schema_version;
        Object.prototype.schema_version = 1;
        try {{
          assert.throws(() => store.replaceSnapshot(inheritedSchema), /INVALID_SNAPSHOT/);
        }} finally {{
          delete Object.prototype.schema_version;
        }}
        assert.equal(store.getState().snapshot, accepted);

        for (const key of ["__proto__", "constructor", "prototype"]) {{
          const hostile = JSON.parse(JSON.stringify(baseline));
          hostile.shadow.latest = [{{ current_state: "reconciled" }}];
          Object.defineProperty(hostile.shadow.latest[0], key, {{
            value: {{ paper_pnl: "HOSTILE-CANARY" }},
            enumerable: true,
            writable: true,
            configurable: true,
          }});
          assert.throws(() => store.replaceSnapshot(hostile), /INVALID_SNAPSHOT/);
          assert.equal(store.getState().snapshot, accepted);
          assert.equal(store.getState().connectionState, INCONSISTENT);
        }}
        """
    )


def test_store_prepares_clock_and_scheduler_before_one_snapshot_commit(tmp_path: Path) -> None:
    uri = module_uri(tmp_path, "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ createSnapshotStore, INCONSISTENT }} from {json.dumps(uri)};

        const baseline = {snapshot};
        for (const seam of ["clock", "scheduler"]) {{
          let hostile = false;
          const store = createSnapshotStore({{
            now: () => {{
              if (hostile && seam === "clock") throw new Error("CLOCK-CANARY");
              return Date.parse("2026-08-16T12:00:05Z");
            }},
            scheduleNotification: (callback) => {{
              if (hostile && seam === "scheduler") throw new Error("SCHEDULER-CANARY");
              callback();
            }},
          }});
          const first = store.replaceSnapshot(baseline);
          const replacement = JSON.parse(JSON.stringify(baseline));
          replacement.revision_id = "e".repeat(64);
          hostile = true;
          assert.throws(() => store.replaceSnapshot(replacement));
          assert.equal(store.getState().snapshot, first, `${{seam}} replaced last-good identity`);
          assert.equal(store.getState().connectionState, INCONSISTENT);
        }}
        """
    )


def test_store_recursively_redacts_aggregate_and_row_level_pnl(tmp_path: Path) -> None:
    uri = module_uri(tmp_path, "store.js")
    snapshot = valid_snapshot()
    snapshot["shadow"]["latest"] = [  # type: ignore[index]
        {
            "current_state": "reconciled",
            "paper_pnl": "PAPER-PNL-CANARY",
            "live_pnl_usd": "LIVE-PNL-CANARY",
            "nested": {"paper_pnl_usd": "NESTED-PNL-CANARY"},
        }
    ]
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ createSnapshotStore, INCONSISTENT }} from {json.dumps(uri)};

        const store = createSnapshotStore({{ scheduleNotification: (callback) => callback() }});
        store.replaceSnapshot({json.dumps(snapshot)});
        store.setConnectionState(INCONSISTENT, "REVISION_MISMATCH");
        const state = store.getState();
        const renderedSurface = JSON.stringify(state.displaySnapshot);
        assert.equal(state.displaySnapshot.live_ledger.pnl_publishable, false);
        assert.doesNotMatch(renderedSurface, /PAPER-PNL-CANARY|LIVE-PNL-CANARY|NESTED-PNL-CANARY/);
        assert.equal(state.displaySnapshot.shadow.latest[0].paper_pnl, null);
        assert.equal(state.displaySnapshot.shadow.latest[0].live_pnl_usd, null);
        assert.equal(state.displaySnapshot.shadow.latest[0].nested.paper_pnl_usd, null);
        """
    )


def test_store_rejects_every_cutoff_mismatch_and_retains_the_previous_snapshot(
    tmp_path: Path,
) -> None:
    uri = module_uri(tmp_path, "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{
          INCONSISTENT,
          createSnapshotStore,
          validateSnapshotCutoff,
        }} from {json.dumps(uri)};

        const baseline = {snapshot};
        const store = createSnapshotStore({{ scheduleNotification: () => undefined }});
        const first = store.replaceSnapshot(baseline);
        assert.equal(validateSnapshotCutoff(first), first.as_of);

        const mutations = [
          (value) => {{ value.execution_readiness.as_of = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.opportunities[0].as_of = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.execution_timeline[0].as_of = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.live_ledger.as_of = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.evidence_status.as_of = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.execution_timeline[0].occurred_at = "2026-08-16T12:00:01Z"; }},
          (value) => {{ value.schema_version = 2; }},
          (value) => {{ value.revision_id = "not-a-revision"; }},
          (value) => {{ value.as_of = "not-a-timestamp"; }},
        ];
        for (const mutate of mutations) {{
          const invalid = structuredClone(baseline);
          mutate(invalid);
          assert.throws(() => store.replaceSnapshot(invalid), /INVALID_SNAPSHOT/);
          assert.equal(store.getState().snapshot, first);
          assert.equal(store.getState().connectionState, INCONSISTENT);
        }}
        """
    )


def test_stream_coalesces_full_get_refreshes_and_fails_closed_on_revision_mismatch(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "stream.js", "api.js", "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{
          createSnapshotStore,
          INCONSISTENT,
        }} from {json.dumps((tmp_path / "store.mjs").as_uri())};
        import {{ startRevisionStream }} from {json.dumps(uri)};

        class FakeEventSource {{
          static instances = [];
          constructor(url) {{
            this.url = url;
            this.closed = false;
            this.listeners = new Map();
            FakeEventSource.instances.push(this);
          }}
          addEventListener(name, listener) {{ this.listeners.set(name, listener); }}
          emit(name, data = undefined) {{ this.listeners.get(name)?.({{ data }}); }}
          close() {{ this.closed = true; }}
        }}

        const store = createSnapshotStore({{ scheduleNotification: (callback) => callback() }});
        const baseline = {snapshot};
        const fetchCalls = [];
        let resolveRefresh;
        let fetchBehavior = ({{ signal }}) => {{
          fetchCalls.push(signal);
          if (fetchCalls.length === 1) return Promise.resolve(structuredClone(baseline));
          return new Promise((resolve) => {{ resolveRefresh = resolve; }});
        }};
        const fetchSnapshot = (options) => fetchBehavior(options);
        const controller = new AbortController();
        const stream = startRevisionStream({{
          store,
          signal: controller.signal,
          fetchSnapshot,
          EventSourceConstructor: FakeEventSource,
          setTimeoutFn: () => 1,
          clearTimeoutFn: () => undefined,
          random: () => 0.5,
          now: () => Date.parse("2026-08-16T12:00:05Z"),
        }});
        await stream.ready;
        assert.equal(fetchCalls.length, 1);
        assert.equal(FakeEventSource.instances.length, 1);
        assert.equal(FakeEventSource.instances[0].url, "/api/v1/predictions-events");
        FakeEventSource.instances[0].emit("open");

        const revision = "e".repeat(64);
        const first = stream.requestRefresh({{ announcedRevisionId: revision }});
        const second = stream.requestRefresh({{ announcedRevisionId: revision }});
        assert.equal(first, second);
        assert.equal(fetchCalls.length, 2);
        const replacement = structuredClone(baseline);
        replacement.revision_id = revision;
        replacement.live_ledger.realized_pnl_usd = "8.25";
        resolveRefresh(replacement);
        await first;
        await stream.whenIdle();
        assert.equal(fetchCalls.length, 2);
        assert.equal(store.getState().snapshot.revision_id, revision);
        assert.equal(store.getState().snapshot.live_ledger.realized_pnl_usd, "8.25");

        let mismatchFetches = 0;
        fetchBehavior = ({{ signal }}) => {{
          assert.equal(signal.aborted, false);
          mismatchFetches += 1;
          const value = structuredClone(replacement);
          value.live_ledger.realized_pnl_usd = "999999";
          return Promise.resolve(value);
        }};
        const announced = "f".repeat(64);
        FakeEventSource.instances[0].emit("revision", JSON.stringify({{
          schema_version: 1,
          revision_id: announced,
          as_of: baseline.as_of,
          emitted_at: baseline.as_of,
          changed_domains: ["ledger"],
          realized_pnl_usd: "999999999",
        }}));
        await stream.whenIdle();
        assert.equal(mismatchFetches, 2);
        assert.equal(store.getState().connectionState, INCONSISTENT);
        assert.equal(store.getState().snapshot.revision_id, revision);
        assert.equal(store.getState().snapshot.live_ledger.realized_pnl_usd, "8.25");

        let resetFetches = 0;
        fetchBehavior = () => {{
          resetFetches += 1;
          const value = structuredClone(replacement);
          value.revision_id = announced;
          return Promise.resolve(value);
        }};
        FakeEventSource.instances[0].emit("reset", JSON.stringify({{
          schema_version: 1,
          latest_revision_id: announced,
          emitted_at: baseline.as_of,
          reason: "CURSOR_NOT_AVAILABLE",
        }}));
        await stream.whenIdle();
        assert.equal(resetFetches, 1);
        assert.equal(store.getState().snapshot.revision_id, announced);
        controller.abort();
        """
    )


def test_stream_native_reconnect_polling_staleness_and_abort_are_bounded(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "stream.js", "api.js", "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{
          CONNECTED,
          DEGRADED,
          STALE,
          DISCONNECTED,
          createSnapshotStore,
        }} from {json.dumps((tmp_path / "store.mjs").as_uri())};
        import {{ startRevisionStream, startBoundedSnapshotPolling }} from {json.dumps(uri)};

        assert.equal(typeof startBoundedSnapshotPolling, "function");
        class FakeEventSource {{
          static instances = [];
          constructor(url) {{
            this.url = url;
            this.closed = false;
            this.listeners = new Map();
            FakeEventSource.instances.push(this);
          }}
          addEventListener(name, listener) {{ this.listeners.set(name, listener); }}
          emit(name, data = undefined) {{ this.listeners.get(name)?.({{ data }}); }}
          close() {{ this.closed = true; }}
        }}

        let timerId = 0;
        const timers = new Map();
        const cleared = [];
        const setTimeoutFn = (callback, delay) => {{
          timerId += 1;
          timers.set(timerId, {{ callback, delay }});
          return timerId;
        }};
        const clearTimeoutFn = (id) => {{ cleared.push(id); timers.delete(id); }};
        const store = createSnapshotStore({{ scheduleNotification: (callback) => callback() }});
        const baseline = {snapshot};
        let mode = "success";
        let activeSignal;
        const fetchSnapshot = ({{ signal }}) => {{
          activeSignal = signal;
          if (mode === "failure") return Promise.reject(new Error("DATABASE_UNAVAILABLE"));
          if (mode === "pending") return new Promise(() => undefined);
          return Promise.resolve(structuredClone(baseline));
        }};
        const controller = new AbortController();
        const stream = startRevisionStream({{
          store,
          signal: controller.signal,
          fetchSnapshot,
          EventSourceConstructor: FakeEventSource,
          setTimeoutFn,
          clearTimeoutFn,
          random: () => 0.5,
          now: () => Date.parse("2026-08-16T12:00:05Z"),
          pollIntervalMs: 5000,
          reconnectBaseMs: 1000,
          reconnectCeilingMs: 16000,
        }});
        await stream.ready;
        const firstSource = FakeEventSource.instances[0];
        firstSource.emit("open");
        assert.equal(store.getState().connectionState, CONNECTED);

        firstSource.emit("error");
        assert.equal(firstSource.closed, false);
        assert.equal(store.getState().connectionState, DEGRADED);
        const scheduledDelays = [...timers.values()].map((item) => item.delay);
        assert.equal(scheduledDelays.includes(1000), false);
        assert.equal(scheduledDelays.includes(5000), true);

        mode = "failure";
        const poll = [...timers.entries()].find(([_id, item]) => item.delay === 5000);
        assert.ok(poll);
        timers.delete(poll[0]);
        await poll[1].callback();
        await stream.whenIdle();
        assert.equal(store.getState().connectionState, DEGRADED);
        assert.equal([...timers.values()].some((item) => item.delay === 5000), true);

        mode = "success";
        firstSource.emit("open");
        assert.equal(FakeEventSource.instances.length, 1);
        assert.equal(firstSource.closed, false);
        assert.equal(store.getState().connectionState, DEGRADED);
        await stream.requestRefresh();
        assert.equal(store.getState().connectionState, CONNECTED);

        mode = "failure";
        await stream.requestRefresh();
        assert.equal(store.getState().connectionState, DEGRADED);
        assert.equal([...timers.values()].some((item) => item.delay === 5000), true);

        mode = "pending";
        const pending = stream.requestRefresh();
        assert.equal(activeSignal.aborted, false);
        controller.abort();
        assert.equal(activeSignal.aborted, true);
        assert.equal(FakeEventSource.instances.every((source) => source.closed), true);
        assert.equal(timers.size, 0);
        assert.ok(cleared.length > 0);
        const stoppedState = store.getState().connectionState;
        firstSource.emit("error");
        assert.equal(store.getState().connectionState, stoppedState);
        void pending;

        const staleStore = createSnapshotStore({{
          scheduleNotification: (callback) => callback(),
        }});
        const staleController = new AbortController();
        const staleStream = startRevisionStream({{
          store: staleStore,
          signal: staleController.signal,
          fetchSnapshot: () => Promise.resolve(structuredClone(baseline)),
          EventSourceConstructor: FakeEventSource,
          setTimeoutFn,
          clearTimeoutFn,
          random: () => 0.5,
          now: () => Date.parse("2026-08-16T12:02:00Z"),
          staleAfterMs: 60000,
        }});
        await staleStream.ready;
        FakeEventSource.instances.at(-1).emit("open");
        assert.equal(staleStore.getState().connectionState, STALE);
        staleController.abort();
        """
    )


def test_snapshot_polling_reschedules_in_finally_after_refresh_failure(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "stream.js", "api.js", "store.js")
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ startBoundedSnapshotPolling }} from {json.dumps(uri)};

        let timerId = 0;
        const timers = new Map();
        const setTimeoutFn = (callback, delay) => {{
          timerId += 1;
          timers.set(timerId, {{ callback, delay }});
          return timerId;
        }};
        let pollAttempts = 0;
        const polling = startBoundedSnapshotPolling({{
          refresh: async () => {{ pollAttempts += 1; throw new Error("POLL-CANARY"); }},
          setTimeoutFn,
          clearTimeoutFn: (id) => timers.delete(id),
          intervalMs: 7000,
        }});
        const firstPoll = [...timers.entries()].find(([_id, item]) => item.delay === 7000);
        assert.ok(firstPoll);
        timers.delete(firstPoll[0]);
        await firstPoll[1].callback();
        assert.equal(pollAttempts, 1);
        assert.equal([...timers.values()].some((item) => item.delay === 7000), true);
        polling.stop();
        """
    )


def test_stream_deadline_starts_sse_independently_and_retains_polling(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "stream.js", "api.js", "store.js")
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{
          DEGRADED,
          createSnapshotStore,
        }} from {json.dumps((tmp_path / "store.mjs").as_uri())};
        import {{ startRevisionStream }} from {json.dumps(uri)};

        let timerId = 0;
        const timers = new Map();
        const setTimeoutFn = (callback, delay) => {{
          timerId += 1;
          timers.set(timerId, {{ callback, delay }});
          return timerId;
        }};
        const clearTimeoutFn = (id) => timers.delete(id);

        class FakeEventSource {{
          static instances = [];
          constructor(url) {{
            this.url = url;
            this.closed = false;
            this.listeners = new Map();
            FakeEventSource.instances.push(this);
          }}
          addEventListener(name, listener) {{ this.listeners.set(name, listener); }}
          emit(name, data = undefined) {{ this.listeners.get(name)?.({{ data }}); }}
          close() {{ this.closed = true; }}
        }}

        let requestSignal;
        const store = createSnapshotStore({{ scheduleNotification: (callback) => callback() }});
        const controller = new AbortController();
        const stream = startRevisionStream({{
          store,
          signal: controller.signal,
          fetchSnapshot: ({{ signal }}) => {{
            requestSignal = signal;
            return new Promise(() => undefined);
          }},
          EventSourceConstructor: FakeEventSource,
          setTimeoutFn,
          clearTimeoutFn,
          random: () => 0.5,
          requestTimeoutMs: 250,
          pollIntervalMs: 5000,
        }});

        assert.equal(FakeEventSource.instances.length, 1);
        assert.equal(FakeEventSource.instances[0].url, "/api/v1/predictions-events");
        FakeEventSource.instances[0].emit("open");
        const deadline = [...timers.entries()].find(([_id, item]) => item.delay === 250);
        assert.ok(deadline);
        timers.delete(deadline[0]);
        deadline[1].callback();
        await stream.ready;
        assert.equal(requestSignal.aborted, true);
        assert.equal(store.getState().snapshot, null);
        assert.equal(store.getState().connectionState, DEGRADED);
        assert.equal([...timers.values()].some((item) => item.delay === 5000), true);
        controller.abort();
        """
    )


def test_stream_manually_retries_only_event_source_construction_failures(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "stream.js", "api.js", "store.js")
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ createSnapshotStore }} from {json.dumps((tmp_path / "store.mjs").as_uri())};
        import {{ startRevisionStream }} from {json.dumps(uri)};

        let constructionAttempts = 0;
        const sources = [];
        class ConstructionFlakyEventSource {{
          constructor(url) {{
            constructionAttempts += 1;
            if (constructionAttempts === 1) throw new Error("CONSTRUCTION-CANARY");
            this.url = url;
            this.closed = false;
            this.listeners = new Map();
            sources.push(this);
          }}
          addEventListener(name, listener) {{ this.listeners.set(name, listener); }}
          emit(name, data = undefined) {{ this.listeners.get(name)?.({{ data }}); }}
          close() {{ this.closed = true; }}
        }}
        let timerId = 0;
        const timers = new Map();
        const setTimeoutFn = (callback, delay) => {{
          timerId += 1;
          timers.set(timerId, {{ callback, delay }});
          return timerId;
        }};
        const controller = new AbortController();
        const stream = startRevisionStream({{
          store: createSnapshotStore({{ scheduleNotification: (callback) => callback() }}),
          signal: controller.signal,
          fetchSnapshot: () => Promise.resolve(structuredClone({snapshot})),
          EventSourceConstructor: ConstructionFlakyEventSource,
          setTimeoutFn,
          clearTimeoutFn: (id) => timers.delete(id),
          random: () => 0.5,
          reconnectBaseMs: 1000,
          reconnectCeilingMs: 16000,
        }});
        await stream.ready;
        assert.equal(sources.length, 0);
        const retry = [...timers.entries()].find(([_id, item]) => item.delay === 1000);
        assert.ok(retry);
        timers.delete(retry[0]);
        retry[1].callback();
        assert.equal(sources.length, 1);
        sources[0].emit("error");
        assert.equal(sources[0].closed, false);
        assert.equal(constructionAttempts, 2);
        assert.equal([...timers.values()].some((item) => item.delay === 2000), false);
        controller.abort();
        """
    )


def test_charts_create_accessible_bounded_svg_nodes_and_explicit_fallbacks(
    tmp_path: Path,
) -> None:
    uri = module_uri(tmp_path, "charts.js")
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ sparklineSvg, depthBarsSvg, freshnessArcSvg }} from {json.dumps(uri)};

        const SVG_NS = "http://www.w3.org/2000/svg";
        class FakeNode {{
          constructor(namespace, tagName) {{
            this.namespaceURI = namespace;
            this.tagName = tagName;
            this.attributes = new Map();
            this.children = [];
            this.textContent = "";
            this._className = "";
          }}
          get className() {{ return this._className; }}
          set className(value) {{
            if (this.namespaceURI === SVG_NS) {{
              throw new TypeError("SVGElement.className is read-only in this runtime");
            }}
            this._className = String(value);
          }}
          setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
          append(...children) {{ this.children.push(...children); }}
          query(tagName) {{
            if (this.tagName === tagName) return this;
            for (const child of this.children) {{
              const found = child.query?.(tagName);
              if (found) return found;
            }}
            return null;
          }}
        }}
        const documentRef = {{
          createElement: (tagName) => new FakeNode(null, tagName),
          createElementNS: (namespace, tagName) => new FakeNode(namespace, tagName),
        }};

        const empty = sparklineSvg([], {{
          documentRef,
          unavailableText: "No observations available",
        }});
        assert.equal(empty.tagName, "p");
        assert.equal(empty.textContent, "No observations available");
        const invalid = depthBarsSvg([1, Number.NaN], {{ documentRef }});
        assert.equal(invalid.tagName, "p");
        assert.match(invalid.textContent, /unavailable/i);
        const redacted = freshnessArcSvg(null, 60, {{ documentRef }});
        assert.equal(redacted.tagName, "p");
        assert.match(redacted.textContent, /unavailable/i);

        const sparkline = sparklineSvg([-Number.MAX_VALUE, 0, Number.MAX_VALUE], {{
          documentRef,
          title: "Observed evidence cadence",
          description: "Three bounded observations.",
        }});
        assert.equal(sparkline.tagName, "svg");
        assert.equal(sparkline.namespaceURI, SVG_NS);
        assert.equal(sparkline.attributes.get("role"), "img");
        assert.equal(sparkline.attributes.get("class"), "chart chart--sparkline");
        assert.ok(sparkline.attributes.get("aria-labelledby"));
        assert.equal(sparkline.query("title").textContent, "Observed evidence cadence");
        assert.equal(sparkline.query("desc").textContent, "Three bounded observations.");
        assert.equal(sparkline.query("path").attributes.get("stroke"), "currentColor");
        assert.doesNotMatch(sparkline.query("path").attributes.get("d"), /NaN|Infinity/);
        for (const child of sparkline.children) assert.equal(child.namespaceURI, SVG_NS);

        const bars = depthBarsSvg([4, 2, 8], {{ documentRef }});
        assert.equal(bars.tagName, "svg");
        assert.equal(bars.children.filter((node) => node.tagName === "rect").length, 3);
        assert.equal(bars.query("rect").attributes.get("fill"), "currentColor");

        const arc = freshnessArcSvg(75, 60, {{ documentRef }});
        assert.equal(arc.tagName, "svg");
        const circles = arc.children.filter((node) => node.tagName === "circle");
        assert.equal(circles.length, 2);
        assert.equal(circles[0].attributes.get("class"), "chart__track");
        assert.equal(circles[0].attributes.get("stroke"), "currentColor");
        assert.equal(circles[0].attributes.get("stroke-opacity"), "0.22");
        assert.doesNotMatch(circles[1].attributes.get("stroke-dasharray"), /NaN|Infinity/);
        """
    )


def test_all_five_views_render_complete_observer_evidence_without_action_affordances(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(tmp_path, "views.js", "charts.js")
    snapshot = valid_snapshot()
    second_opportunity = dict(snapshot["opportunities"][0])  # type: ignore[index]
    second_opportunity.update(
        candidate_id="00000000-0000-0000-0000-000000000000",
        conservative_surplus_usd="99.50",
        capacity_usd="500.00",
        evidence_hashes=["e" * 64],
    )
    snapshot["opportunities"] = [snapshot["opportunities"][0], second_opportunity]  # type: ignore[index]
    second_timeline = dict(snapshot["execution_timeline"][0])  # type: ignore[index]
    second_timeline.update(
        record_id="00000000-0000-0000-0000-000000000003",
        occurred_at="2026-08-16T11:59:00Z",
        state="SIGNED",
        reason_code=None,
        reconciled=True,
        evidence_hashes=["f" * 64],
    )
    snapshot["execution_timeline"] = [snapshot["execution_timeline"][0], second_timeline]  # type: ignore[index]
    snapshot["markets"] = [
        {
            "schema_version": 1,
            "venue": "polymarket",
            "market_id": "market-atlas-1",
            "question": "Will the evidence cutoff remain internally consistent?",
            "active": True,
            "closed": False,
            "retrieved_at": snapshot["as_of"],
        }
    ]
    snapshot["recipes"] = {
        "schema_version": 1,
        "recipes": ["polytrading predictions health --db $PREDICTIONS_DATABASE --format json"],
    }
    run_node_module_test(
        rf"""
        import assert from "node:assert/strict";
        import {{
          renderOverview,
          renderMarkets,
          renderExecution,
          renderLedger,
          renderEvidence,
        }} from {json.dumps(uri)};

        class FakeNode {{
          constructor(namespace, tagName) {{
            this.namespaceURI = namespace;
            this.tagName = tagName;
            this.attributes = new Map();
            this.children = [];
            this.textContent = "";
            this.className = "";
            this.hidden = false;
          }}
          setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
          append(...children) {{ this.children.push(...children); }}
          replaceChildren(...children) {{ this.children = [...children]; this.textContent = ""; }}
        }}
        const documentRef = {{
          createElement: (tagName) => new FakeNode(null, tagName),
          createElementNS: (namespace, tagName) => new FakeNode(namespace, tagName),
        }};
        const textTree = (node) => [node.textContent, ...node.children.map(textTree)].join(" ");
        const nodes = (node) => [node, ...node.children.flatMap(nodes)];
        const makeRoot = () => documentRef.createElement("section");
        const snapshot = {json.dumps(snapshot)};
        const context = {{
          connectionState: "CONNECTED",
          financialsHidden: false,
          now: () => Date.parse("2026-08-16T12:00:05Z"),
          documentRef,
        }};

        const overview = makeRoot();
        renderOverview(overview, snapshot, context);
        const overviewText = textTree(overview);
        assert.match(overviewText, /LIVE_DISABLED/);
        assert.match(overviewText, /2 observed opportunities/);
        assert.match(overviewText, /UNKNOWN/);
        assert.match(overviewText, /2026-08-16 12:00:00 UTC/);
        assert.match(overviewText, /5s ago/);
        const postureValue = nodes(overview).find((node) => node.textContent === "LIVE_DISABLED");
        assert.match(postureValue.className, /metric-card__value--token/);

        const zeroCheckpointSnapshot = structuredClone(snapshot);
        zeroCheckpointSnapshot.live_ledger.reconciliation_count = 0;
        zeroCheckpointSnapshot.live_ledger.complete_reconciliation_count = 0;
        zeroCheckpointSnapshot.live_ledger.incomplete_reconciliation_count = 0;
        const zeroCheckpointOverview = makeRoot();
        renderOverview(zeroCheckpointOverview, zeroCheckpointSnapshot, context);
        assert.match(textTree(zeroCheckpointOverview), /Reconciliation\s+UNAVAILABLE/);
        assert.match(textTree(zeroCheckpointOverview), /NO CHECKPOINT/);

        const incompleteSnapshot = structuredClone(snapshot);
        incompleteSnapshot.live_ledger.complete_reconciliation_count = 0;
        incompleteSnapshot.live_ledger.incomplete_reconciliation_count = 1;
        const incompleteOverview = makeRoot();
        renderOverview(incompleteOverview, incompleteSnapshot, context);
        assert.match(textTree(incompleteOverview), /Reconciliation\s+INCOMPLETE/);

        const markets = makeRoot();
        renderMarkets(markets, snapshot, context);
        const marketsText = textTree(markets);
        assert.match(marketsText, /Probability\s+Unavailable/);
        assert.match(marketsText, /Depth\s+Unavailable/);
        assert.match(marketsText, /Liquidity\s+Unavailable/);
        assert.match(marketsText, /Will the evidence cutoff remain internally consistent/);
        assert.ok(
          marketsText.indexOf("00000000-0000-0000-0000-000000000000") <
          marketsText.indexOf("00000000-0000-0000-0000-000000000001"),
        );

        const execution = makeRoot();
        renderExecution(execution, snapshot, context);
        const executionText = textTree(execution);
        assert.ok(executionText.indexOf("SIGNED") < executionText.indexOf("UNKNOWN"));
        assert.match(executionText, /Recovery evidence/);
        assert.match(executionText, /RECONCILIATION_ACTION_REQUIRED/);
        assert.match(executionText, new RegExp("c".repeat(64)));
        const timelineRegion = nodes(execution).find(
          (node) =>
            node.className.includes("table-region") &&
            node.attributes.get("role") === "region",
        );
        assert.ok(timelineRegion);
        assert.match(timelineRegion.attributes.get("aria-label"), /chronology/i);
        const executionTable = nodes(timelineRegion).find((node) => node.tagName === "table");
        assert.match(executionTable.className, /data-table--execution/);

        const ledger = makeRoot();
        renderLedger(ledger, snapshot, context);
        const ledgerText = textTree(ledger);
        assert.match(ledgerText, /7.75/);
        assert.match(ledgerText, /Fee detail\s+Unavailable/);
        assert.match(ledgerText, /Position detail\s+Unavailable/);
        const hiddenLedger = makeRoot();
        renderLedger(hiddenLedger, snapshot, {{
          ...context,
          connectionState: "INCONSISTENT",
          financialsHidden: true,
        }});
        const hiddenLedgerText = textTree(hiddenLedger);
        assert.doesNotMatch(hiddenLedgerText, /7.75/);
        assert.match(hiddenLedgerText, /Financial totals hidden/);
        assert.match(hiddenLedgerText, /Snapshot validation failed/);
        assert.match(hiddenLedgerText, /financial values are suppressed/);
        const integrityAlert = nodes(hiddenLedger).find(
          (node) => node.attributes.get("role") === "alert",
        );
        assert.ok(integrityAlert);

        const evidence = makeRoot();
        renderEvidence(evidence, snapshot, context);
        const evidenceText = textTree(evidence);
        assert.match(evidenceText, /polymarket-clob-2026-08-25-v1/);
        assert.match(evidenceText, new RegExp("d".repeat(64)));
        assert.match(evidenceText, /EXECUTION_KILL_ENGAGED/);
        assert.match(evidenceText, /Evidence recipes/);
        assert.match(evidenceText, /polytrading predictions health/);

        for (const root of [overview, markets, execution, ledger, evidence]) {{
          const allTags = nodes(root).map((node) => node.tagName);
          assert.equal(allTags.includes("form"), false);
          assert.equal(allTags.includes("button"), false);
          assert.equal(allTags.includes("a"), false);
          assert.equal(allTags.includes("h2"), true);
          assert.equal(root.attributes.get("aria-busy"), "false");
        }}
        """
    )


def test_five_view_styles_support_dense_but_legible_information_hierarchy() -> None:
    css = asset_text("app.css")

    for selector in (
        ".metric-grid",
        ".metric-card",
        ".panel",
        ".opportunity-grid",
        ".opportunity-card",
        ".timeline",
        ".state-badge",
        ".hash-list",
        ".empty-state",
        ".chart-fallback",
    ):
        assert selector in css
    assert "min-width: 760px" in css
    assert "text-overflow: ellipsis" not in css


def test_app_bootstrap_navigation_state_rendering_and_abort_cleanup(
    tmp_path: Path,
) -> None:
    uri = module_bundle_uri(
        tmp_path,
        "app.js",
        "api.js",
        "store.js",
        "stream.js",
        "charts.js",
        "views.js",
    )
    snapshot = json.dumps(valid_snapshot())
    run_node_module_test(
        f"""
        import assert from "node:assert/strict";
        import {{ startMarketAtlas }} from {json.dumps(uri)};

        class FakeNode {{
          constructor(namespace, tagName, id = "") {{
            this.namespaceURI = namespace;
            this.tagName = tagName;
            this.id = id;
            this.attributes = new Map();
            this.children = [];
            this.textContent = "";
            this.className = "";
            this.hidden = false;
            this.dataset = {{}};
            this.listeners = new Map();
            this.focusOptions = null;
          }}
          setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
          getAttribute(name) {{ return this.attributes.get(name) ?? null; }}
          append(...children) {{ this.children.push(...children); }}
          replaceChildren(...children) {{ this.children = [...children]; this.textContent = ""; }}
          addEventListener(name, listener) {{
            const listeners = this.listeners.get(name) ?? [];
            listeners.push(listener);
            this.listeners.set(name, listeners);
          }}
          emit(name, event = {{}}) {{
            for (const listener of this.listeners.get(name) ?? []) listener(event);
          }}
          focus(options) {{
            this.focusOptions = options;
            documentRef.activeElement = this;
          }}
          contains(candidate) {{
            return this === candidate || this.children.some((child) => child.contains?.(candidate));
          }}
        }}
        const ids = new Map();
        const register = (id, tagName = "div") => {{
          const node = new FakeNode(null, tagName, id);
          ids.set(id, node);
          return node;
        }};
        const body = register("body", "body");
        const viewRoot = register("view-root", "section");
        const tabs = ["overview", "markets", "execution", "ledger", "evidence"].map((view) => {{
          const tab = register(`tab-${{view}}`, "button");
          tab.dataset.view = view;
          tab.setAttribute("aria-selected", view === "overview" ? "true" : "false");
          tab.setAttribute("tabindex", view === "overview" ? "0" : "-1");
          return tab;
        }});
        for (const [id, tag] of [
          ["main", "main"],
          ["view-title", "h1"],
          ["view-context", "p"],
          ["connection-state", "dd"],
          ["implementation-state", "dd"],
          ["kill-state", "dd"],
          ["snapshot-cutoff", "dd"],
          ["last-refresh", "dd"],
          ["update-summary", "p"],
          ["connection-overlay", "div"],
          ["connection-overlay-title", "p"],
          ["connection-overlay-detail", "p"],
          ["as-of", "p"],
          ["candidates-summary", "p"],
          ["candidates", "div"],
          ["candidates-empty", "p"],
          ["proofs-summary", "p"],
          ["proofs", "div"],
          ["proofs-empty", "p"],
          ["scans-summary", "p"],
          ["scans", "div"],
          ["scans-empty", "p"],
          ["shadow-summary", "p"],
          ["shadow-proposals", "div"],
          ["shadow-empty", "p"],
          ["stale-notice", "p"],
        ]) register(id, tag);
        const documentRef = {{
          body,
          activeElement: null,
          getElementById: (id) => ids.get(id) ?? null,
          querySelectorAll: (selector) => selector === "[data-view]" ? tabs : [],
          createElement: (tagName) => new FakeNode(null, tagName),
          createElementNS: (namespace, tagName) => new FakeNode(namespace, tagName),
        }};
        const windowListeners = new Map();
        const windowRef = {{
          addEventListener(name, listener) {{ windowListeners.set(name, listener); }},
          removeEventListener(name) {{ windowListeners.delete(name); }},
        }};
        const frames = [];
        let streamClosed = false;
        let streamSignal;
        const startStream = (options) => {{
          streamSignal = options.signal;
          options.store.replaceSnapshot({snapshot});
          options.store.setConnectionState("CONNECTED");
          return {{ ready: Promise.resolve(), close: () => {{ streamClosed = true; }} }};
        }};
        const app = startMarketAtlas({{
          documentRef,
          windowRef,
          startStream,
          scheduleNotification: (callback) => callback(),
          requestAnimationFrameFn: (callback) => {{ frames.push(callback); return frames.length; }},
          cancelAnimationFrameFn: () => undefined,
          now: () => Date.parse("2026-08-16T12:00:05Z"),
        }});
        const flushFrames = () => {{ while (frames.length) frames.shift()(); }};
        const textTree = (node) => [node.textContent, ...node.children.map(textTree)].join(" ");
        await app.ready;
        flushFrames();

        assert.equal(ids.get("view-title").textContent, "Overview");
        assert.match(textTree(viewRoot), /LIVE_DISABLED/);
        assert.equal(ids.get("connection-state").textContent, "CONNECTED");
        assert.equal(ids.get("implementation-state").textContent, "LIVE_DISABLED");
        assert.match(ids.get("kill-state").textContent, /ENGAGED/);
        assert.match(ids.get("snapshot-cutoff").textContent, /2026-08-16 12:00:00 UTC/);
        assert.match(ids.get("update-summary").textContent, /CONNECTED/);

        let prevented = false;
        tabs[0].emit("keydown", {{
          key: "ArrowRight",
          preventDefault: () => {{ prevented = true; }},
        }});
        flushFrames();
        assert.equal(prevented, true);
        assert.equal(tabs[1].getAttribute("aria-selected"), "true");
        assert.equal(tabs[1].getAttribute("tabindex"), "0");
        assert.equal(documentRef.activeElement, tabs[1]);
        assert.deepEqual(tabs[1].focusOptions, {{ preventScroll: true }});
        assert.equal(ids.get("view-title").textContent, "Markets");
        assert.match(textTree(viewRoot), /Ranked opportunities/);

        tabs[1].emit("keydown", {{ key: "End", preventDefault: () => undefined }});
        flushFrames();
        assert.equal(ids.get("view-title").textContent, "Evidence");
        assert.equal(documentRef.activeElement, tabs[4]);
        tabs[4].emit("keydown", {{ key: "Home", preventDefault: () => undefined }});
        flushFrames();
        assert.equal(ids.get("view-title").textContent, "Overview");
        assert.equal(documentRef.activeElement, tabs[0]);

        for (const state of ["DEGRADED", "STALE", "DISCONNECTED", "INCONSISTENT"]) {{
          app.store.setConnectionState(state, `${{state}}_TEST`);
          flushFrames();
          assert.equal(ids.get("connection-state").textContent, state);
          assert.equal(body.getAttribute("data-connection"), state.toLowerCase());
          assert.match(ids.get("update-summary").textContent, new RegExp(state));
        }}
        assert.equal(ids.get("connection-overlay").hidden, true);
        app.store.setConnectionState("DISCONNECTED", "CHANNELS_UNAVAILABLE");
        flushFrames();
        assert.equal(ids.get("connection-overlay").hidden, false);

        app.selectView("ledger");
        app.store.setConnectionState("INCONSISTENT", "REVISION_MISMATCH");
        flushFrames();
        assert.match(textTree(viewRoot), /Financial totals hidden/);
        assert.doesNotMatch(textTree(viewRoot), /7.75/);

        windowListeners.get("pagehide")();
        assert.equal(streamSignal.aborted, true);
        assert.equal(streamClosed, true);
        """
    )


def test_app_source_keeps_navigation_and_rendering_accessible_and_observer_only() -> None:
    source = asset_text("app.js")

    for contract in (
        "ArrowRight",
        "ArrowLeft",
        "Home",
        "End",
        "aria-selected",
        "aria-labelledby",
        "requestAnimationFrame",
        "AbortController",
        "aria-busy",
        "preventScroll",
    ):
        assert contract in source
    assert "window.location" not in source
    assert "history.pushState" not in source
    assert "sessionStorage" not in source
    assert "localStorage" not in source
