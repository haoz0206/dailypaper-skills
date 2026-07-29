# Releasing DailyPaper Skills

GitHub Releases are immutable public installation points for this repository.
Use semantic tags such as `v1.0.0`. Do not create a release directly from a
feature branch.

## Before the first release

1. Merge the unified implementation into `main`.
2. Confirm `origin/HEAD` points to `main`.
3. Confirm the fixed Vault repository policy is intentional for the release.
4. Confirm `README.md`, `NOTICE`, `LICENSE`, and `CHANGELOG.md` describe the
   actual tagged contents.
5. Enable branch protection for `main` and require the CI workflow.
6. Create the labels used by `.github/release.yml`, or accept the catch-all
   category until labels are available.

## Prepare a release

1. Choose the next semantic version.
2. Move completed entries from `Unreleased` into a dated version section in
   `CHANGELOG.md`.
3. Update pinned installation examples in `README.md` when the major/minor
   compatibility target changes.
4. Materialize and verify all public Skills:

   ```bash
   python3 tools/sync_public_skills.py
   python3 -m pip install -r requirements-dev.txt
   python3 tools/release_gate.py
   python3 tools/installer_smoke.py
   ```

   This is the same gate entry point used by ordinary CI and tag publication.

5. Validate each public `SKILL.md` with the target harness validator.
6. Confirm the installer smoke test used the version pinned in `README.md`,
   installed all four Skills into Claude Code and Codex, produced identical
   copies, and compiled both installed trees.
7. Commit the release preparation and merge it into `main`.

## Publish

Create an annotated tag on the tested `main` commit:

```bash
git switch main
git pull --ff-only origin main
git tag -a v1.0.0 -m "DailyPaper Skills v1.0.0"
git push origin v1.0.0
```

Pushing a semantic tag triggers `.github/workflows/release.yml`. The workflow
re-runs the release gates and creates a GitHub Release from the existing tag
using `.github/release.yml` for generated notes.

Do not move or overwrite a published semantic tag. If a release is wrong,
publish a new patch version. Before publishing, a draft release may be used for
manual review; after publishing, treat the tag and its source archives as
immutable.

## Verify

After GitHub Actions succeeds:

1. Open the Release page and review generated notes and contributor attribution.
2. Confirm GitHub marks the intended version as latest.
3. Test the README installation command against the published tag.
4. Confirm all four Skills are discovered.
5. Confirm `configure-dailypaper` is presented as the first-run entry.
6. Record any follow-up under `Unreleased`.

## Rollback

Do not force-update the release tag. If the release workflow fails before a
GitHub Release is created, fix the repository and create a new patch tag. If a
release was published with a severe problem, mark it as a pre-release or add a
warning, then publish a corrected patch release.
