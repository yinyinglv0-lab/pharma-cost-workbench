# Active synthetic manufacturing bundles (round 2)

**All factories, products, quantities, prices, rates, reference ranges and documents in this directory are SIMULATION fixtures, not production observations or independently researched industry benchmarks.** Importing an example must not grant permissions or dispatch a real task.

The five examples (`pharma`, `machinery`, `auto_parts`, `chemicals`, `electronics`) all execute the identical `enterprise.manufacturing_runtime` core. Each declares two products, both home/peer factories, May and June 2026, and five complete CSV table families. Pharma deliberately adds simulated fourth/fifth product definitions in the new `tablet` reporting unit; it does not modify the three legacy products or derive tablets from boxes. Chemical production reports `kg`; the other three industries report `piece`. There is no relabelling into pharmaceutical factory names or box denominators.

## Files in each industry directory

- `domain.json`: active `manufacturing-domain/2` reference semantics, labels, exact home/peer identities, BOM, materials, process/equipment relationships and metric vocabulary.
- `adapter.json`: active `manufacturing-adapter/2`, explicit source column/unit/currency mappings. Same-currency and same-unit factors are declared explicitly.
- `actual.csv`, `budget.csv`, `materials.csv`, `labor.csv`, `overhead.csv`: exact decimal text, complete 2 products × 2 factories × 2 months.
- `knowledge.txt`: clearly marked simulation reference document with explicit product/process/equipment/BOM semantics and boundaries; ingest only through the existing controlled knowledge release pipeline.
- `manifest.json`: explicit periods, data classification and the conventional fixture filenames. This is **documentation only**, not a runtime loader or URL/path configuration capability.

## Pure API contract

```python
from enterprise.manufacturing_runtime import build_manufacturing_runtime
runtime = build_manufacturing_runtime(profile, adapter_config, tables=tables,
                                      periods=['2026-05', '2026-06'])
facts = runtime.analysis(product=exact_name, specification=exact_spec,
                         month='2026-06')
frozen_revision = runtime.to_dict()
assert runtime.verify(profile=profile, adapter_config=adapter_config).fingerprint == runtime.fingerprint
```

`tables` has **exactly** `actual`, `budget`, `materials`, `labor`, `overhead`. Each value has exactly `{columns: list[str], rows: list[list[str]], source_id: str, source_sha256: str}`. `source_sha256` is SHA-256 of original file bytes; the pure runtime treats it as caller-attested. A repository must independently verify/store the bytes, preserve immutable revisions and compare current configuration hashes. Header order may differ; duplicate/missing/unknown columns fail. Cells are strings, not floats or inferred nulls. The pure core has no I/O, security grants, executable configuration, external URL resolution, model calls or RPA effects.

Actual and budget share the 13 explicitly mapped adapter columns. Detail headers are exported as `DETAIL_COLUMNS`:

- materials: `factory,product,specification,month,material_id,unitcost,amount,currency,reporting_unit,quantity,quantity_unit,unit_price`
- labor: `factory,product,specification,month,amount,currency,reporting_unit,hours,headcount,working_days,hourly_rate`
- overhead: `factory,product,specification,month,category,unitcost,amount,currency,reporting_unit`

**Complete-bundle policy**: actual and budget cover all configured products × both configured factories × all declared, ordered, contiguous periods. Every actual row has all three detail families; material lines cover its explicit BOM. A partial import must first be merged by a versioned repository with an existing confirmed bundle; this pure API rejects missing coverage instead of filling gaps. Only current home facts are analyzed; peer records are used explicitly for same-specification benchmarking. First observed month has an explicit unavailable previous-period comparison.

Every detail amount closes **exactly** to the corresponding actual component amount. Detail currency/reporting units must already equal canonical target (no hidden detail conversions). Optional material `quantity`, `quantity_unit`, `unit_price` and labor `hours`, `headcount`, `working_days`, `hourly_rate` use **empty cells for unavailable**. Material quantity+unit travel together; any supplied price × observed quantity must equal amount exactly. Hourly rate requires supplied hours and exact rate×hours=amount. Empty cells are not zero. Material quantities never come from BOM, reference prices or amount/price inference.

## Neutral analysis output

`manufacturing-facts/1` exports `scope`, `measurement`, `labels`, `data_classification`, `current`, `period`, `budget`, `benchmark`, `details`, `peer_details`, `metrics`, `domain_context`, `provenance`, `limitations`. `metrics` executes only fixed element-share and unit-cost calculations; `source_only` metrics such as yield are explicitly unavailable without observations and are never derived from BOM or costs.

- Amounts/output/unit costs are exact decimal **strings**.
- Percentages/ratios have exact numerator, denominator, scale plus a six-place half-even `value` explicitly marked display-only; zero denominator returns `available: false`, never infinity or fabricated zero.
- `period.elements` and `budget.elements` retain exact amount deltas, volume and unit-cost effects; each bridge closes exactly.
- `benchmark.elements` compares exact same product/spec/month/denominator and uses home output to standardize the amount gap, distinct from raw totals or causal efficiency claims.
- `details.materials` supplies actual quantity/price bridges **only when both source periods supply both observations**; otherwise it records a specific unavailable reason. Same discipline applies to labor hours/rate bridges.
- `domain_context` contains explicit reference semantics, not current-period observations or mechanism proof. Knowledge document release/eligibility, model prompt and validated task mapping remain application concerns.

Do not convert these canonical fields into pharmacy headers as a production integration strategy. A context projection may adapt key structure, but must preserve measurement, factory identity, source authority and exact quantitative evidence.

## Offline validation

On 2026-09-23, `python -m pytest tests/test_round2_manufacturing_runtime.py tests/test_manufacturing_adapter.py -q` passed **428 tests**: 173 canonical runtime cases plus all 255 pre-existing adapter cases. This establishes the deterministic core contract and fixture execution, not real-model response quality, source authenticity or dispatch acceptance. Independent read-only probes identified and prompted regression coverage for source-ID role delimiters and an attempted domain/2→adapter/1 schema downgrade.

## Safety / scope

The legacy `manufacturing-domain/1` and `manufacturing-adapter/1` contracts remain supported unchanged by their existing APIs. The new runtime requires **domain/2 plus adapter/2** and does not automatically migrate legacy unit assumptions or allow a schema downgrade to bypass canonical text validation. Package units such as `box` and `tablet` are distinct identity units: no count/box conversion exists without a separately evidenced future contract. Closed unit conversions, exact currency factors, schema allowlists, unknown-key rejection and immutable provenance are enforced.

The runtime is an implemented import-to-analysis foundation, **not a claim that UI/API/model/knowledge release/task repository are already integrated**. Application wiring must be demonstrated separately; real model calls, real dispatch, OIDC, human quality scoring and production source authenticity are outside these offline fixtures.
