# High-Nesting MongoDB-Native DataWorld Design

## Status

Replacement design for the first native construction attempt. The first implementation
proved that per-database recipe files and native artifacts can be produced, but the
generated DataWorlds and MQL records were still too shallow: most collections were
table-shaped documents plus one top-level dynamic object, top-level tag array, or top-level
event array. The benchmark goal now requires semantic database restructuring first, and
NL-MQL construction second.

## Goal

TEND native mode must build MongoDB databases whose structure itself requires MongoDB
reasoning. A record should not be solvable by treating MongoDB as a thin wrapper around the
original relational schema.

The construction contract is:

1. Each database has a database-specific conversion module designed from its domain,
   source schema, foreign keys, data samples, and workload semantics.
2. Shared code provides reusable transformation primitives, but it does not decide the
   final document model for every database.
3. Each generated field is traceable to source columns or an explicit deterministic
   derivation rule.
4. Every core DataWorld has deep nested structure, not just a top-level array or object.
5. Phase B uses per-database query blueprints tied to those deep structures.
6. The final dataset is imported into MongoDB databases whose names exactly match the
   BIRD mini-dev database identifiers, and every gold MQL query is executed there.

## Non-Negotiable Complexity Gates

Each database must have at least one core collection that satisfies all of these gates:

- Maximum nested depth is at least four meaningful levels.
- At least one path has the shape `object -> dynamic key -> array -> object` or deeper.
- At least one path has the shape `array -> object -> dynamic key or attribute bag`.
- At least one query-bearing feature uses missing, null, empty-array, and present-field
  distinctions explicitly.
- At least one query-bearing feature uses a database-specific derived field, not a generic
  tag copied from a source column.
- The feature manifest records dynamic key samples, nested array length distribution,
  populated document counts, and query blueprints.
- Release validation rejects records that only vary by limit, sort, marker fields, or
  constant true predicates.

Examples of insufficient structures:

- `account` document with `transactions: [...]` only.
- `school` document with `frpm_by_year: {"2014-2015": 0.52}` only.
- `molecule` document with `atom_counts_by_element: {"c": 10}` only.
- MQL variants that differ only by `$limit`, `_id` sort, or synthetic marker fields.

## Architecture

Native construction becomes a three-layer system.

### Layer 1: Database-Specific DataWorld Builders

Each file under `src/tend/construct/native_designs/` declares the target document model for
one source database. The design is allowed, and encouraged, to restructure the database:

- Merge multiple source tables into one business document.
- Split one source table into several semantic collections.
- Create derived entities that do not exist as source tables, such as external
  counterparties, card identifiers, graph components, or basket sessions.
- Use dynamic keys where the key value is domain meaningful: month, year, power name,
  measurement code, bookmaker, status label, product id, language, revision id, or atom id.
- Use nested arrays where the source semantics are naturally ordered or repeated:
  events, comments, pit stops, lap records, lab panels, expenses, rulings, or neighbors.

### Layer 2: Shared Deep Materialization Primitives

The executor must support reusable deep transformations:

- `multi_hop_embed`: parent to child to lookup embedding.
- `nested_embed_array`: arrays whose element spec can itself contain objects, arrays,
  dynamic objects, and derived fields.
- `dynamic_attribute_bag`: dynamic keys from lookup labels or source values.
- `time_bucketed_document`: `by_month`, `by_year`, and versioned metric objects.
- `wide_column_pivot`: convert columns such as `home_player_1..11`, `B365H/B365D/B365A`,
  or `q1/q2/q3` into arrays or dynamic objects.
- `parsed_document_field`: deterministic parsing for XML, JSON-like text, tag strings,
  comma-separated lists, and purchase URL bags.
- `graph_neighborhood`: build graph adjacency, endpoint, element-pair, and neighborhood
  structures from edge tables.
- `sessionized_events`: group multiple source rows into one business event, such as a POS
  basket or same-time card transaction.
- `presence_profile`: emit explicit present, missing, null, and empty states.
- `path_lineage`: record source columns, join path, formula, and optional source row ids
  for nested output paths.

### Layer 3: Per-Database Query Blueprints

Phase B no longer expands four generic templates. Each native design emits query blueprints
that describe what kind of NL-MQL task is valid for that database. A blueprint includes:

- Natural-language intent family.
- Target collection and nested path.
- Required MongoDB constructs, such as `$objectToArray`, `$getField`, `$filter`, `$map`,
  `$reduce`, `$switch`, `$type`, `$setWindowFields`, or nested `$unwind`.
- Required data witnesses, such as at least one matching dynamic key or non-empty nested
  event array.
- Expected result shape and required output fields.
- Provenance references for all fields needed by the query.

## Database Targets

### financial

Target collections:

