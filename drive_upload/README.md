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

Uploaded files:

| File | Drive link | SHA256 |
| --- | --- | --- |
| `TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst` | https://drive.google.com/file/d/1O9ctyY6mUuKBF6OMCqk_UXyGdNkM8MRE/view?usp=drivesdk | `3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594` |
| `TEND_native_variant_11db_110distinct_final3_paper_2026-06-03.tar.zst` | https://drive.google.com/file/d/1uEE_rVYBE1rNlKgCFOlHjI3omchdE_SA/view?usp=drivesdk | `05173b579fabdb7ff89828378e4293455cba063e776acef33cfa0e3753dfc80c` |

The full package preserves the source-build path. For the formal release, use
`release/tend-native-mongodb-v1/MONGODB_DATA.md` to extract only
`mongodb_data/*.json` into the release directory.

The raw per-database MongoDB JSON export checksums are in
`raw_mongodb_data_checksums_and_sizes.txt`.
