# Google Drive Upload Manifest

The binary packages in this directory are local staging artifacts for Google
Drive. The large `.tar.zst` files are intentionally ignored by Git; this folder
tracks only the manifest and checksums needed for paper collaborators.

The formal GitHub release package is:

```text
release/tend-native-mongodb-v1/
```

Google Drive folder:

https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

Current lean public package staged locally:

| File | Upload status | SHA256 | Size bytes |
| --- | --- | --- | ---: |
| `TEND_native_mongodb_v1_lean_public_2026-06-05.tar.zst` | pending Drive upload | `062dffc7abf2ce5cd798f395f09ab8ac3334e100545ae2a381821bd064445b57` | 317355 |

Existing Drive files visible in the target folder:

| File | Drive link | Status | SHA256 |
| --- | --- | --- | --- |
| `TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst` | https://drive.google.com/file/d/1O9ctyY6mUuKBF6OMCqk_UXyGdNkM8MRE/view?usp=drivesdk | raw witness package still valid | `3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594` |
| `TEND_native_variant_11db_110distinct_final3_paper_2026-06-03.tar.zst` | https://drive.google.com/file/d/1uEE_rVYBE1rNlKgCFOlHjI3omchdE_SA/view?usp=drivesdk | superseded by the lean public package | `05173b579fabdb7ff89828378e4293455cba063e776acef33cfa0e3753dfc80c` |

The current Codex Google Drive connector can list the folder but does not expose
an arbitrary raw-file upload action, so the lean package remains staged locally
until uploaded through Drive UI/API/CLI.

The raw per-database MongoDB JSON export checksums are in
`raw_mongodb_data_checksums_and_sizes.txt`.
