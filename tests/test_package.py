"""Smoke test so the factory's checks have something to run from day one."""

import pensieve_traces


def test_package_imports_and_has_a_version():
    assert isinstance(pensieve_traces.__version__, str)
