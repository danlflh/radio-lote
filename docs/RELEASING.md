# Release checklist

Releases are published on GitHub. The current CHIRP direct-URL dependency is
not accepted by PyPI; do not substitute the unrelated PyPI package named
`chirp` or remove the dependency guard to make publication succeed.

## Prepare

1. Update the application version and `pyproject.toml` together. Use a matching
   `vMAJOR.MINOR.PATCH` tag and update installation examples in both READMEs.
2. Keep all runtime versions in `pyproject.toml` and `requirements.lock`
   identical. CHIRP's commit and source-archive hash must also match `NOTICE`
   and the transport's revision guard.
3. If CHIRP changes, inspect its driver geometry and protocol before changing
   the pin. Run the full regression suite and a controlled physical pilot;
   source-code installation succeeding is not a compatibility check.
4. Check bilingual documentation against the actual CLI and UI. Describe
   experimental platform support accurately.

## Build and verify

Use a clean checkout and Python 3.12:

```sh
python -m pip install ".[dev]"
python -m build
python -m unittest discover -s tests -v
```

Set `RADIO_LOTE_WHEEL=dist` and rerun `tests.test_packaging`. Both the wheel
and source archive must contain no customer data, `.img` images, logs, local
environment, credentials or user-specific absolute paths. Review the archive
contents as well as the test output.

Install the wheel into a fresh isolated environment, run `--help`, `--version`,
`status`, `batch list` and `doctor` from outside the checkout with a temporary data
directory. These checks must not open a serial connection or start a TUI.
The CI workflow runs the wheel checks on all three supported desktop systems.

Before publishing, distinguish the validation already performed from any
remaining physical pilot. Emulator tests do not establish RF performance.

## Publish

Create the reviewed tag and a GitHub release for that tag. Attach the wheel,
source distribution and a SHA-256 checksum file. Retain the corresponding
source and license notices. CI builds and checks artifacts but does not create
public releases automatically.

Prefer installation examples pinned to the release wheel or source tag.
Never recommend installing from an unreviewed branch for a live batch.
Updates are explicit operator actions and must not run during programming.

After publication, verify the release asset URLs and a clean installation.
Do not claim this verification happened merely because an upload completed.
