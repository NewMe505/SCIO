# Rename format_cents to format_money with a currency argument

Every module in `shop` formats amounts through `shop.money.format_cents`, which only
knows dollars. Replace it with `format_money(cents, currency="USD")`: `USD` renders
`$`, `EUR` renders `EUR ` as a prefix, `GBP` renders `GBP `, and any other code
raises `ValueError`. The sign handling and the two-decimal rendering stay as they are.
Every caller moves to the new name; the old name must not remain anywhere in the
package.

Acceptance criteria:
- [ ] `format_money(1234) == "$12.34"`, `format_money(1234, "EUR") == "EUR 12.34"`, `format_money(-250, "GBP") == "-GBP 2.50"`, and `format_money(1, "XXX")` raises `ValueError`
- [ ] No module under `shop/` references `format_cents`; every caller uses `format_money` and the existing module tests still pass with dollar output
- [ ] `tests/test_money.py` covers the new function
