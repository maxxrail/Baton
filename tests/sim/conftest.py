"""Fixtures for simulation tests: the compiled AOPs and the tool registry."""

import pytest

from baton.aop.compiler import compile_aops
from baton.aop.models import AOPBundle
from baton.tools.registry import ToolRegistry, default_registry
from tests.runtime.support import REPO


@pytest.fixture(scope="session")
def bundle() -> AOPBundle:
    return compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")


@pytest.fixture(scope="session")
def registry() -> ToolRegistry:
    return default_registry()
