## ADDED Requirements

### Requirement: Unchanged content is not downloaded again

When a link has already been gathered successfully, holds a revalidation token captured on the transport now in use, and is presented for gathering again, the system SHALL ask the remote host whether the content changed instead of downloading it. An answer of "unchanged" SHALL be treated as a successful gather outcome that transfers no payload: extraction SHALL NOT run again, no shard record SHALL be appended, no key rows SHALL be produced or altered, and previously recorded findings SHALL remain exactly as they were. Recency SHALL still advance so the link is visibly re-observed. Any other answer SHALL fall through to the ordinary full-fetch path. Conditional revalidation SHALL NOT be attempted for a link that has never been gathered successfully, nor with a token captured on a different transport.

#### Scenario: S30 unchanged content short-circuits the download
- **WHEN** a previously gathered link is re-gathered and the host answers that the content is unchanged
- **THEN** no payload is transferred, the task completes as a success, an unchanged-content counter increments, and the latency summary records the exchange as a real network fetch

#### Scenario: S31 unchanged content preserves prior findings and advances recency
- **WHEN** a re-gather is answered as unchanged for a link whose earlier gather produced extracted values
- **THEN** the earlier values and shard records are untouched, no new key rows appear, the link's last-seen recency advances, and its successful visit status is preserved

#### Scenario: S32 a token is never replayed across transports
- **WHEN** a link holds a revalidation token captured on one transport and gather now runs on a different transport
- **THEN** the request is sent unconditionally, the full payload is processed normally, and the stored token is replaced by one from the transport actually used

#### Scenario: S33 first gathering sends no conditional header
- **WHEN** a link that has never been gathered successfully is fetched
- **THEN** the request carries no revalidation precondition, the payload is downloaded and searched in full, and any token returned is stored for later runs

#### Scenario: S34 changed content falls through to a full fetch
- **WHEN** a conditional re-gather is answered with fresh content rather than "unchanged"
- **THEN** the payload is processed exactly as an unconditional fetch would be, findings are recorded, and the stored token is updated

#### Scenario: S35 an unusable revalidation answer degrades to a full fetch
- **WHEN** a conditional request fails in a way that yields neither "unchanged" nor usable content
- **THEN** the outcome is classified by the ordinary refusal and failure rules, the task is not counted as a successful unchanged gather, and no findings are silently lost
