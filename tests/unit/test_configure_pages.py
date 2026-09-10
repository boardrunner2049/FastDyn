import importlib.util
from pathlib import Path
import tomllib

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_pages", Path(__file__).resolve().parents[2] / "utils/configure_pages.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('repository,expected', [
    ('jgoppert/FastDyn', '/FastDyn/'),
    ('PSecLab/FastDyn', '/FastDyn/'),
    ('Example/Renamed', '/Renamed/'),
    ('Example/example.github.io', '/'),
])
def test_repository_and_pages_prefix_follow_the_checkout(tmp_path, repository, expected):
    path = tmp_path / 'book.toml'
    path.write_text('[output.html]\nadditional-css = ["theme.css"]\n')
    module.configure(path, repository, 'main')
    html = tomllib.loads(path.read_text())['output']['html']
    assert html['site-url'] == expected
    assert html['git-repository-url'] == f'https://github.com/{repository}'
    assert html['edit-url-template'] == f'https://github.com/{repository}/edit/main/docs/book/{{path}}'
    assert html['additional-css'] == ['theme.css']
