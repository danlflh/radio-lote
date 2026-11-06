# Architecture and operational invariants

The terminal UI and command line share the same local services. They do not
implement separate serial programming protocols. Hardware access remains in
the transport adapter around the pinned CHIRP H777 driver.

## Programming cycle

1. Resolve the selected local USB serial port and validate driver compatibility.
2. Read the individual radio and persist its original image and hashes.
3. Reopen and validate the saved backup before building an image for that unit.
4. Apply only the profile's permitted memory fields to a copy of that backup.
5. Persist and reopen the prepared image before uploading it.
6. Read the radio again and compare the complete raw memory with the prepared
   image; CHIRP file metadata is excluded from the comparison.
7. Record memory verification separately from a manual voice check.

The radio family's clone image is 992 bytes. The driver may normalize reserved
bits while editing a channel. The application therefore merges only the
permitted frequency/tone fields and the FM/NFM bit, preserving unrelated data.
Extending this rule to another driver without inspecting its memory format is
not supported.

## Serial sessions

Each transfer closes its serial connection. The next transfer waits for clone
mode to settle, reopens the port and clears residual input. Physical testing
found an `F` reply after `E` which the upstream driver does not consume; the
emulator reproduces it. The driver repeats its compatibility handshake for
each transfer.

There is no automatic upload retry. A failure after writing may have started
leaves the unit pending verification. A subsequent operator retry only reads
and compares the same unit. Restoring a backup is a separate operation that
first backs up the currently connected radio.

## Persistence

Application data belongs in the user's data directory, not inside the source
checkout or virtual environment. An explicit data-directory option overrides
the platform default. Batch and profile management must retain attempt history
and immutable original images.

Exports contain operational data and should be treated as customer documents.
Import must validate archive paths and integrity before making a batch usable.
Legacy migration copies a quiescent source and leaves it intact.

## Validation boundaries

- A USB identifier describes the cable, not a unique physical radio.
- A successful compatibility handshake does not establish frequency-use rights.
- Readback verifies stored bytes, not radiated power, emitted RF tones, range
  or audible communication.
- Offline CI runs without hardware. New operating-system or radio combinations
  need a physical pilot before being described as physically validated.

The project intentionally keeps these boundaries visible in documentation and
reports rather than treating emulator success as a hardware guarantee.
