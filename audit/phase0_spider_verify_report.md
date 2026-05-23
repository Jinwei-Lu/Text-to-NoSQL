# Phase 0 Spider Verification Report

**Task**: `phase0-spider-verify`  
**Date**: 2026-05-23  
**Verdict**: **MISSING — block**

## Search Performed

| Location | Result |
|----------|--------|
| `/Users/jwlu18/Developer/TEND/**/orchestra.sqlite` | Not found |
| `/Users/jwlu18/**/orchestra.sqlite` (30s find) | Not found |
| `/Users/jwlu18/Downloads/*spider*` | Not found |

## Expected Anchor

- **DB id**: `orchestra`
- **File**: `orchestra.sqlite` (Spider 1.0)
- **Required tables**: `{conductor, orchestra, performance, show}`

## Remediation

Obtain Spider 1.0 dataset (Yale Spider benchmark) and place `orchestra.sqlite` on disk before running Gate 2 pilot or Gate 4 NormExec regression against live data.

## Proceeding Note

Documentation, schemas, fixtures, and agent prompts in this revision use **documented Spider orchestra schema** and **v2-original canonical anchor `orchestra/1001`** as design references. Runtime verification against live SQLite is deferred until Spider data is available locally.

## Public Schema Reference (Spider 1.0 orchestra)

| Table | Key columns |
|-------|-------------|
| `conductor` | Conductor_ID, Name, Age, Nationality, Year_of_Work |
| `orchestra` | Orchestra_ID, Orchestra, Conductor_ID, Record_Company, Year_of_Founded, Major_Record_Format |
| `performance` | Performance_ID, Orchestra_ID, Type, Date, Official_ratings_(millions), Weekly_rank, Share |
| `show` | Show_ID, Performance_ID, Result, Attendance |
