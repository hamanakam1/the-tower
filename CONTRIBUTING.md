# Contributing to The Tower

## Setup
```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Guidelines
- Keep changes focused and add tests.
- Run the full test suite before committing.
- Use frequent, small commits with clear messages.
- Follow existing code style (ruff-friendly, type hints).
- Read [`docs/roadmap.md`](docs/roadmap.md) before starting anything structural. Decisions that contradict the plan need an ADR in `docs/adr/`, not a quiet divergence.

## Areas to contribute
- New node types / telemetry sources
- Improved anomaly detection models
- Dashboard UX improvements
- Additional storage backends
- Integration with El Destilador / EdgeRecord
- Sensor backends for platforms beyond macOS and generic Linux
- Policy rule packs for a new node class