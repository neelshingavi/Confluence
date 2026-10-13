# Confluence Ticketing Platform — Implementation Specification

## Product scope

Internal event operations only. The organizer uploads an Excel workbook and ticket artwork; the system issues personalized PNGs in a ZIP. Authorized volunteers scan the QR credentials on a mobile browser. One shared PostgreSQL database is the attendance source of truth. Attendees do not create accounts, register through the platform, or log into the platform.

## Confirmed decisions

- Event name: **Confluence**.
- Input: organizer-provided Excel workbook.
- Ticket artwork: organizer-provided image with an intended QR placement area.
- Printed dynamic data: Name, Year and Phone.
- Ticket output: individual PNG images in a ZIP; a `ticket_manifest.csv` is included in the ZIP.
- Entry: internet available; online server verification.
- Attendance output columns: Name, Year, Email, Phone, Attendance Status.
- No ticket emails/SMS are required in the specified workflow.

## Architecture

- Frontend: Vite-built single-page app, browser-based UI, `qr-scanner` camera decoder.
- API: FastAPI, Python, SQLAlchemy.
- Production persistence: PostgreSQL.
- Spreadsheet I/O: `openpyxl`.
- Ticket rendering: Pillow and `qrcode`.
- Authentication: signed server session cookie, scrypt password hashes, role checks at API endpoints, same-origin check and CSRF token for state-changing operations.
- Deployment: Docker Compose for a single app container, PostgreSQL container and persistent volumes; terminate public TLS at a trusted reverse proxy or host.

## User journey

1. OWNER or ADMIN authenticates.
2. Upload `.xlsx`; inspect headers and choose a column mapping.
3. Preview rows. Name is required; invalid and duplicate rows get explicit status. Commit inserts valid rows only. Email and import fingerprint constraints protect against repeated import.
4. Upload PNG/JPG/WebP artwork. Image is decoded and re-encoded as PNG, stripping embedded metadata. The visual editor stores normalized QR/name/year/phone positions.
5. Generate tickets. Existing valid ticket credentials are reused on ZIP re-generation. Missing tickets receive a new unique ticket number and cryptographically random credential. QR token is stored as a keyed lookup hash plus authenticated encryption, allowing the ZIP to be regenerated without changing the credential.
6. Scanner decodes QR payload and submits credential plus a unique request ID to API. Backend authenticates staff, checks event and ticket status, locks the ticket row in PostgreSQL, and inserts or activates a unique check-in record in the same transaction.
7. Subsequent scans receive a duplicate result. Invalid/revoked/event-mismatch results do not mark attendance. Server timeout/network failure is shown as unverified.
8. Admin exports current attendance to Excel. Rows for all registered attendees are present, regardless of whether they checked in.

## Database entities

- `events`: event metadata, Confluence seeded at startup.
- `staff_users`: individual staff identities, roles, active state and password hashes.
- `attendees`: imported roster fields, normalized email, per-event fingerprint, source row.
- `tickets`: unique per attendee/event, ticket number, status, QR credential HMAC and encrypted credential.
- `check_ins`: one row per ticket, current admission state and controlled reversal metadata.
- `scan_attempts`: outcome and response snapshot per scanner request ID; no raw QR token stored.
- `audit_logs`: administrative and event-day actions.
- `import_previews`: 2-hour staging record, payload and counts used to ensure the committed content is the content reviewed.
- `ticket_templates`: image path/dimensions and normalized field placements.

## Critical invariants

- Exactly one ticket record per attendee/event.
- Exactly one ticket number and QR token hash per ticket.
- A QR credential is not a PII-bearing identifier.
- Exactly one check-in state row per ticket; unique database constraint is the final duplicate prevention mechanism.
- Only API-side transaction can mark check-in.
- Repeated operation ID with same credential returns its saved result. Reusing the same operation ID for a different credential is rejected.
- Revoked tickets cannot check in.
- Administrative correction is audited and carries a reason.
- Export counts and data are calculated from persisted rows, not client state.

## API groups

- Auth: `/api/auth/login`, `/api/auth/logout`, `/api/auth/me`, `/api/auth/change-password`.
- Dashboard and roster: `/api/dashboard`, `/api/attendees`.
- Import: `/api/imports/inspect`, `/api/imports/preview`, `/api/imports/{previewId}/commit`.
- Design/tickets: `/api/template`, `/api/template/image`, `/api/template/placements`, `/api/tickets/generate`, `/api/tickets/{ticketId}/revoke`.
- Check-in: `/api/check-ins`, `/api/check-ins/recent`, `/api/check-ins/{checkinId}/reverse`.
- Reports/staff: `/api/reports/attendance.xlsx`, `/api/staff`, `/api/staff/{staffId}/deactivate`.
- Health: `/api/health/live`, `/api/health/ready`.

## Release acceptance

- Roster import idempotency verified on staging.
- Image positions validated at original output resolution.
- Random sample tickets scanned from an actual phone display and print.
- One valid scan marks one attendance row.
- A repeated or concurrent scan cannot create a second active check-in.
- Revoked QR is rejected.
- Unauthorized staff actions fail closed.
- Network timeout shows “not verified.”
- Export fields and all-student coverage verified.
- PostgreSQL backup restore tested.
- On-site network fallback and paper emergency roster are ready.

## Deployment limitations

The source package does not provision a public hostname, TLS certificate, hosted database, monitoring service, backup destination, or vendor account. A reachable HTTPS deployment and event-specific acceptance test are mandatory before volunteers use it. Online check-in is the only authoritative admission path in this version.
