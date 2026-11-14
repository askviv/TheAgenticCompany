# Changelog

Versioned changelogs are published automatically in
[GitHub Releases](https://github.com/askviv/TheAgenticCompany/releases).
Each release lists merged pull requests, contributors and the comparison with
the preceding release. GitHub generates these notes; there is no second,
manually maintained copy of the release history.

## Releasing

After merging the intended changes, tag the release commit and push the tag:

```sh
git tag v1.1.0 <release-commit>
git push origin v1.1.0
```

Replace the example version and commit with the intended release. Use
`vMAJOR.MINOR.PATCH`: major for breaking changes, minor for new functionality,
and patch for fixes. A tag with a suffix, such as `v1.1.0-rc.1`, is published as
a prerelease. No release is created merely by merging a pull request.

The `Publish Release Changelog` workflow publishes the release and generates
its notes using `.github/release.yml`:

- **Upgrade Notes:** PRs labelled `breaking-change` or `migration`.
- **Added:** PRs labelled `enhancement` or `feature`.
- **Fixed:** PRs labelled `bug` or `fix`.
- **Changed:** all remaining PRs, including unlabelled changes.

Use clear PR titles and include concrete upgrade instructions in PRs that need
them. Generated notes link to those PRs; they do not infer migration instructions
from source code. Empty categories may be omitted by GitHub.

This workflow publishes release notes only. It does not deploy the Mini, run
experiments or build images. The existing task-image publishing workflow remains
separate. Company-pack versions and pack schema versions are separate from the
platform's release tags.

The first tagged release after this workflow is merged creates the first
automated changelog. Existing releases are not rewritten.
