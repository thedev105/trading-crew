# AI Augmentation for the Market-Neutral Opportunity Router

Date: 2026-08-12

Status: Approved architecture; specification ready for final user review

Companion to: [Market-Neutral Opportunity Router Design](2026-08-12-market-neutral-opportunity-router-design.md)

## 1. Objective

Use artificial intelligence to expand the number of markets the system can analyze, improve estimates of carry and execution risk, and make research more efficient without giving a probabilistic model control over funds.

The governing principle is:

> AI scouts and estimates; deterministic software proves, constrains, and executes.

AI is not a new evidence class. Every AI-assisted candidate remains Class G, C, or S under the master design:

- Class G still requires deterministic payout proof.
- Class C still requires deterministic compatibility, capital, margin, cost, and stress gates.
- Direct AI forecasts and every proposal whose profit depends on a fitted model remain Class S.

This design covers AI components, data, permissions, evaluation, monitoring, and phased activation. It does not authorize live AI trading.

## 2. Constraints and Decisions

- Initial portfolio remains below USD 10,000 with the capital and drawdown limits in the master design.
- AI runs asynchronously outside the execution-critical path.
- AI output is untrusted until schema, source, and deterministic validation succeed.
- No AI component receives trading keys, wallet-signing authority, withdrawal access, risk-limit write access, or deployment credentials.
- An AI outage pauses new AI-assisted candidates but never blocks cancellation, hedging, exiting, reconciliation, or the kill switch.
- Model training and evaluation use point-in-time data. Pretrained-model backtests do not count as evidence when the model may have learned later outcomes.
- Model inference cost is included in strategy economics and reported separately.
- Provider and model changes are treated as new model versions and require revalidation.
- AI-generated code or configuration cannot deploy itself or modify production risk settings.
- Untrusted market rules, news, web pages, and retrieved documents are data, never system instructions.

## 3. Research Conclusion

### 3.1 Selected architecture

The selected architecture uses isolated AI sidecars around the deterministic trading kernel. The sidecars perform tasks where approximate semantic or statistical reasoning is useful:

- discovering related contracts in large market universes;
- extracting normalized fields from rules with exact source evidence;
- estimating funding persistence, sign reversal, and adverse exit distributions;
- estimating order fill and adverse-selection risk;
- running a forward-only event-forecasting research arena;
- assisting literature review, experiment definition, and reporting.

The trading kernel continues to own:

- payout proof;
- contract compatibility;
- fees, balances, capital, margin, and liquidation calculations;
- evidence-class assignment;
- proposal approval and sizing;
- order sequencing and execution;
- reconciliation and kill switches.

### 3.2 Rejected alternatives

| Alternative | Decision | Reason |
|---|---|---|
| End-to-end LLM trading agent | Reject | Forecasting, calibration, instruction adherence, and temporal-contamination evidence is not strong enough for autonomous capital control |
| Reinforcement-learning portfolio controller | Reject initially | Reliable counterfactual environments, cost models, and representative stress regimes are unavailable at this scale |
| LLM numerical risk engine | Reject | Balances, funding, payoff, margin, and liquidation arithmetic require deterministic reproducibility |
| Unrestricted multi-agent research system | Reject initially | Cost, correlated errors, prompt injection, and unclear accountability outweigh unproven benefits |
| AI used only as a chat interface | Insufficient | It does not create a measurable data or opportunity advantage |
| Offline-only AI with no pipeline integration | Safe fallback | Useful if sidecar validation fails, but it leaves semantic scale and continuous risk estimation unused |

## 4. Authority and Trust Boundaries

### 4.1 Zones

The system has three isolated zones:

1. **Research zone:** may access approved public data, sanitized historical datasets, and retrieval services. It contains LLMs, embeddings, and experimental models.
2. **Decision zone:** validates structured AI artifacts, runs deterministic proofs and risk calculations, and creates or rejects `TradeProposal` records.
3. **Execution zone:** holds restricted trading credentials and submits deterministic, pre-approved orders. It has no LLM dependency.

Information flows from research to decision through a one-way, versioned artifact interface. No model response is interpreted as a command.

### 4.2 Permission matrix

