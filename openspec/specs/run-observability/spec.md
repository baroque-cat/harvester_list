# run-observability Specification

## Purpose

Guarantees that what the pipeline measures is what an operator can read: every run-metric surface published on the pipeline status object has a rendering path into status output, rendering is deterministic, fail-open and structurally incapable of leaking credential material, and derived figures (byte volume, latency percentiles) are published in a form that answers the acceptance questions without grepping logs.

## Requirements

### Requirement: Every published metric surface has an operator rendering path

A run-metric surface that the pipeline publishes on its status object SHALL be reachable from operator status output. Publishing a surface that nothing renders SHALL be treated as a defect, not as an implementation detail: a counter that no operator can read cannot be used to accept or reject a run, and acceptance criteria that cite it become unexecutable. Rendered figures SHALL appear in the detailed status rendering, which is produced both periodically during a run and once at shutdown.

#### Scenario: RO-S1 collected surfaces are rendered in detailed status output
- **WHEN** a run finishes having collected transport, aggregation and refinement governance figures
- **THEN** detailed status output contains one line per non-empty surface naming its principal figures — transport: requests and delivered bytes per transport, latency percentiles, refusals by class and deferrals by cause; aggregation: cache hits, misses, joins, entries and bytes; refinement: mode, children generated and admitted, refusals by reason, remaining budget and the coverage estimate

#### Scenario: RO-S2 an empty or absent surface renders nothing
- **WHEN** a metric surface is empty, or is not present on the status object at all
- **THEN** its line is omitted entirely, no placeholder or zero-filled line is printed, and rendering does not raise

#### Scenario: RO-S3 rendering is deterministic
- **WHEN** the same run state is rendered twice
- **THEN** both renderings are byte-identical in labels, field order and values, so output can be diffed between runs

#### Scenario: RO-S4 no published surface lacks a renderer
- **WHEN** the set of run-metric surfaces published on the status object is enumerated
- **THEN** every member of that set is covered by a rendering path, and a newly published surface without one is reported by the completeness check rather than silently invisible

### Requirement: Rendering is allowlisted, fail-open and free of credential material

A renderer SHALL read only the figures it declares, never the whole of a metric surface, so that a producer adding a key later cannot widen operator output or leak a value through it. Rendered lines SHALL carry only counters, rates, mode identifiers and enumerated labels: no credential, token, cookie, authorization value, filesystem path or payload content. A missing, malformed or unexpected figure SHALL degrade the line (omit the field or report zero) and SHALL NOT raise into the status path, which runs on the shutdown sequence.

#### Scenario: RO-S5 an unexpected key does not reach operator output
- **WHEN** a producer adds a key to a rendered metric surface that the renderer does not declare
- **THEN** the rendered line is unchanged, and the undeclared value does not appear in status output or logs

#### Scenario: RO-S6 rendered output carries no credential material
- **WHEN** any metric line is rendered after a run that used pooled credentials
- **THEN** the line contains no token, cookie or authorization value in any form, masked or unmasked, and no path or payload fragment

#### Scenario: RO-S7 a malformed figure degrades instead of raising
- **WHEN** a rendered metric surface holds a missing, non-numeric or out-of-range value for a declared figure
- **THEN** that field is omitted or reported as zero, the rest of the line still renders, and no exception escapes into the status or shutdown path

#### Scenario: RO-S8 compact output is unchanged
- **WHEN** status is rendered in a compact or summary mode rather than detailed
- **THEN** the added metric lines do not appear, so periodic compact output keeps its existing shape and width

### Requirement: Derived transport figures answer the acceptance questions

Published transport figures SHALL be sufficient to answer, from status output alone, how much content a run fetched per file and how fast: delivered bytes and request counts per transport, from which the mean bytes per file is computable, and the 50th and 99th percentiles of fetch latency in milliseconds per transport. Latency percentiles SHALL be derived from a fixed-band distribution whose memory does not grow with the number of requests, SHALL be reported as the upper edge of the band containing the target rank so a published percentile never understates the measured one, and SHALL count only fetches that actually reached the network. Resetting the transport surface SHALL reset the latency distribution with it.

#### Scenario: RO-S9 percentiles come from the band containing the rank
- **WHEN** a known set of fetch latencies is recorded across the fixed millisecond bands
- **THEN** the published 50th and 99th percentiles are the upper edges of the bands containing those ranks, which is at or above the true percentile and never below it

#### Scenario: RO-S10 resetting the surface resets latency with it
- **WHEN** the transport metric surface is reset between runs
- **THEN** sample counts return to zero and published percentiles report no data, so a fresh run cannot inherit the previous run's latency distribution

#### Scenario: RO-S11 mean bytes per file is computable from published output
- **WHEN** a run gathered files over one transport
- **THEN** the published byte total and request count for that transport allow the mean bytes per fetched file to be computed without reading log lines or inspecting harvested data
