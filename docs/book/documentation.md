# Preview and publish this book

The book is built with **mdBook**, an open-source GitBook-style static book
generator. It does not use GitBook.com's hosted service. Markdown chapters are
in `docs/book/`, navigation is in `docs/book/SUMMARY.md`, and build settings are
in `docs/book.toml`.

## View locally

If you already chose a [development environment](general/environment.md), run:

```bash
mdbook serve docs --open
```

Open **http://localhost:3000**. The server rebuilds and reloads saved chapters.
Stop it with Ctrl-C. To build only the static files, run `mdbook build docs`;
the output is `out/docs/`.

For a **docs-only Nix shell**, use:

```bash
nix develop .#docs
mdbook serve docs --open
```

This downloads mdBook and browser assets without building QEMU or the simulation
dependencies. If Nix is new to you, follow the [Nix setup links](general/environment.md#option-a-nix).

For a **manual docs-only installation**, install mdBook using its
[official instructions](https://rust-lang.github.io/mdBook/guide/installation.html)
(the book is checked with mdBook 0.5.2). With Python 3.11 or newer:

```bash
python3 -m venv out/docs-venv
source out/docs-venv/bin/activate
python -m pip install -e ./src
mdbook serve docs --open
```

The Python preprocessor downloads the pinned editor assets on the first build,
checks their hashes, and caches them under `out/docs-downloads/`. Node and Nix
are not needed for this path. The normal FastDyn venv already includes it.
For **Docker**, see the [container preview command](general/container.md#run-against-your-checkout).

## Modelica source panels

Use a `modelica` fenced code block for highlighted, read-only source panels.
For a repository model, put an mdBook include directive inside the fence
so the displayed source stays in sync with the file being compiled. Short
equation excerpts can go directly in a fence. Readers can copy the code, fold
sections, and use the book's light or dark theme.

The viewer uses Monaco and the shared Modelica language definition from
`@cognipilot/rumoca`, as in [Rumoca's user guide](https://cognipilot.github.io/rumoca/user-guide/language/neural-odes.html).
`docs/assets.toml` pins the npm archives by version and hash. Both the Nix
shell and the manual Python preprocessor stage their browser assets and licenses
under the ignored `docs/book/vendor/` directory. The build and local preview
commands above do this automatically; readers load the assets from the book's
own server. The viewer's npm version is independent of the native compiler
used to produce FMUs. Ordinary source blocks remain available without
JavaScript and when printing.

## Automatic GitHub Pages deployment

`.github/workflows/docs.yml` builds the book for pull requests and main-branch
pushes. Every build uploads a **documentation-preview** artifact. Main-branch
builds also deploy the site to GitHub Pages:

**https://jgoppert.github.io/FastDyn/**

The site becomes available after the first successful deployment from `main`.
PR builds provide downloadable previews and do not replace the published site.
For another repository, select **Settings → Pages → Build and deployment →
GitHub Actions** once. The workflow derives repository links and the URL prefix
from the current GitHub repository; no source changes are needed upstream or
in a fork. The `jgoppert/FastDyn` Pages setting is already enabled.
