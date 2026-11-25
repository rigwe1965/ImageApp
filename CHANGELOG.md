# Changelog

All notable changes to ImageApp. Follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versions use `MAJOR.MINOR.PATCH`.

## [Unreleased]

### Added
- LICENSE, SECURITY.md, this changelog, a pull request template and Dependabot config (project standard).
- `.env.example` listing all 17 settings with their defaults (all commented out, so copying it changes nothing), and the 7 that were missing from the README table; `tests/test_env_example.py` fails when the code reads a setting that is not documented in both, or documents one it no longer reads.

### Fixed
- README said `SECRET_KEY` defaults to a dev key; it is a new random value at each start.

### Security
- Uploads are decoded only as JPEG, PNG, WebP, BMP or GIF (other formats named .jpg/.png are refused), and Pillow moved to 13.x, which closes 42 published advisories.

Earlier history is in the git log. The first tagged release will be `v0.1.0`.
