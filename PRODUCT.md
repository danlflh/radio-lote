# Radio Lote

## Platform

Local terminal application, Python 3.12, Textual. macOS, Windows and Linux.

## Users and purpose

Operators programming sets of compatible handheld radios for customers and teams.
The operator should select a batch, connect the labelled device and press Enter,
without editing JSON or depending on an AI chat. The first supported adapter is
the Baofeng 777/888 family already exercised on macOS.

## Confirmed workflow

Create a batch, choose a reference or existing profile, optionally create new
groups, back up each device, apply only selected fields, write, read back and
verify. Record manual voice checks separately. Keep recovery inside the terminal.
Frequencies are inherited or deliberately edited, never generated at random.
New DCS choices are made once per profile; integrating an existing team preserves
its tones. A USB cable is not an individual radio identity.

## Constraints

Local data, no service account, cloud dependency or telemetry. Preserve originals
and historical attempts. A write that might have started requires verification
or explicit restoration after any restart. No automatic repeated upload.
Each release pins its radio driver. Public examples use synthetic fixtures only.
Portuguese and English interfaces. Command-line and interactive interfaces share
the same domain operations. Labels identify radios within a batch, not across
customers automatically.

## Evidence

Legacy workflow was exercised with actual radios; automated tests cover memory
preservation, readback, serial framing, residual F response, and retries. Only
macOS has physical validation so far. Linux and Windows require their own
documented hardware validation in addition to CI.
