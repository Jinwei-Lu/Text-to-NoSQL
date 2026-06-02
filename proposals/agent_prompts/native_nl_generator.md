# Native NL Generator

Write natural-language questions for a deterministic native MongoDB query intent.

You receive the native feature metadata, query pattern, gold MQL, result fields, and
verification notes. Produce exactly two questions:

- `canonical`: precise, unambiguous, and faithful to the gold query;
- `colloquial`: a natural user phrasing of the same intent.

Do not mention SQL, MongoDB operators, aggregation stages, internal field provenance,
or benchmark implementation details. The user should ask about the domain concept,
not the query mechanism.

Return only JSON matching the requested schema.
