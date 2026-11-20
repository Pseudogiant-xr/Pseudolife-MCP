"""Map the committed Console build to the frozen Python daemon's static directory."""

from setuptools import find_namespace_packages, setup

CONSOLE_PACKAGE = "pseudolife_memory.web.static"

setup(
    packages=[*find_namespace_packages(include=["pseudolife_memory*"]), CONSOLE_PACKAGE],
    package_dir={CONSOLE_PACKAGE: "frontend/dist"},
)
