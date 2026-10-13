# Confluence — Ticket Generator & Attendance Check-in

A self-hostable event-operations application for the exact Confluence workflow:

1. Import the final student roster from Excel (`.xlsx`).
2. Upload the ticket artwork and drag the QR/name/year/phone elements into place.
3. Generate a ZIP containing one personalized PNG ticket per active attendee.
4. Use the mobile scanner on the event's HTTPS URL to validate QR credentials and mark attendance centrally.
5. Export all students and their current attendance status to Excel.

There is **no attendee login or registration portal**. Attendees receive the image ticket that the organizer distributes. The QR credential is validated by the server when scanned.

## What is included

- Responsive staff UI: overview, Excel import/preview, visual template editor, ticket ZIP generation, scanner, attendee list, reports and staff management.
- FastAPI API with PostgreSQL (Docker deployment) and SQLAlchemy models.
- Unique random QR credentials; SHA-256 HMAC lookup hashes; encrypted token storage to enable safe re-generation of ticket images.
- Transactional check-in, a unique database constraint per ticket, request idempotency, and server-side role authorization.
- Audit trail for imports, ticket generation, staff changes, ticket revocation, attendance reversals and exports.
- Excel attendance export with exactly: Name, Year, Email, Phone, Attendance Status.
- Docker Compose with a PostgreSQL database, persistent volumes and an Alembic bootstrap migration.

## Quick start (local Docker)

Prerequisites: Docker Desktop (or Docker Engine + Compose) and an internet connection for the first container build, which installs pinned-range Python and JavaScript dependencies.

1. In this folder, generate local secrets and a first-owner account:

   ```bash
   python3 scripts/setup_local.py
   ```

   Store the displayed password securely. The script creates `.env`; `.gitignore` excludes it.

2. Build and start the application:

   ```bash
   docker compose up --build -d
   docker compose logs -f app
   ```

3. Open `http://localhost:8000` in a desktop browser and sign in with the initial owner credentials printed by the setup script. Change the password in **My account** after the first login.

4. In **Import students**, select your final `.xlsx` roster, verify the mapping and preview, and commit it.

5. In **Ticket design**, upload the supplied ticket design (PNG, JPG or WebP) and drag the QR, name, year and phone fields into their designated areas. Save the layout.

6. In **Generate tickets**, download `Confluence-tickets.zip`. It contains individual PNGs named with ticket number and attendee name, plus a `ticket_manifest.csv`.

7. Before the event, deploy the same application behind HTTPS on a stable hostname, create staff accounts, and test scans on the actual phones and venue network. Do not rely on a LAN URL served over plain HTTP for phone camera access; camera permissions require a secure context.

8. After the event, open **Attendance report** and download `Confluence-attendance.xlsx`.

Stop the app with `docker compose down`. Persistent PostgreSQL records remain in the Docker volume. `docker compose down -v` deletes the database volume and must not be used for a live event unless you intentionally want to destroy the data.

## Upload requirements

### Excel roster

- Supported: `.xlsx`, first worksheet, first non-empty row as the header row.
- Maximum file size: 15 MB. Maximum 10,000 data rows.
- Name is required; Year, Email and Phone may be blank, but use the values you intend to retain in the attendance export.
- Common header names are detected automatically; custom column names can be mapped on screen.
- The preview labels invalid rows and possible duplicates. Only `VALID` rows are imported; duplicates and invalid rows are skipped and counted. The system does not silently update a previously imported student's details.
- Store phone numbers as text in Excel when leading zeros or a plus-prefixed country code matters.

### Ticket artwork

- Supported: PNG, JPG/JPEG, WebP. SVG and PDFs are not accepted as templates in this first release.
- Minimum 400×250 pixels; maximum 24 megapixels / 12 MB upload.
- The preview uses normalized positions, while the generated ticket is rendered at the artwork's original dimensions.
- The QR should be on a plain, high-contrast area with enough room around it. Scan-test tickets after generation before distributing them.
- Name, Year and Phone are overlaid from the workbook. Put these fields in visibly empty areas of the supplied design.

## Event-day operating checklist

- Use a production HTTPS hostname, not a temporary preview URL.
- Import the final roster, review duplicate/invalid rows, and reconcile the registered count.
- Upload the final artwork, save placement positions, generate the ZIP and open at least 10 random ticket images to check name/phone/QR quality.
- Verify one valid ticket, one duplicate scan, one revoked ticket and one invalid QR on the actual volunteer devices.
- Create individual volunteer accounts; do not share logins.
- Ensure the hosted API/database is reachable through the venue network and take power banks plus an alternate internet connection.
- Export a printed or offline emergency roster before doors open. The scanner intentionally fails closed: a camera read or network timeout is not an approval.
- After the event, export the attendance workbook and store both it and the ticket ZIP in an access-controlled location.

