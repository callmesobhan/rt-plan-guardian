.PHONY: install test lint type fmt docs demo app check-all clean

install:            ## editable install with dev + app extras
	pip install -e ".[dev,app]"

test:               ## run the test-suite
	pytest -q

lint:               ## static checks (no changes made)
	ruff check .
	ruff format --check .

type:               ## type-check the package
	mypy

fmt:                ## auto-format and auto-fix lint
	ruff format .
	ruff check --fix .

docs:               ## regenerate docs/rule-catalog.md and configs/template_all_options.yaml from the registry
	rtpg rules --format md > docs/rule-catalog.md
	rtpg rules --format yaml > configs/template_all_options.yaml

check-all: lint type test   ## everything CI runs

demo:               ## synthetic case + report in ./demo_out (git-ignored)
	rtpg demo demo_out/case
	rtpg check demo_out/case --out demo_out/report

app:                ## launch the Streamlit UI
	rtpg app

clean:
	rm -rf demo_out reports .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist
