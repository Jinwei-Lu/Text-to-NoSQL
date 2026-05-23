# TEND Proposals Changelog

## v2-Agent (2026-05) — Active

**Paradigm shift**: Spider-anchored LLM Agent framework replaces rule-based synthesis.

### Added
- 7 specialized agents: WP, SRA, SC, DM, QRA, NNC, RA
- Spider 1.0 as sole schema/data/workload foundation (200 DBs)
- Layered documentation: Part I (research) + Part II (implementation appendix) per volume
- `proposals/agent_prompts/` — 7 agent prompt files with 4-piece structure
- `proposals/schemas/` — machine-readable JSON Schemas
- `proposals/fixtures/` — 5 Spider DB end-to-end fixtures
- `proposals/_meta/{GLOSSARY,CANONICAL_ANCHOR,CHANGELOG}.md`
- Workload-driven MongoDB design (11 official patterns + 3 anti-patterns)

### Removed (from v2-original)
| Concept | v2-Agent disposition |
|---------|---------------------|
| 105 handwritten domain template YAML | Deleted → Spider DBs |
| Phenomena Planter 15-class active injection | Deleted → natural emergence + audit detectors |
| 6×36 noise taxonomy | Reduced → ≤8 optional stress tests |
| Intent Template Lattice 60+200 | Deleted → QRA direct pattern selection |
| SI DSL + ≡_SI 7 conditions | Deleted → QRA internal representation |
| Symbolic Lift → QIR | Deleted → compiler unit tests |
| V_correct neighborhood mining ≥5 LLM | Deleted |
| 4-panel amplify ≤3 rounds | Deleted → 4-panel observation only |
| 9+1 axis min/max dual quota | Reduced → 5-axis single max quota |
| 5 specificity NLQ levels | Reduced → 2 (canonical + colloquial) |
| 35 mutations × 4 dims | Reduced → 5–8 per record |
| failure-mode bank ≥30/family | Reduced → ≥10/family |

### Retained (from v2-original)
- gold-as-class / canonical_form_set 4-tuple
- EX dual-condition (AST_check + NormExec ≡_rec)
- dual-bridge defeat
- L4 ≥ 15% hard constraint
- cross-domain holdout
- Norm 4-layer normalization
- ≡_rec recursive equality
- 6 disabled operators
- SMART 4-stage solver architecture
- 7 metrics + 4-panel report (without amplify feedback)

### Volume renames
| Old | New |
|-----|-----|
| `03_dataworld_synthesis.md` | `03_spider_anchored_dataworld.md` |
| `04_intent_to_query_construction.md` | `04_agent_framework.md` |

---

## v2-original (archived)

Location: `proposals/archive/v2-original/`

Rule-based DataWorld synthesis with Domain Template Bank, Schema Composer, Witness Data Generator, Phenomena Planter, SI DSL, Intent Template Lattice, and 4-phase adversarial validation (V_correct / V_discrim / V_diverse).

---

## v1 (TEND.json)

Mechanical Spider→MongoDB graph-algorithm conversion. Preserved in `artifacts/TEND/full/TEND.json`. Known limitations: loss of schema-less MongoDB characteristics, non-native document design.