- `bank_accounts`: account ledger documents with branch profile, participants, cards,
  loan, monthly cashflow, transaction events, standing orders, and presence state.
- `financial_party_graph`: polymorphic account, client, card, loan, branch, and external
  counterparty nodes with typed edges.
- `regional_bank_markets`: district-level market documents with versioned metrics and
  embedded account, loan, and cashflow rollups.
- `counterparty_flows`: external bank/account flow documents derived from transaction and
  standing-order counterparties.

Example native query families:

- Compare withdrawals and credits inside `cashflow_by_month.<month>` against nested branch
  salary metrics.
- Compare standing-order purpose keys with transaction symbol keys via `$objectToArray`
  and `$setDifference`.
- Use `$switch` over `financial_party_graph.entity_type` to read subtype-specific risk
  fields.

### debit_card_specializing

Target collections:

- `fuel_customers`: customer profiles with long-term `consumption_by_month`, card dynamic
  objects, POS event samples, and product bags.
- `fuel_pos_baskets`: sessionized same-time card/station baskets with polymorphic line
  items.
- `fuel_station_profiles`: station profiles with traffic by day/hour and product/customer
  segment dynamic bags.
- `fuel_network_entities`: customer, card, station, chain, and product graph entities.

Example native query families:

- Find customers with POS events but missing `consumption_by_month.<month>`.
- Traverse product mix dynamic keys where price is high but amount is zero.
- Filter baskets whose `line_items` contain both fuel and service variants.

### card_games

Target collections:

- `card_print_dossiers`: card printing documents with identity, taxonomy, legalities,
  localization by language, ruling timeline, market link bags, and print relationships.
- `set_release_ecosystems`: set documents with translations, booster profiles, sheet
  dynamic objects, and card rollups.
- `card_catalog_entities`: polymorphic card, set, and translation entities for dispatch
  tasks.

Example native query families:

- Use `$objectToArray` on `legality_by_format` to find banned or restricted formats while
  checking missing localization keys.
- Traverse booster profile dynamic objects and nested sheet card-weight bags.
- Filter ruling timelines by year while preserving matched language and market subobjects.

### toxicology

Target collections:

- `molecule_graph_documents`: molecule documents with atom inventory, bond inventory,
  atoms with neighborhoods, bonds with endpoints, adjacency by atom, and element-pair
  matrix.
- `chemical_component_contexts`: polymorphic atom and bond context documents with
  neighborhood or endpoint summaries.

Example native query families:

- Filter atoms whose neighborhood has a single-bond chlorine-to-carbon pattern.
- Use `$getField` or `$objectToArray` on `element_pair_matrix.<pair>.<bond_type>`.
- Preserve graph slices by filtering `atoms[].neighborhood.neighbors[]`.

### codebase_community

Target collections:

- `community_threads`: question-centered thread documents with answers, comments,
  votes by type, revisions by GUID, links by type, and participants by user.
- `community_actor_profiles`: user documents with badge bags and thread participation.
- `community_knowledge_facets`: tag and post-link reference documents.

Example native query families:

- Compare accepted answer metrics with non-accepted answer metrics inside one thread.
- Traverse `answers[].revisions_by_guid` and detect field activity patterns.
- Use participant badge dynamic keys while preserving thread shape.

### european_football_2

Target collections:

- `match_documents`: match documents with competition context, scoreline, home/away
  lineups, player snapshots, bookmaker odds, and XML-derived event streams.
- `team_season_profiles`: team documents with `seasons.<season>` dynamic keys, match
  rollups, and tactical snapshots.
- `player_profiles`: player documents with dated attribute snapshots and match roles.

Example native query families:

- Traverse bookmaker odds by dynamic bookmaker key and compare with scoreline result.
- Filter lineups by player attribute snapshots.
- Parse and query XML-derived possession and card events.

### formula_1

Target collections:

- `race_weekends_v2`: race documents with calendar, circuit, qualifying entries, race
  entries, pit stops, lap index dynamic keys, fastest laps, and standings after the race.
- `driver_season_profiles`: driver-year documents with races and standings by round.
- `constructor_season_profiles`: constructor-year documents with paired driver results and
  constructor standings.

Example native query families:

- Query `lap_index.<lap>.position` with `$getField`.
- Filter race entries by pit stop count and points finish.
- Traverse `results_by_status` via `$objectToArray` to compare DNF buckets.

### california_schools

Target collections:

- `school_profiles`: school documents with campus addresses, administrators,
  classification code bags, operations flags, meal program year buckets, grade buckets,
  SAT records, and derived metrics.
- `district_school_rollups`: district/county documents with embedded school slices and
  metric rollups.
- `school_reference_facets`: code reference documents for classification and operation
  codes.

Example native query families:

