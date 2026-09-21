# Vendored charting library

Chart.js and its date adapter, served from this image rather than from a CDN.

| File | Package | Version | SHA-256 |
| --- | --- | --- | --- |
| `chart.umd.js` | [chart.js](https://www.npmjs.com/package/chart.js) | 4.4.0 | `321e3a3fa98da4aaa957d10be57cbb514de0989eed8f9d726b5d05902cd01904` |
| `chartjs-adapter-date-fns.bundle.min.js` | [chartjs-adapter-date-fns](https://www.npmjs.com/package/chartjs-adapter-date-fns) | 3.0.0 | `ea7ab30d26c38dcf1f2d26bb43e73a94537b58f1906f55e1a546dd09321b5615` |

Both are MIT licensed; the adapter bundle includes date-fns, also MIT. Each
file carries its own licence banner, which is why they are checked in exactly
as npm publishes them — unmodified, not rebuilt, not re-minified.

## Why they are here rather than on jsDelivr

The rest of this app fetches nothing at runtime. That is a deliberate rule:
the forecast, the nowcast and every number on the page come from the
station's own readings, and the page is self-contained by design. Two
`<script src="https://…">` tags were the one exception, and they were the
expensive kind — a script tag with no `integrity` hands whoever controls that
origin the ability to run code on the page.

Serving them locally also removes the failure mode they created: the page
had to carry a "charts need network access" fallback, and behind the HTTPS
reverse proxy a CDN outage or a blocked request left the dashboard with
values but no graphs.

## Updating

Take the published artifact, do not rebuild it:

```bash
cd "$(mktemp -d)"
curl -sSO https://registry.npmjs.org/chart.js/-/chart.js-<version>.tgz
tar xzf chart.js-<version>.tgz package/dist/chart.umd.js
cp package/dist/chart.umd.js <repo>/app/static/vendor/
sha256sum <repo>/app/static/vendor/chart.umd.js   # update the table above
```

The same for `chartjs-adapter-date-fns`, whose file is
`package/dist/chartjs-adapter-date-fns.bundle.min.js`.

Fetch from the npm registry rather than from a CDN: jsDelivr prepends its own
banner to what it serves, so its copy is not byte-identical to what the
project published.

After updating, load the dashboard and check that all four charts draw — the
adapter and Chart.js are version-coupled, and a mismatch shows up as a time
axis with no ticks rather than as an error.
