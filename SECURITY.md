# Security policy

## Reporting a problem

Please do not open a public issue for a security problem. Email **info@facyke.com** with what you found and where, how to reproduce it, and what an attacker could do with it. You will get a first reply within 5 working days.

## What is in scope

The code in this repository: upload handling, image decoding, the processing routes and the temporary-file handling. Past reviews are in `audits/`.

## Running it safely

- Uploads are decoded only as JPEG, PNG, WebP, BMP or GIF, and are limited in file size, pixel count and batch size. Keep those limits (see the Settings section of the README) when you change them.
- Set a long random `SECRET_KEY` and run behind HTTPS and a reverse proxy.
- Keep Pillow current: CI fails the build when `pip-audit` finds a known vulnerability in a dependency.

## Supported versions

Only `main` receives fixes.
