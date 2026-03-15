## Python tooling

Always use `uv` for all Python operations. The Python project lives in `installer/`.

- Use `uv run python -m pytest` (from `installer/`) instead of `pytest` or `python -m pytest`
- Use `uv run python` instead of `python` or `python3`
- Use `uv pip install` instead of `pip install` or `pip3 install`
- Use `uv sync` to install dependencies from pyproject.toml
- Never use bare `pip`, `pip3`, `python -m pip`, or `python -m pytest`
