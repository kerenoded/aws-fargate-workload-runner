# Contributing

Thank you for your interest in contributing to `aws-fargate-workload-runner`.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Adding a New Scenario

1. Create `src/awfr/scenarios/<your_scenario>.py`.
2. Implement a `run(cfg: dict, metrics: MetricsWriter, stop_event: threading.Event) -> None` function.
3. Register the scenario in `src/awfr/scenarios/__init__.py`.
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
