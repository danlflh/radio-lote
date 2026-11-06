# Radio Lote terminal interface

Mode: Operate. The working object is a labelled radio in a selected batch.

- Primary structure: searchable batch table, batch detail with radio status,
  contextual operation area, and a stable footer with keyboard actions.
- Use the terminal font and Textual's native widgets, focus model and adaptive
  layouts. Dense tables are appropriate; avoid decorative cards and banners.
- Default to the terminal's light/dark preference with a saved override. Use a
  restrained cyan/blue accent for selection and primary actions. Success, pending
  and failure always include readable text as well as color.
- At 80x24, stack details and scroll supporting content. At 120x40, allow batch
  summary, table and operation details to share the viewport. Do not hide the
  current radio, operation state or recovery action when resizing.
- Keep channel numbers and technical settings behind an advanced editor. Show
  reference source, selected group count and human labels in the normal flow.
- Error messages name the failed stage and the next safe action. After a possible
  write, the primary action is verification, never an implicit fresh upload.
- Refresh progress only on meaningful driver/service events. No decorative
  animations. Dialogs are reserved for forms that need focused input and restore
  confirmation with the physical label.
- Portuguese and English have equivalent actions and status meanings. Text entry
  accepts accents; frequency fields accept a decimal comma or dot and store Hz.