| Capability | AI sidecar | Deterministic decision services | Execution services |
|---|---:|---:|---:|
| Read approved public rules and news | Yes | Yes | No |
| Read sanitized historical market data | Yes | Yes | Limited |
| Read live balances or credentials | No | Balances only | Required minimum |
| Generate candidate relationships | Yes | Yes | No |
| Prove Class G payout | No | Yes | No |
| Calculate binding margin and risk | No | Yes | No |
| Increase risk limits | No | No; manual governance only | No |
| Approve or size trades | No | Yes | No |
| Place, cancel, transfer, or sign | No | No | Yes |
| Deploy code or model versions | No | No; reviewed release only | No |

## 5. AI Artifact Interface

AI components never create `TradeProposal` objects. They emit immutable candidate artifacts with:

- artifact type and unique identifier;
- input record identifiers and point-in-time cutoff;
- source URLs, hashes, timestamps, and exact supporting spans;
- model provider, exact model version, prompt/template version, and inference parameters;
- feature and training-data versions where applicable;
- structured output conforming to a strict schema;
- uncertainty, disagreement, abstention, and missing-data fields;
- inference latency and monetary cost;
- creation time, expiration time, and invalidation conditions;
- validation status and downstream disposition.

Invalid JSON, missing evidence, unknown fields, type errors, expired artifacts, or unknown model versions are rejected. Free-form reasoning is stored for audit but never parsed into executable fields.

## 6. Semantic Opportunity Scout

### 6.1 Purpose

The semantic scout reduces a large contract universe to a small set of candidate relationships for deterministic review. Its value comes from recall and scale, not authority.

### 6.2 Pipeline

1. Deterministic metadata filters group contracts by asset, event type, date range, and settlement family.
2. Embeddings retrieve potentially related contracts, including relationships not obvious from title text.
3. An LLM extracts a typed rule schema and attaches exact source spans for every critical field.
4. A separate adversarial-review prompt searches for boundary, oracle, timezone, fallback, cancellation, and scope mismatches.
5. Deterministic validators compare extracted values with parsable source fields and reject unsupported claims.
6. The payoff compiler enumerates terminal states and proves or rejects supported templates.
7. Ambiguous or novel relationships enter a manual review queue; they cannot trade automatically.

### 6.3 Critical fields

- proposition subject and scope;
- settlement oracle and exact source instrument;
- observation date, time, timezone, and window;
- comparison operator, inclusivity, threshold, unit, precision, and rounding;
- mutually exclusive and exhaustive set membership;
- cancellation, postponement, substitution, dispute, clarification, and fallback clauses;
- payout and collateral asset;
- document version, rule hash, and retrieval timestamp.

The model must emit `unknown` rather than infer a missing critical field.

### 6.4 Relationship discovery

The scout may suggest:

- complements and exhaustive sets;
- logical implications;
- nested thresholds and deadlines;
- range identities;
- cross-event implications;
- semantically similar contracts that must be rejected because one field differs.

The final category is deliberately labeled. High-quality negative examples create much of the long-term training value.

## 7. Carry Risk Forecaster

### 7.1 Purpose

Estimate distributions relevant to Class C holding risk rather than predict the underlying asset's direction.

Targets by asset, venue pair, and 7-, 14-, and 30-day horizon include:

- cumulative funding spread;
- probability and time of funding-sign reversal;
- basis divergence and convergence;
- maximum adverse funding excursion;
- executable entry and forced-exit cost;
- collateral and mark-oracle divergence;
- probability of a venue-health or liquidity danger state.

### 7.2 Candidate features

- normalized funding levels, spreads, intervals, caps, and predicted next payments;
- perp premium, futures basis, spot-perp dispersion, and term structure;
- realized volatility and jump measures;
- open-interest changes and liquidation intensity;
- order-book depth, spread, imbalance, resilience, and cross-venue fragmentation;
- mark-index-oracle divergence;
- collateral price and liquidity;
- venue outages, rejected requests, transfer status, and historical recovery time;
- calendar and known settlement effects.

Features must be available at the recorded decision time. Revised, aggregated, or later-published data cannot enter a historical row.

