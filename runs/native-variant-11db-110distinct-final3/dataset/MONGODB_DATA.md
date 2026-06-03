# MongoDB Witness Data

Run id: `native-variant-11db-110distinct-final3`

The raw MongoDB witness exports are intentionally not tracked in GitHub. They
are large JSON exports, totaling about 4.9 GiB locally before compression. The
GitHub repository keeps the benchmark records, schemas, provenance, manifests,
paper statistics, and audit evidence; the raw `mongodb_data/` directory should
be restored from the Google Drive package.

Google Drive folder:
https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

Expected full package:

```text
TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst
```

Package SHA256:

```text
3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594
```

Restore from the repository root:

```bash
tar -I zstd -xf TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst
test -d runs/native-variant-11db-110distinct-final3/dataset/mongodb_data
```

The restored directory should contain one JSON file per database:

| Database | File | Size bytes | SHA256 |
| --- | --- | ---: | --- |
| `california_schools` | `california_schools.json` | 133760965 | `1f525eb35024fc2e92eca945091445a27aacd9fe888724670f8a56706c714827` |
| `card_games` | `card_games.json` | 708311206 | `ac62e4ac0d5d62a7be56d48253b99de0e4b53a3549d54a816fc947d7546b36f9` |
| `codebase_community` | `codebase_community.json` | 328078849 | `23a09edbb50c58bee4eb89a845cf0c35a2c41b3f612ea3e588875181e199e7ae` |
| `debit_card_specializing` | `debit_card_specializing.json` | 370391889 | `ef9bd59e3df0e2576e0e4ce66cb77cd7bda26d24830bcbd3288ccb6fbf8486b8` |
| `european_football_2` | `european_football_2.json` | 2378475452 | `004bbb56c130790e1053e88fde79ba114327a853c3d219132d0a6d4def2b38a2` |
| `financial` | `financial.json` | 575975761 | `936e5c0a4264bdd3638eee6c134dfc2f304140ac29b70e3111f52b5a5f8096f8` |
| `formula_1` | `formula_1.json` | 616698385 | `7530db84fa375e460f0d87762a6c04f7ef469c23c56f168dfc8a10a612acd3a0` |
| `student_club` | `student_club.json` | 1211022 | `64c33f4bdc7ad7f291eddd76360df78f07d55cf9f08950ab2c623d4fcd7b9c04` |
| `superhero` | `superhero.json` | 16026379 | `6b09ccc2d90db17a62b1af4a598fa13eb24460601b262d967e3808f4fb7dcc56` |
| `thrombosis_prediction` | `thrombosis_prediction.json` | 130655705 | `52701dce10ab9bf410ae6545b149dd0e1067d2633284b2a8a249c8f41bd99e2a` |
| `toxicology` | `toxicology.json` | 46206778 | `3ae8903b7528a5a0cca19ce6c42206136c626f2fd6127ee7da7a2f7d086c2697` |

Related tracked verification evidence:

- `audits/paper_statistics/fresh_exact_execution_by_db_verification.json`
- `audits/paper_statistics/release_validator_snapshot.json`
- `audits/paper_statistics/pipeline_stage_detailed_statistics.json`
- `audits/surgery/post_surgery_exact_execution.json`