- Convert `meal_programs.by_academic_year` to an array, filter `grade_buckets`, and
  compare SAT-derived rates.
- Rank charter schools with `$setWindowFields` over nested SAT metrics.
- Query missing and present administrator/contact fields.

### student_club

Target collections:

- `club_events`: event documents with attendance roster, member snapshots, budget category
  dynamic bags, budget lines, and nested expenses.
- `member_accounts`: member documents with profile, major and home facets, event
  participation, and `financial_ledger.by_month`.
- `club_finance_calendar`: month documents with income and expense source bags.

Example native query families:

- Filter attendance roster members by nested shirt size or major facets.
- Traverse `budget.by_category.<category>.lines[].expenses[]`.
- Compare income and expense dynamic keys by month.

### thrombosis_prediction

Target collections:

- `patient_clinical_profiles`: patient documents with demographics, care context,
  `clinical_timeline.by_year`, encounters, lab panels, measurement groups,
  measurements by code, examinations, and derived clinical flags.
- `clinical_measurement_index`: measurement-code documents with patient/date references
  and abnormal status rollups.
- `diagnosis_cohorts`: cohort documents keyed by diagnosis, admission state, sex, and year.

Example native query families:

- Traverse `clinical_timeline.by_year.<year>.encounters[].lab_panels[]`.
- Use `measurements_by_code.<code>.status` and sex-adjusted rules.
- Filter examinations by coagulation-test dynamic markers.

### superhero

Target collections:

- `hero_profiles`: hero documents with identity, appearance, nested color slots,
  attribute-by-name bags, power-by-name bags, power families, and derived query flags.
- `publisher_universes`: publisher documents with hero summaries, alignment breakdown,
  power index, and appearance rollups.
- `ability_catalog`: attribute and power catalog documents with holder arrays and
  publisher breakdown.

Example native query families:

- Query `abilities.powers.by_name.Super Strength.present`.
- Rank heroes within a publisher by nested height using `$setWindowFields`.
- Query appearance color slots and attribute bags without joining lookup tables.

## MQL Diversity Requirements

Distinct MQL strings are not enough. The release must report:

- Exact distinct MQL count.
- Distinct normalized semantic pipelines after removing limits, sorts, and nonsemantic
  projections.
- Distinct query blueprints per database.
- Distinct nested paths used by records.
- Distinct MongoDB native operators and operator combinations.
- Maximum family size after abstracting constants.
- Counts of records using each of `$objectToArray`, `$getField`, `$filter`, `$map`,
  `$reduce`, `$switch`, `$type`, `$setWindowFields`, and nested `$unwind`.

Release validation rejects synthetic diversity from:

- `native_slot_serial` or similar marker fields.
- Unique `$limit` values.
- Constant-true `$match` expressions.
- Sort-only variants.
- Final projections that remove fields requested by the natural language question.

## MongoDB Execution Requirements

Native release validation has two MongoDB modes:

1. Run-scoped mode for isolated construction and tests.
2. Exact-name release mode for handoff, where MongoDB databases are named exactly as the
   BIRD mini-dev `db_id` values.

Exact-name release validation must:

- Drop only the target database identifiers and explicitly requested stale `tend_` witness
  databases.
- Import `mongodb_data/<db_id>.json` into the exact `db_id` database.
- Verify collection counts against the artifact.
- Execute every gold MQL query from `test.json`.
- Record execution failures, empty results, result count summaries, and missing expected
  result fields.
- Fail the release if any gold MQL fails to parse or execute.

## Structure Audit Requirements

Each generated database must produce a structure audit report with:

- Collections and document counts.
- Maximum nested depth per collection.
- Paths matching `object -> dynamic key -> array -> object`.
- Paths matching `array -> object -> dynamic key or attribute bag`.
- Dynamic key domains and samples.
- Nested array length distributions.
- Polymorphic discriminator values and variant counts.
- Missing, null, empty, and present field counts for query-bearing paths.
- Source provenance coverage ratio.

The audit should be written as an artifact and summarized in logs. A database that does not
meet the complexity gate is not eligible for Phase B records.

## Implementation Strategy

The implementation should be incremental but aligned with the final goal:

1. Add a high-nesting structure audit module and run it on the current native dataset to
   make failures explicit.
2. Add reusable deep materialization primitives.
3. Implement three exemplar databases first: `formula_1`, `card_games`, and `toxicology`.
   They provide regular nested sessions, parsed document bags, and graph structures.
4. Implement the remaining eight database-specific builders.
5. Replace generic Phase B compilers with query-blueprint compilers.
6. Add exact-name MongoDB import and execution validation.
7. Regenerate the full release and verify structure, MQL diversity, and execution.

The final release is not complete until all eleven databases pass the structure gate and
all generated MQL records execute successfully in MongoDB.