### 7.3 Model ladder

Models compete in increasing complexity:

1. no-change and historical-mean baselines;
2. EWMA and autoregressive baselines;
3. regime-switching or hidden-Markov models;
4. quantile gradient-boosted trees;
5. time-series transformer or foundation-model challenger.

The simplest model meeting the economic validation gate becomes champion. Complexity receives no preference.

### 7.4 Initial authority

During shadow and initial assisted operation, the forecaster may only:

- increase uncertainty and forced-exit reserves;
- veto or delay a Class C entry;
- prioritize review among proposals that already pass every deterministic gate.

The deterministic router retains allocation and sizing authority. The forecaster may not decrease reserves, raise size, extend holding limits, authorize leverage, or make a failing proposal pass. Any future increase in authority requires a new approved design.

## 8. Execution-Risk Model

### 8.1 Purpose

Estimate whether maker-first execution improves expected completed-basket economics after accounting for fill probability and adverse selection.

Outputs include:

- calibrated fill-time distribution by price level;
- probability of partial fill before artifact expiration;
- expected market movement conditional on fill;
- expected completion, hedge, and unwind cost;
- uncertainty interval and out-of-distribution flag.

### 8.2 Scope

The first model is a simple survival or hazard baseline. Neural sequence models are challengers only after sufficient venue-specific data exists. The model remains shadow-only until a continuous 60-day evaluation and every gate in Section 13.3 are complete.

Execution services continue to enforce price, quantity, time-in-force, incomplete-leg, and kill-switch limits independently.

## 9. AI Forecast Arena

### 9.1 Purpose

Test whether retrieval-augmented LLM forecasting adds information beyond executable prediction-market prices. This is a Class S research program and has no live authority.

### 9.2 Competitors

- executable market-implied probability after spread and fees;
- unconditional and category base rates;
- a simple calibrated statistical model;
- one retrieval-augmented LLM;
- an aggregated LLM ensemble only if the single-model system justifies the additional cost.

### 9.3 Point-in-time protocol

- Forecast only events unresolved at inference time.
- Retrieve only documents published before the forecast timestamp.
- Store the original document, publication time, retrieval time, query, ranking, and source hash.
- Do not count retrospective forecasts from a general pretrained model as out-of-sample evidence.
- Freeze prompts and model versions before the forward evaluation window.
- Record every forecast, including abstentions and malformed responses.
- Evaluate forecast quality and executable paper P&L separately.

### 9.4 Output

Each forecast includes the market identifier, probability, forecast horizon, base rate, market price, evidence citations, uncertainty, abstention reason, model cost, and timestamp. It never becomes a live proposal under this specification.

## 10. Research Copilot

The research copilot may:

- search and summarize primary research and official venue documentation;
- draft pre-registered hypotheses and adversarial test cases;
- identify missing data, suspicious revisions, and inconsistent schemas;
- explain experiment results and generate review reports;
- suggest code, tests, and documentation for human-reviewed development.

It may not:

- change a registered hypothesis after seeing test results;
- select only favorable experiments for reporting;
- alter raw data or experiment records;
- merge or deploy code;
- access production secrets;
- communicate with venues as an authenticated user;
- provide jurisdiction or tax conclusions without professional review.

## 11. Data and Learning Flywheel

The defensible asset is the labeled, point-in-time dataset rather than a particular vendor model.

The dataset accumulates:

- accepted and rejected rule extractions;
- exact corrections to critical fields;
- true and false semantic relationship candidates;
- proof-template outcomes and manual-review reasons;
- funding regimes, sign reversals, basis paths, and realized carry;
- predicted versus realized fill times and adverse selection;
- forecast probabilities, market baselines, resolutions, and calibration errors;
- model outages, malformed output, drift, and prompt-injection attempts.

Training, validation, and test partitions are separated by time and event family. A label produced using later information cannot be backfilled into an earlier feature row.

## 12. Security and Prompt-Injection Controls

