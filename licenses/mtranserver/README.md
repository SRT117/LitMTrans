# MTranServer release compliance

MTranServer is distributed as a separate local translation runtime. Before a
release includes `resources/mtranserver/bin/mtranserver-windows-amd64.exe` or
any directory under `resources/mtranserver/models/`, add the upstream source,
version, license, copyright notice, redistribution terms, and any required
corresponding-source location to this directory and to
`THIRD_PARTY_NOTICES.md`.

Only language directories listed in `MTRAN_RELEASE_MODELS` in
`litmtrans.spec` are included in a release. Keep the allowlist empty until
each selected language pack has been reviewed.
