# Changelog

Entries here are the audit-trail record of things a commit message does not say. History is never rewritten (a commit may be cited
elsewhere); a mislabelled commit is recorded here instead, and `tests/test_commit_hygiene.py` (CLAUDE.md D30) excuses a past mislabel only
when a line below starts with its hash and says it contained code changes behind a docs-only message.

- efdb147 contained code changes (harness provider gate; provider_model field) behind a docs-only message; recorded here for the audit trail. Code files touched: `agent/provider_config.py` (new `credentials_present`), `scripts/live_judgment.py` (provider-aware key gate `_Ctx.key`/`client_key`, `provider_model` on every record, free-provider PASS label, `PROVIDER_API_KEY` redaction), `tests/scenarios/test_planner_contract.py` and `tests/ingest/test_doc_contract.py` (the live gates now use `credentials_present`), `tests/test_live_attest_and_mock.py` and `tests/scenarios/test_provider_config.py` (tests for the above); documentation: `AUDIT.md`, `CLAUDE.md`, `docs/ONBOARDING.md`.
