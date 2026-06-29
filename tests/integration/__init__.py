"""Integration tests for Rewind.

These tests exercise the full stack (CLI → server → SQLite → reload) and are
marked with ``@pytest.mark.integration``. They are deselected by default in
fast test runs; explicitly invoke with:

    pytest -m integration
"""