- Treat retrieved text as quoted data inside a fixed template, never as instructions.
- Disable tools, browsing, code execution, and credential access for parsing calls.
- Allowlist artifact fields and reject model-proposed actions, URLs, or tool calls.
- Strip active content while preserving exact original text and hashes for evidence.
- Mark document origin and trust level; public user-authored rules and comments receive the lowest trust.
- Use source-span validation to ensure extracted values exist in the supplied document.
- Run adversarial tests containing direct and indirect prompt injection, misleading examples, Unicode confusables, hidden text, and contradictory clauses.
- Prevent model output from entering shell commands, SQL, templates, or logs without contextual escaping.
- Do not send secrets, balances, identity records, tax data, or proprietary credentials to external model providers.
- Review provider retention, training, residency, and deletion terms before sending non-public data.
- Rate-limit inference and enforce a hard monetary budget to contain loops or abuse.

## 13. Validation and Activation Gates

### 13.1 Semantic scout

Build a manually reviewed gold set of at least 500 contracts across at least 20 rule templates, organized into at least 250 labeled relationship pairs or sets. At least 200 labeled examples must contain adversarial boundaries, differing oracles, timezone mismatches, exception clauses, or deliberately similar non-equivalent language.

The scout may enter read-only production only if:

- critical-field exact-match is at least 99.5%;
- known-relationship candidate recall is at least 90%;
- every non-`unknown` critical field has a valid supporting source span;
- the full AI-plus-deterministic pipeline produces zero false Class G-eligible relationships on the gold set;
- malformed or unsupported output fails closed;
- changing an operator, timestamp, oracle, or fallback invalidates the relevant relationship;
- the number of pairs requiring manual review falls by at least 50% relative to reviewing every embedding-retrieved pair, without lowering the zero-false-eligibility standard.

The AI artifact alone is never sufficient for Class G eligibility, even after this gate passes.

### 13.2 Carry risk forecaster

Use at least twelve months of point-in-time history and the same 90-day frozen forward period required by the master Class C design.

The model may become a production veto and ranking sidecar only if:

- it beats the best no-change, EWMA, and autoregressive baseline on out-of-sample quantile loss;
- the empirical coverage of each reported 80% prediction interval remains between 75% and 85%;
- sign-reversal expected calibration error is no more than 0.05;
- applying its conservative filter reduces worst-decile Class C loss by at least 20%;
- the filter retains at least 75% of baseline conservative net carry;
- benefits remain after removing the best asset, venue pair, and month separately;
- benefits remain positive under doubled costs and all master stress scenarios;
- model and inference costs are included in the economic comparison;
- no deterministic Class C gate is weakened.

Failure leaves the model in research mode and does not change the underlying Class C strategy.

### 13.3 Execution-risk model

Require at least 60 continuous shadow days and 1,000 eligible hypothetical orders per supported venue. Proceed only if:

- fill-probability and fill-time scores beat deterministic queue-position baselines;
- predicted probability buckets are empirically calibrated;
- maker-first decisions improve conservative completed-basket P&L after adverse selection and missed-opportunity costs;
- results remain positive under doubled latency and costs;
- no incomplete-leg or execution-risk limit is weakened;
- performance is not concentrated in one market or short time window.

### 13.4 AI Forecast Arena

Run for at least 180 forward calendar days and 500 resolved markets. Promotion can only trigger a new design review, not live activation. Research success requires:

- positive Brier skill against executable market-implied probabilities with a 95% bootstrap confidence interval;
- positive net paper P&L after current fees, depth, slippage, model cost, and doubled transaction costs;
- Deflated Sharpe probability above 95% after all prompt and model trials are counted;
- maximum simulated drawdown below 8%;
- positive results after removing the best category, best month, and ten best trades separately;
- no category contributes more than 35% of total profit;
- calibrated probabilities rather than accuracy or confidence anecdotes;
- zero temporal leakage in an independent audit.

## 14. Model Governance and Monitoring

### 14.1 Registry

Every model has an immutable card containing:

- owner and intended use;
- prohibited uses and authority level;
- provider, model snapshot, weights or API version;
- training-data knowledge where available;
- prompt, retrieval, feature, and calibration versions;
- validation dataset and metrics;
- known limitations and failure cases;
- approval, activation, expiration, and rollback status.

### 14.2 Champion-challenger policy

