# Engineering decisions

## Evidence before automation

Original bytes are retained and hashed. Extracted text and candidates are stored separately from confirmed contract fields. An OCR result can guide review without becoming an authoritative amount or company assignment. This preserves the distinction between observation and decision.

## Relational storage

The running web interface primarily uses the `documents`, `companies`, `projects`, `finance_records`, `payment_confirmations`, users and audit tables in `db.py`. `schema.py` and `migrations.py` additionally provide an extended relational model and migration commands. Their presence does not mean that all extended-model workflows are integrated into the web application. Migration and rollback commands are not part of the beginner demo.

SQLite simplifies local deployment. Transactions, foreign keys and revision checks support integrity; SQLite remains a single-host database and its file should not be shared directly between machines. A future multi-host version would need a different deployment design and measured concurrency requirements.

## Financial semantics

- Amounts use integer minor units to avoid floating-point arithmetic errors.
- Missing amounts remain unknown rather than becoming zero.
- Receivable and payable amounts are distinct.
- Currency groups stay separate; there is no automatic exchange-rate conversion.
- Overpayments are shown as exceptions instead of cancelling unrelated outstanding balances.
- Corrections void the original entry and preserve a reason.
- A confirmation is a snapshot; later changes make it stale.
- Outstanding balances are not automatically overdue: that would require due dates and payment schedules.

## Security and reliability boundaries

The implementation includes password hashing, role checks, CSRF tokens, response headers, upload controls, file hashes and backup restoration checks. These controls are not evidence of a comprehensive security audit. The demo remains on localhost. Operational deployment also needs transport protection, access management, backup retention and recovery drills appropriate to its environment.

## Testing strategy and next work

Existing tests exercise real Flask routes, SQLite records, generated documents and exported workbooks. They cover rejected updates and preservation of source files as well as successful paths. A scanned PDF integration test runs the actual OCR pipeline.

Priorities for further work: tests for extended-model migrations and rollback; an English interface; a labelled synthetic OCR evaluation set reporting extraction accuracy and review workload; measured load tests; and a separate production deployment assessment. These are proposed work, not completed results.
