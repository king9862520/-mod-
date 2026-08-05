# Contributing

1. Fork the repository and create a focused branch.
2. Install development dependencies with `python -m pip install -e ".[dev]"`.
3. Run `python -m ruff check src tests` and `python -m pytest` before opening a pull request.
4. Keep Steam integration changes isolated from the GUI process. The main process must never initialize App ID `457140`.
5. Do not commit local databases, settings, downloaded Workshop files, or user Mod configurations.
