Batch catalog lookups: one call for price and stock instead of two calls per item

Hypothesis: the service calls GET /products/{sku} twice per line (once for price, once for a stock check). One batched lookup returns both.
