# Publish development images

`.github/workflows/dev-container.yml` builds and checks the development image
for pull requests. Pushes to `main` also publish `latest` and `sha-<commit>` tags
to **`ghcr.io/<owner>/<repository>/dev`**, using lowercase repository names.
For this repository, use `ghcr.io/jgoppert/fastdyn/dev:latest`.
Same-repository pull requests publish a `pr-<number>` preview tag and a
`sha-<head-commit>` tag; pull requests from forks only build and check the image.
Each tag becomes available after the corresponding **Development container**
workflow finishes publishing it.

The image is generated from the same Nix development shell, including its
compiler setup hooks. There is no separate Dockerfile or package list.
Nix store caching reuses the compiler, QEMU, and Python dependencies; layered
images also reuse unchanged layers in the registry. Documentation edits do
not rebuild the native FastDyn plugin. Builds run on pushes rather than a
nightly schedule.

For a new repository, GitHub may initially create a private container package.
Its administrator can select **Package settings → Change visibility → Public**
to allow readers to pull without signing in. The workflow uses
the repository's `GITHUB_TOKEN` to publish and needs no personal access token.

The general documentation walks through [building the image with Nix, loading
it into Docker, sharing an archive, and running the container](../general/container.md).
That chapter also covers source mounts, host-owned results, port forwarding,
and a documentation preview. You need only Docker to use a published image;
the workflow uses Nix on the build machine.