- One simple champion serves each task.
- Challengers run on identical point-in-time inputs in shadow mode.
- A challenger replaces the champion only after its pre-registered economic and calibration gates pass.
- Vendor aliases such as `latest` are not valid production versions unless the returned version is recorded and separately validated.
- Rollback restores the previous model artifact without database or schema migration.

### 14.3 Drift and incidents

Monitor input distribution, missingness, abstention, disagreement, critical-field error, interval coverage, calibration, realized economic value, latency, failure rate, and cost.

Triggers include:

- rule-template or venue-specification changes;
- calibration or coverage outside approved bands;
- abnormal confidence or reduced abstention;
- source-span failure;
- material feature drift;
- model-provider change or undocumented update;
- prompt-injection or data-poisoning detection;
- inference cost or latency breach.

A trigger disables new artifacts from the affected model. Existing positions remain under deterministic monitoring and exit logic.

### 14.4 Cost policy

- Default research inference budget: the lesser of USD 25 per month and 0.3125% of total equity.
- Cache document parsing and embeddings by content hash.
- Use deterministic filters and inexpensive retrieval before LLM inference.
- Escalate only ambiguous, economically relevant candidates to a stronger model.
- Book all inference, retrieval, storage, and data costs to the relevant research or strategy ledger.
- Live-assisted operation requires projected AI cost below 10% of conservative incremental net profit attributable to the AI component.

## 15. Error Handling

- Invalid or unavailable AI output means abstain; it never causes fallback to a guessed value.
- Source or rule changes expire dependent artifacts immediately.
- Timeouts and provider outages open a circuit breaker for new AI-assisted candidates.
- Conflicting model outputs are stored and escalated; they are not averaged automatically for semantic fields.
- Out-of-distribution inputs are rejected or routed to manual review.
- Deterministic services recalculate all numeric values from raw inputs.
- A model cannot retry indefinitely; retry count, cost, and backoff are bounded.
- Every incident has a replayable packet containing inputs, versions, outputs, validations, and downstream effects.

## 16. Testing Strategy

### 16.1 Schema and boundary tests

- valid, invalid, incomplete, extra-field, and malformed artifacts;
- exact source-span and source-hash verification;
- timezones, inclusivity, rounding, cancellation, and fallback mutations;
- deterministic recomputation of every numeric field;
- artifact expiration, invalidation, and model-version rejection.

### 16.2 Adversarial AI tests

- direct and indirect prompt injection;
- instructions embedded in rule text and retrieved news;
- contradictory documents and malicious citations;
- Unicode confusables, hidden text, and extreme context length;
- high-confidence false premises and missing critical information;
- provider returning prose, tools, or actions instead of the schema.

### 16.3 Statistical tests

- rolling walk-forward evaluation with embargo for overlapping holding periods;
- leakage audit of every feature and retrieved document;
- baseline, ablation, and leave-one-group-out comparisons;
- calibration, coverage, and economic-utility metrics;
- multiple-testing correction across prompts, features, models, and thresholds;
- sensitivity to doubled and quadrupled costs;
- model and dataset version reproducibility.

### 16.4 Permission and resilience tests

- prove AI processes cannot read credential stores or execution networks;
- prove AI artifacts cannot call order endpoints;
- provider outage, slow response, corrupt response, and budget exhaustion;
- model rollback and registry revocation;
- execution, hedge, reconciliation, and kill-switch operation with all AI offline.

## 17. Dependencies and Rollout

The AI sidecars depend on master-system components but do not expand their authority:

- offline semantic extraction needs only the labeled corpus and artifact schemas;
- source-span validation needs the instrument and rule registry;
- Class G eligibility testing needs the deterministic payoff compiler and graph;
- carry modeling needs the point-in-time recorder, fee model, and Class C historical data;
- execution modeling needs venue-specific book, simulated order, and eventual paper-order outcomes;
- the Forecast Arena needs timestamped retrieval and resolution data but remains isolated from execution.

The implementation sequence is:

