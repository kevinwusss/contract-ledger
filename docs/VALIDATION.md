# Validation evidence

Date: 2026-09-28. Platform: Windows, Python 3.12.14.

## Local automated run

From the portfolio repository root, using the existing project's Python environment:

```text
python -X utf8 -m pytest -q
27 passed in 13.98s
```

This run covers the copied application and includes the new synthetic demo test. The latter checks three imported documents, an unconfirmed record with unknown company and amount, four financial entries, the CNY confirmation balances and refusal to overwrite existing data. Existing tests cover the actual OCR pipeline, permissions, CSRF, conflicting edits, currency separation, financial corrections, exports, and backup restoration.

A fresh `uv sync --locked --extra test` installation was also attempted with an explicit local Python 3.12 interpreter. Lock resolution succeeded, but large dependency downloads did not finish during the preparation session and were interrupted. A complete clean-environment installation is therefore unverified; the passing suite above used the pre-existing environment. Rerun installation before using the new copy's environment.

An initial packaging test identified missing Windows service scripts. Both scripts were included before the successful run. The restart test mocks process creation; it does not prove an actual Windows service restart.

## Publication boundary

The source package was assembled with a file allowlist. Operational documents, database files, credentials, backups, previous outputs and company-specific import scripts were not copied. A real company name in a migration comment was replaced by a fictional example. Application source comparison showed only that comment change. Service script defaults were separately changed to localhost.

Git ignore checks cover the data directory, virtual environment, environment files, database extensions, keys and common document/export formats. A targeted text scan found no matches for the original local username, the removed company name or common API-token prefixes in the publication source and documentation. This is a scoped publication check, not a guarantee that arbitrary later additions contain no sensitive information.

## Not established by these results

- A successful GitHub Actions run; the workflow must execute after upload.
- Browser screenshot or visual regression validation in this preparation session.
- OCR accuracy on a representative labelled corpus, or production load capacity.
- Full security review, extended-model migration coverage, or public-hosting readiness.

No production data was used by the tests. No remote repository was created or pushed during preparation.
