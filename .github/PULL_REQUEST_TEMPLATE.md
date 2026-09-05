## What

<!-- One paragraph: the change and why. Link the issue. -->

## How to verify

<!-- Commands / curl calls / screenshots. Tests you added. -->

## Checklist

- [ ] `ruff check app tests` and `pytest -q` pass locally
- [ ] Nothing city-specific was hardcoded (Bogotá lives only in `cities/bogota.yaml` and fixtures)
- [ ] No secrets, personal paths or credentials in the diff
- [ ] `docs/API.md` updated if the contract changed (and `CHANGELOG.md`)
