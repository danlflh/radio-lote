# Security policy

Security fixes target the latest published release. A dependency or hardware
compatibility change requires its own verification before release; the
application does not update itself during a batch.

Report vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/danlflh/radio-lote/security/advisories/new)
when available. If private reporting is unavailable, open an issue requesting
a private contact channel without publishing exploit details or customer data.

Useful details include the application version, operating system, affected
operation, expected behavior, and a minimal reproduction using synthetic data.
Do not attach real radio images, complete batch exports, credentials or
unredacted logs to a public report.

Areas requiring particular care include archive import/path traversal,
overwriting backups, bypassing profile integrity checks, concurrent access to
one radio, and repeating writes after a communication failure.

Ordinary compatibility issues can use public issues with redacted diagnostics.
Never demonstrate a problem against a radio or system you are not authorized
to operate.
