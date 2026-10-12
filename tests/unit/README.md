# FastDyn Unit Tests

## Framework
- **Test runner**: [pytest](https://docs.pytest.org/)
- **Mocking**: `unittest.mock` from the Python standard library

## Directory Layout

```
tests/unit/
  __init__.py
  test_config_paths.py      # Tests for files named in a configuration
  test_runtime_config.py    # Tests for FMU/Rumoca/helper process TOML parsing
  test_swarm.py             # Tests for per-worker swarm port and work-dir planning
```

## How to Run

Run all unit tests:
```bash
PYTHONPATH=src pytest tests/unit/ -v
```

Run a specific test file:
```bash
PYTHONPATH=src pytest tests/unit/test_config_paths.py -v
```

Run a specific test function:
```bash
PYTHONPATH=src pytest tests/unit/test_config_paths.py::test_absolute_paths_are_kept -v
```

## Conventions

### Naming
- Test files: `test_<module_name>.py`
- Test functions: `test_<descriptive_name>` (use underscores, be specific)
- Test classes (optional): `TestClassName` when grouping related tests

### Fixtures
- Shared fixtures live in a `conftest.py`
- Module-specific fixtures can live at the top of the test file
- Use `@pytest.fixture` decorator, not manual setup/teardown

### Mocking
- **Never make real API calls** in unit tests
- Use `unittest.mock.patch` or `unittest.mock.MagicMock` for external dependencies
- Mock at the boundary (e.g., mock the QEMU process, not internal functions)

### Assertions
- Use plain `assert` statements (pytest rewrites them for clear failure messages)
- Use `pytest.raises(ExceptionType)` for expected exceptions

### Test Data
- Inline small test data directly in the test function
- For larger fixtures (e.g., sample configurations), use multi-line strings or
  files in a `testdata/` subdirectory

## Adding New Tests

1. Create a new file `test_<module>.py` in this directory
2. Import the module under test
3. Write test functions prefixed with `test_`
4. Run `PYTHONPATH=src pytest tests/unit/ -v` to verify
