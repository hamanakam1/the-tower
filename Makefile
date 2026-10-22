.PHONY: install test serve demo clean

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

serve:
	python -m the_tower serve --config config.example.yaml

demo:
	python -m the_tower demo

clean:
	rm -rf .pytest_cache __pycache__ the_tower.egg-info build dist