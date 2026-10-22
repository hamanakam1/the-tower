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

## Areas to contribute
- New node types / telemetry sources
- Improved anomaly detection models
- Dashboard UX improvements
- Additional storage backends
- Integration with El Destilador / EdgeRecord