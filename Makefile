.PHONY: build

PYTHON := .venv/bin/python

build:
	$(PYTHON) -m build --wheel
