#!/usr/bin/env python3
"""Set mdBook's repository links and URL prefix for this GitHub repository."""
import argparse
from pathlib import Path
import re
import tomllib

import tomli_w


def configure(path, repository, branch, base_path=None, server_url="https://github.com"):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Repository must be OWNER/REPO")
    config = tomllib.loads(path.read_text())
    owner, name = repository.split('/')
    if base_path is None:
        base_path = '/' if name.lower() == f'{owner.lower()}.github.io' else f'/{name}/'
    html = config['output']['html']
    html['git-repository-url'] = f'{server_url}/{repository}'
    html['edit-url-template'] = f'{server_url}/{repository}/edit/{branch}/docs/book/{{path}}'
    html['site-url'] = '/' + base_path.strip('/') + '/' if base_path.strip('/') else '/'
    path.write_text(tomli_w.dumps(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book', type=Path, default=Path('docs/book.toml'))
    parser.add_argument('--repository', required=True)
    parser.add_argument('--branch', default='main')
    parser.add_argument('--base-path')
    parser.add_argument('--server-url', default='https://github.com')
    args = parser.parse_args()
    configure(args.book, args.repository, args.branch, args.base_path, args.server_url)


if __name__ == '__main__':
    main()
