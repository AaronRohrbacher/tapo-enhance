# tapo — common dev tasks. Use `.venv/bin/python` everywhere so we stay
# inside the virtualenv that ships pytapo / uvicorn / playwright.

PY      := .venv/bin/python
PYTEST  := .venv/bin/pytest

.PHONY: run test test-browser test-all check fmt clean

run:
	$(PY) -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# Default suite — all unit + integration tests EXCEPT the playwright/browser
# tests, which run in their own session because sync_playwright collides
# with pytest-asyncio's per-test event loop.
test:
	$(PYTEST) tests/

test-browser:
	$(PYTEST) tests/test_browser.py -m browser

# Everything, in two passes — what CI should run.
test-all: test test-browser

clean:
	rm -rf cache/stream cache/thumbs cache/previews
	rm -rf __pycache__ srv/__pycache__ tests/__pycache__ .pytest_cache

# Fast sanity check before committing.
check: test test-browser
	@echo
	@echo "all green."
