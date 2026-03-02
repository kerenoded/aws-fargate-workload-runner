# Contributing

Thank you for your interest in contributing to `aws-fargate-workload-runner`.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
```

## Adding a New Scenario

1. Create `src/awfr/scenarios/<your_scenario>.py`.
2. Implement a module-level entrypoint with this exact signature:
   ```python
   def run(run_env: RunEnv, metrics_writer: MetricsWriter) -> dict:
       ...
   ```
   The return value is a plain `dict` that is merged into `summary.json` — include any
   scenario-level counters you want surfaced (e.g. `{"sends_succeeded": 1000, "sends_failed": 2}`).
3. Register the scenario in `src/awfr/scenarios/registry.py`:
   ```python
   from awfr.scenarios import your_scenario

   SCENARIOS = {
       ...
       "your_scenario": your_scenario.run,
   }
   ```
4. Add a sample config JSON under `loadtest/configs/`.
5. Update `README.md` with the config schema.

## Code Style

This project uses `ruff` for linting and formatting (configured in `pyproject.toml`).

```bash
ruff check src/
ruff format src/
```

## Pull Request Guidelines

- Keep PRs focused on a single change.
- Include a brief description of what and why.
- Update `README.md` if you change CLI flags or outputs.
- Do not commit real AWS account IDs, ARNs, or credentials.

## Reporting Issues

Open a GitHub Issue with:
- What you were trying to do
- The command you ran
- The error message or unexpected output
