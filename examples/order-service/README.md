# Example: order-service

An HTTP order service (SQLite) that prices orders from a supplier catalog, reserves stock and takes payment. The supplier
is a Crossfade world (`crossfade/world.py`). The baseline calls `GET /products/{sku}` twice per order line.

Candidates (`candidates/`): `batch-lookups` (one batched call; accepted) and `skip-reservation` (no reservation; faster and
rejected). `crossfade example order-service <dir>` builds a git repo with both as branches.
