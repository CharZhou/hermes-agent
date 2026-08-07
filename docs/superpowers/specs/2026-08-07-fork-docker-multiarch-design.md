# Fork Docker Multi-Architecture Publishing

## Goal

Publish the fork's GHCR Docker image for both `linux/amd64` and `linux/arm64`,
while keeping the existing per-image smoke checks and tag names.

## Design

The fork workflow will use a two-entry build matrix with the native GitHub
hosted runners used by the upstream workflow. Each matrix job will build one
platform with `load: true`, run the existing `--help` and `dashboard --help`
smoke checks, and push that platform's image by digest to GHCR.

After both matrix jobs finish, a separate merge job will download the digest
artifacts and create one registry-side multi-platform manifest. The merge job
will apply the existing `main`, `latest`, full SHA, and short SHA tags to the
manifest list. No image layers are rebuilt or copied during the merge.

The image name remains configurable through `FORK_IMAGE_NAME`, and the
workflow continues to authenticate only to GHCR with `GITHUB_TOKEN`.

## Testing

The workflow test will assert that the build matrix contains exactly
`linux/amd64` and `linux/arm64`, that both jobs publish by digest, and that the
merge job creates and inspects the multi-platform manifest. It will retain
checks for the fork image naming, triggers, and absence of overlay variants.

## Failure Handling

The matrix uses `fail-fast: false` so a failing architecture reports its own
error. The merge job depends on both architecture jobs, so it cannot publish
tags pointing at an incomplete manifest. A failed manifest creation is
retried before the workflow exits unsuccessfully.
