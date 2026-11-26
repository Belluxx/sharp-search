IMAGE := sharp-search-mcp
PYTHON := /usr/local/searxng/.venv/bin/python

.PHONY: build test check bots

build:
	docker build -t $(IMAGE) .

# Offline tests, inside the image with networking disabled.
test: build
	docker run --rm --network none -v "$(CURDIR)/tests/test.py:/app/test.py:ro" -w /app --entrypoint $(PYTHON) $(IMAGE) -m unittest -v test

# End-to-end tests of both transports against the live web.
check: build
	./tests/check.sh $(IMAGE)

# Fetches 50 pages behind bot detection on the live web, and prints the failures and a score.
bots: build
	docker run --rm -v "$(CURDIR)/tests/bots.py:/app/bots.py:ro" -w /app --entrypoint $(PYTHON) $(IMAGE) bots.py