1. **Gold data and schemas:** label rule fields and relationships; define artifact, model-card, and evaluation schemas.
2. **Offline semantic scout:** embeddings, extraction, adversarial review, source-span and schema validation, and a gold-set report. Payoff-proof integration waits for the master Class G compiler.
3. **Carry research lab:** build baseline and challenger forecasts from master-system point-in-time data.
4. **Read-only semantic operation:** generate review candidates only after the semantic gate passes.
5. **Carry shadow operation:** emit veto and ranking counterfactuals without affecting proposals.
6. **Execution shadow model:** start only after sufficient venue-specific book and order data exists.
7. **Forecast Arena:** run independently with a fixed forward protocol and no live use.
8. **Limited assisted operation:** permit semantic candidate routing and Class C veto/ranking only after their separate gates pass.

The first AI implementation plan covers Steps 1 and 2 only. It must not contain authenticated venue access, production credentials, order submission, live forecasting, or a carry model deployment.

## 18. Success and Stop Conditions

AI augmentation succeeds if it produces measurable incremental value over deterministic and simple statistical baselines while preserving the master system's hard guarantees.

Stop or narrow an AI component when:

- it cannot beat the declared simple baseline out of sample;
- benefit disappears after model, retrieval, and data costs;
- it increases false eligibility, incomplete-leg risk, or drawdown;
- results depend on a provider version that cannot be pinned or audited;
- temporal leakage cannot be excluded;
- manual review burden or technical debt exceeds expected dollar value;
- a deterministic method achieves equivalent performance;
- security isolation cannot be demonstrated.

No AI component is retained merely because it is novel.

## 19. Primary Evidence

The design reflects the following evidence:

- Dynamic forecasting benchmarks avoid known-answer contamination. Current expert human forecasters still outperform top LLM systems, although retrieval and aggregation can approach crowd performance.
- A recent short-horizon Polymarket preprint found only two of seven evaluated models profitable, with very low reported Sharpe ratios and dangerous confidence in crypto. It supports forward research, not autonomous deployment.
- Financial-news research shows that LLMs can extract economic meaning in narrow textual tasks. It does not establish general crypto or prediction-market alpha.
- Pretrained LLMs can embed future financial information, invalidating ordinary historical backtests. Forward evaluation and point-in-time models are required.
- LLM autoformalization can help translate natural language into formal candidates, but incomplete proof success supports independent mechanical verification.
- Data-driven models can estimate limit-order fill distributions, supporting a later execution-risk sidecar after venue-specific data collection.
- Foundation time-series models show task-dependent, often small gains over simple benchmarks in noisy financial data. Complexity must compete against simple baselines.
- NIST identifies prompt injection, poisoning, privacy, and misuse risks for generative AI. Isolation and least authority are required.
- Effective model-risk management requires documented intended use, independent challenge, validation, monitoring, and governance proportional to risk.
- Production ML systems accumulate hidden technical debt through data dependencies, feedback loops, configuration, and boundary erosion. Separate sidecars and immutable artifacts reduce coupling.

Sources:

- https://proceedings.iclr.cc/paper_files/paper/2025/hash/ea74e45a229dac70b5b63b28d8934db6-Abstract-Conference.html
- https://proceedings.neurips.cc/paper_files/paper/2024/file/5a5acfd0876c940d81619c1dc60e7748-Paper-Conference.pdf
- https://arxiv.org/abs/2604.14199
- https://www.sciencedirect.com/science/article/pii/S0304405X26001066
- https://www.nber.org/papers/w35247
- https://papers.nips.cc/paper_files/paper/2022/hash/d0c6bc641a56bebee9d985b937307367-Abstract-Conference.html
- https://arxiv.org/abs/2206.01962
- https://business.columbia.edu/sites/default/files-efs/citation_file_upload/deep-lob-2021.pdf
- https://arxiv.org/abs/2606.27100
- https://arxiv.org/abs/2607.05291
- https://www.nist.gov/publications/adversarial-machine-learning-taxonomy-and-terminology-attacks-and-mitigations-0
- https://airc.nist.gov/airmf-resources/airmf/5-sec-core/
- https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm
- https://proceedings.neurips.cc/paper_files/paper/2015/hash/86df7dcfd896fcaf2674f757a2463eba-Abstract.html
