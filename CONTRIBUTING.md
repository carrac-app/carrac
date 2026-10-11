# Contributing

Carrac is published as a reference release. There is no roadmap, and pull
requests may not be reviewed; for a change you need, fork the repository.
Security issues go through [SECURITY.md](SECURITY.md), never a public issue.

## Pull requests

- **One concern per pull request**, with a line on how to check it works:
  a command to run, a page to load, a value to look at.
- **Make the checks pass** ([Tests](README.md#tests) has the setup):

  ```bash
  make lint     # ruff + eslint
  make test     # lint, the JS tests, then the full pytest suite
  ```

- **Sign off every commit** (`git commit -s`). Contributions are accepted
  under the [Developer Certificate of Origin](https://developercertificate.org/);
  there is no CLA, and you keep your copyright.

## Conventions

- Follow the file you are in: vanilla JavaScript with no build step, and one
  Alembic migration per schema change (`make migration msg="..."`).
- `static/js/indicator-registry.js` declares every indicator; a new one
  appears in every menu once it is registered there.
- Code is baked into the Docker image, so a change reaches a running container
  only through a rebuild (`docker compose up -d --build`). See
  [docs/architecture.md](docs/architecture.md).