## Configuration and secrets

`SESSION_SECRET`, `APP_FERNET_KEY`, `QR_TOKEN_PEPPER`, the database password and bootstrap credentials are private secrets. `scripts/setup_local.py` creates unique development values. Keep `.env` private and back it up securely if needed.

**Do not change `APP_FERNET_KEY` or `QR_TOKEN_PEPPER` after issuing tickets.** The Fernet key is required to recreate a ticket image using the same QR credential; the pepper is required to validate existing QR credentials. Rotate them only through a planned credential-reissue procedure.

The bootstrap email/password are used only if there is no staff user in the database. After the first startup and after securely changing the owner password, remove `BOOTSTRAP_ADMIN_PASSWORD` from `.env` if you no longer need the bootstrap recovery value. Do not reset or delete the database to recover access.

Set `COOKIE_SECURE=true` in a production deployment behind HTTPS. The setup script sets it to `false` for local `http://localhost` testing only. If TLS terminates at a reverse proxy, configure proxy headers and restrict direct access to the app port.

## Database / deployment notes

- Production database: PostgreSQL. SQLite is not the configured deployment path and should only be used for local development/testing.
- The app is a same-origin deployment so staff session cookies are not sent to a separate frontend origin.
- The Docker image runs one Uvicorn worker by default. The ticket check-in correctness guarantee comes from PostgreSQL row locking and a unique `check_ins.ticket_id` constraint, not the worker process.
- Schema changes must be versioned. The included initial Alembic revision bootstraps the initial schema; subsequent changes should be reviewed and created with Alembic autogeneration, then tested against a disposable PostgreSQL database before deployment.
- Take regular database backups and test restoration before event day. Persistent storage by itself is not a backup.
- Configure TLS, firewall/access restrictions, infrastructure monitoring and backup retention on the hosting provider. Those are deployment responsibilities and are not created automatically by this source ZIP.

## Security behaviour

- QR codes contain opaque random credentials, not names or other personal information.
- Only authorized staff can use the check-in endpoint. Attendee details are returned to the scanner only after the QR token is found and validated for the event.
- Repeated scans are rejected and recorded; invalid QR tokens are never approved. Revoked tickets are rejected.
- Scanner response timeout displays `NOT VERIFIED`; it never assumes the entry was accepted.
- Ticket revocation and check-in reversal are audited. Reversals require an administrator reason.
- The attendance export includes all registered students, including those without an active check-in, so no-shows remain visible.

## Verification

The backend unit tests and a full API workflow were exercised with disposable SQLite test data: Excel preview/import, duplicate re-import, image-template upload, PNG ZIP generation, stable QR credentials across regeneration, successful/idempotent check-in, duplicate rejection, invalid/revoked QR rejection, attendance workbook columns/rows, and password session invalidation. These tests verify application behaviour but do not substitute for a PostgreSQL concurrency test or a deployment/HTTPS device rehearsal.

Run the included automated checks with:

```bash
cd backend/..
python3 -m pip install -r backend/requirements.txt
pytest -q
node --check frontend/src/main.js
```

The browser bundle must also be built with npm during the Docker build.

## Development

API development (after installing `backend/requirements.txt` and setting environment variables in your shell):

```bash
cd backend
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Frontend development (in another terminal):

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` calls to `http://localhost:8000`. For local backend development, set `COOKIE_SECURE=false`, configure the secrets from `.env`, and set `DATABASE_URL` to a PostgreSQL URL or a disposable SQLite URL. Do not use SQLite to validate production concurrency semantics.

API documentation is available at `/api/docs` when the app is running.

## Known scope boundaries

- This source package is not yet deployed to a public host. Cross-device event check-in requires hosting the API and PostgreSQL on a stable HTTPS origin.
- Ticket design and student Excel are intentionally uploaded by the organizer at runtime; they are not bundled in the code ZIP.
- This build does not send tickets by email or SMS. The intended flow is ZIP export followed by the organizer distributing the individual PNGs.
- The ZIP manifest is included for the organizer and should not be distributed publicly.
- Online validation is required. Offline admission is not automatically approved; keep a controlled emergency roster and reconcile any manual admissions after connectivity returns.
- The provided setup is an initial production-oriented application package, not a substitute for a deployment-specific security review, backup restore drill, or on-site acceptance test.
