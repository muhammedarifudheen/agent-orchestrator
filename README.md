# Agent orchestration system

Industry-agnostic orchestrator: a goal in natural language, tools from MCP servers, one error contract, and a human handoff with
full context when automation cannot finish. Architecture, rules and every recorded decision are in [CLAUDE.md](CLAUDE.md).

## Running the tests

Default pytest treats budget overruns as warnings and logs them (`.perf/overruns.jsonl`).
CI uses `pytest --strict-budgets` and fails on them.
If you see a warning, re-run on a quiet host; if it persists, it's a real regression.
