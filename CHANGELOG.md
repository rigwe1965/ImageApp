# Changelog

All notable changes to ImageApp. Follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versions use `MAJOR.MINOR.PATCH`.

## [Unreleased]

### Added
- LICENSE, SECURITY.md, this changelog, a pull request template and Dependabot config (project standard).

### Security
- Uploads are decoded only as JPEG, PNG, WebP, BMP or GIF (other formats named .jpg/.png are refused), and Pillow moved to 13.x, which closes 42 published advisories.

Earlier history is in the git log. The first tagged release will be `v0.1.0`.
