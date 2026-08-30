# RoadSeva — Vini Implementation Physical Delivery (v2)

Same file set and content as roadseva_vini_delivery.tar.gz
(SHA-256 57ec0fcffebce86158101d82a5e8e73cccb6f39e6237a0380eabf91320ce5d9e),
regenerated fresh for this delivery request. No source changes occurred
between the two — confirmed by identical `git diff --numstat` output.

## Base state

- Base commit: 84a111eee3b57c60a6e6cb0c683c634a68accf9b
- Branch: main
- Current HEAD: 84a111eee3b57c60a6e6cb0c683c634a68accf9b (unchanged)
- Working-tree status: dirty, 6 modified tracked files, 13 untracked files

## 1. Modified tracked files (6)

| File | +lines | -lines | What changed | Phase |
|---|---|---|---|---|
| database.py | 434 | 173 | Bootstrap credential removal; centralized authz integration; viewer policy; team reset/toggle authorization; training_labels + ai_inference_runs schema; durable training storage; inference provenance logging; human-severity linkage in ai_corrections; PostgreSQL transaction-safety rollback fix | Security + Priority 4 |
| main.py | 203 | 29 | OpenAPI production gating; 12 routes gained authz object-scope checks; complaint-story IDOR fix; team reset/toggle authorization + credential-card password delivery; verify-inspection human-severity capture | Security + Priority 4 |
| severity.py | 56 | 2 | analyse_severity() gained optional report_id/source_photo_reference params and inference-attempt logging at every return path; Groq call/prompt/parsing unchanged | Priority 4 |
| templates/field.html | 11 | 0 | Required human_severity select field added to the live verification form | Priority 4 |
| templates/track.html | 5 | 5 | GPS, photo, and assigned-staff-name gated behind is_staff | Security |
| tests/test_routes.py | 18 | 3 | AE fixture division corrected to a real ward-containing division; rate-limiter reset fixture added | Security (test support) |

## 2. New files (13)

| File | Size | Purpose | Phase | Required for app |
|---|---|---|---|---|
| authz.py | 11,791 B | Centralized object/geographic/workflow authorization layer | Security | **Yes** |
| tests/test_authz.py | 20,189 B | Authorization layer test coverage | Security | No |
| tests/test_bootstrap.py | 6,506 B | Bootstrap credential removal test coverage | Security | No |
| tests/test_complaint_story.py | 12,543 B | Complaint-story IDOR fix test coverage | Security | No |
| tests/test_human_severity.py | 19,785 B | Independent human severity verification test coverage | Priority 4 | No |
| tests/test_inference_provenance.py | 11,355 B | ai_inference_runs test coverage | Priority 4 | No |
| tests/test_openapi_exposure.py | 3,956 B | OpenAPI production gating test coverage | Security | No |
| tests/test_password_in_url.py | 11,788 B | Password-in-URL fix test coverage | Security | No |
| tests/test_safe_add_columns.py | 4,233 B | PostgreSQL transaction-safety fix test coverage | PostgreSQL fix | No |
| tests/test_staff_management.py | 15,042 B | Team reset/toggle authorization test coverage | Security | No |
| tests/test_track_privacy.py | 8,898 B | /track privacy fix test coverage | Security | No |
| tests/test_training_storage.py | 8,650 B | Durable training-label storage test coverage | Priority 4 | No |
| tests/test_viewer_policy.py | 8,018 B | Viewer city-wide-read policy test coverage | Security | No |

Only `authz.py` is required for the running application; the other 12 are test-only.

## PostgreSQL verification status

Live-verified against a real, locally-installed PostgreSQL 16.15 server using
the actual `psycopg` v3.2.13 driver pinned in requirements.txt. The
`_safe_add_columns()` rollback fix was confirmed to prevent the real
`InFailedSqlTransaction` cascade (reproduced via a negative control running
the pre-fix pattern against the same live server). No source file was
touched during that verification; the test database/role were created and
dropped via direct psql administration.

## Recovery instructions

```bash
git clone https://github.com/Vineeth2002/roadseva.git
cd roadseva
git checkout 84a111eee3b57c60a6e6cb0c683c634a68accf9b

cp -r /path/to/roadseva_vini_delivery_v2/* .

python3 -m pytest tests/ -q
# Expected: 366 passed, 1 skipped
```
