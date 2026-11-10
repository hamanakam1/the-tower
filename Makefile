.PHONY: install test serve demo verify policy screenshots clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

serve:
	python -m the_tower serve --config config.example.yaml --policy policy.example.yaml

demo:
	python -m the_tower demo

# Check that the Tower is trustworthy: audit chain, schema, policy, freshness.
verify:
	python -m the_tower verify --config config.example.yaml --policy policy.example.yaml

# Show the admission rules in force.
policy:
	python -m the_tower policy --policy policy.example.yaml

# Regenerate the README's dashboard screenshots.
screenshots:
	python scripts/capture_screenshots.py

clean:
	rm -rf .pytest_cache __pycache__ the_tower.egg-info build dist
