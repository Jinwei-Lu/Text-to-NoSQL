# Surgical NL-MQL Patch Report

- Mode: existing final3 artifact patched in place; no construct generation.
- Changed records: `220`
- Per DB changed counts:
  - `california_schools`: `20`
  - `card_games`: `20`
  - `codebase_community`: `20`
  - `debit_card_specializing`: `20`
  - `european_football_2`: `20`
  - `financial`: `20`
  - `formula_1`: `20`
  - `student_club`: `20`
  - `superhero`: `20`
  - `thrombosis_prediction`: `20`
  - `toxicology`: `20`

## Templates
### `california_schools`
- `california.district_equity_readiness_gap`: Uses grade-span metrics, FRPM burden, SAT readiness, and active school counts.
- `california.county_d_vs_s_readiness_gap`: Unwinds D/S record-type maps and regroups by district to compute readiness gaps.
- `california.charter_virtual_frpm_sat`: Combines program tags, academic-year FRPM panels, grade spans, and readiness summaries.
- `california.district_active_charter_spans`: Unwinds grade-span school arrays and aggregates program tags plus FRPM values.
### `card_games`
- `card.legality_language`: Combines legality and localization dynamic maps.
- `card.set_rarity_translation_balance`: Uses set-level rarity maps, translation maps, release metadata, and total-card rollups.
- `card.view_consistency_legal_translation`: Replaces binary presence checks with materialized view consistency across format and language maps.
### `codebase_community`
- `code.thread_tags_votes`: Crosses tag and vote dynamic maps with lifecycle state.
- `code.answer_votes_comments`: Uses answers arrays plus answer-level vote and comment dynamic objects.
- `code.tag_status_year_mix`: Traverses status-to-year dynamic objects and nested thread arrays.
### `debit_card_specializing`
- `debit.customer_events_segment_gross`: Uses transaction event arrays rather than only monthly consumption keys.
- `debit.station_product_customer_cross`: Crosses two dynamic objects in the station catalog: product mix and customer segments.
- `debit.monthly_consumption_volatility`: Aggregates across monthly dynamic keys rather than testing one month key.
### `european_football_2`
- `football.betting_market_result`: Uses bookmaker dynamic maps, odds, league-season context, and final result semantics.
- `football.lineup_rating_year`: Combines lineup player arrays with rating-by-season dynamic maps.
- `football.team_home_away_split`: Uses season dynamic keys and nested fixtures with side/result semantics.
### `financial`
- `financial.activity_orders`: Aligns activity_by_month and standing_orders_by_symbol maps.
- `financial.loan_schedule`: Uses loan schedule dynamic months with district-market context.
- `financial.district_frequency_gender_loan_mix`: Combines account-frequency buckets with gender client pools and district context.
- `financial.party_role_card_loan_mix`: Uses role dynamic objects, member arrays, card arrays, and loan context.
### `formula_1`
- `f1.race_entries_status`: Uses race entry arrays, constructor identity, finish status, grid, and points.
- `f1.actor_career`: Turns polymorphic actor records into career-year aggregates instead of field-name inspection.
- `f1.pit_stop_points_burden`: Combines race entries, pit-stop arrays, finish order, and points.
### `student_club`
- `student.event_budget_burn_roles`: Combines budget categories with role participation instead of checking budget keys alone.
- `student.member_expense_gap`: Crosses member participation, finance summary, and expense category dynamic objects.
- `student.officer_budget_attendee`: Walks event attendees, embedded accounts, roles, and attendee-level finance maps.
- `student.guest_speaker_member_budget_mix`: Crosses participation events, event type, and budget-by-category dynamic records.
### `superhero`
- `hero.attribute_power_cross`: Uses attribute dynamic maps, observations, and power-family maps.
- `hero.publisher_alignment_power`: Crosses alignment and power-family dynamic maps at publisher level.
- `hero.power_alignment_completeness`: Uses ability catalog alignment maps and nested hero membership arrays.
### `thrombosis_prediction`
- `thrombosis.events_evidence`: Uses event arrays and evidence_by_code dynamic maps.
- `thrombosis.measure_years`: Uses yearly measurement dynamic maps and reading arrays.
- `thrombosis.diagnosis_risk_mix`: Uses risk-group dynamic maps and nested patient arrays.
### `toxicology`
- `tox.halogen_carbon_carcinogenic`: Uses element dynamic keys, atom arrays, neighbor arrays, and assay labels as a real chemistry graph query.
- `tox.bond_type_by_label`: Combines assay view arrays, bonds_by_type dynamic objects, and outcome labels.
- `tox.branching_carbons`: Uses atom topology with $map, $setUnion, neighbor arrays, and assay context.
- `tox.n_o_double_bonds`: Combines element-set membership with bond-type dynamic objects rather than testing a single key.
