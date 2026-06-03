# Raw MongoDB Data

The raw JSON exports are intentionally distributed through the Google Drive full
package and are not tracked in Git.

In this workspace, the full local release contains 11 raw JSON exports in this
directory. They are hard-linked from the final build artifacts to avoid
duplicating several GiB on disk.

GitHub should track this README only. The raw files are ignored by `.gitignore`.

Expected files:

```text
california_schools.json
card_games.json
codebase_community.json
debit_card_specializing.json
european_football_2.json
financial.json
formula_1.json
student_club.json
superhero.json
thrombosis_prediction.json
toxicology.json
```

The expected per-database files, byte sizes, and SHA256 values are listed in:

```text
../external/raw_mongodb_data_checksums_and_sizes.txt
```

See also `../MONGODB_DATA.md`.
