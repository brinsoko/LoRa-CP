# OpenAPI spec workflow

The canonical OpenAPI document is at [openapi.json](openapi.json). The
Swagger UI proxy at `/docs/` reads it via `/api/docs/openapi.json`.

## Keeping it in sync

When you add, rename, or remove a route under `/api/...` (or `/health`,
`/ready`), regenerate the spec:

```bash
make openapi
git diff docs/openapi.json   # eyeball the changes
git add docs/openapi.json && git commit -m "docs: refresh openapi"
```

CI can enforce sync with:

```bash
make openapi-check
```

That target exits nonzero if running `make openapi` would change
anything, so a forgotten regeneration shows up as a failed build.

## What the generator does

[scripts/generate_openapi.py](../scripts/generate_openapi.py) walks the
live Flask `url_map`, filters to API + health routes, and updates
`docs/openapi.json` in place:

- Adds entries for newly-wired routes (with auto-derived path
  parameters and a stub `responses.default` block).
- Drops entries whose route was removed.
- Updates HTTP methods on existing routes.
- **Preserves** every hand-written `summary`, `description`,
  `requestBody`, `responses` block, and `components/schemas` definition.

It does NOT infer request/response schemas from view function
signatures. If you add a new field to a response, edit `openapi.json`
by hand (or describe it in the route's docstring and update the spec
to match).

## Long-term path

Two reasonable upgrades, post-launch:

1. **`flask-smorest` or `apispec`** — schema-first. You declare a
   Marshmallow schema per request/response and the framework emits a
   strict spec that also validates incoming requests. High value, but
   requires a pass over every route to add schemas.

2. **More aggressive auto-detection** — extend the generator to inspect
   `request.get_json()` calls and `return jsonify(...)` patterns to
   propose schemas. Brittle, but cheap.

For now, option (a) — manual schemas + auto-managed path index — is the
right balance: low maintenance burden and the spec stays accurate to
what's actually wired.
