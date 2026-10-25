# AutoSprint

Turn a Jira sprint into a formatted **Excel sheet, PDF and summary image**, and
post them to a **Zoho Cliq** channel — on a schedule, with nobody present.

Built for teams who maintain their work in Jira but have to report it as a
spreadsheet. Instead of rebuilding that sheet by hand every sprint, AutoSprint
reads the sprint, translates each team's Jira statuses into the vocabulary
management reads, and delivers the files.

- **Pull from Jira** with a saved JQL query or filter ID — or upload a Jira CSV
  export and run with no credentials at all
- **Per-project configuration** of status buckets, percentage KPI cards, column
  order and colours
- **Three outputs** — `.xlsx`, `.pdf`, `.png`
- **Scheduled delivery** to a chat channel on the days you choose
- **No database** — configuration lives in a Zoho Sheet, or local JSON files

Python 3.12 + Flask, server-rendered. No JavaScript build step.

---

## Quick start

### Run it locally

```bash
git clone https://github.com/aayuk11/AutoSprint.git
cd AutoSprint

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # optional - see "No credentials?" below
python app.py
```

Open <http://localhost:8501>.

### Run it with Docker

```bash
cp .env.example .env               # fill in what you need
docker compose up -d --build
curl http://localhost:8501/healthz
```

A healthy response reports `"status": "ok"` and names any integration that is
not configured, with the exact variables to set.

### No credentials?

It works with none. Export your sprint from Jira as CSV (**all fields**), upload
it on step 2, and download the reports. Credentials only unlock fetching from
Jira directly and posting to chat.

---

## How to use it

The app is a three-step wizard.

### 1 · Sprint Details

Sprint number, start and end dates, release dates, scrum master, sprint goal and
up to three major items. Saved per project, so next sprint you only change the
number and the dates.

Choose **Product Sprint** or **Design Sprint** — design sprints use a single
release date and rename the label column to "Requirement Type".

Type a name into **New project** to create a project. No redeploy needed.

### 2 · Get Jira Data

Either paste a JQL query, or give a saved filter ID, or upload a CSV export.

```
project = ABC AND sprint in openSprints() ORDER BY key ASC
```

Use `sprint in openSprints()` so the query follows the current sprint. If you
pin a specific sprint (`sprint = 4408`), the app warns you on screen and in the
posted message, because the same sprint would otherwise be reported forever.

The query is saved per project, so it is typed once.

After fetching, a **data health** panel reports what was understood: issue
counts, which statuses were seen, which ones are not mapped yet, and which
issues have no dates. Read it before generating.

### 3 · Report

KPI tiles, bucket counts, an inline preview of the summary image, download
buttons, and a form to post to a chat channel.

### Settings

Per-project configuration:

| Section | What it controls |
|---|---|
| **Status buckets** | The groups management reads — "In Progress", "QA Review", "Production". Clearing a bucket's name deletes it |
| **Status mapping** | Which Jira status belongs in which bucket. `(ignore)` means it counts toward nothing |
| **% KPI cards** | A named percentage over one or more buckets |
| **Task columns** | Which of the 14 columns appear, and in what order |
| **Schedule** | Enabled, which weekdays, which files, whether to post the summary |

A new project starts with 9 buckets, 4 KPI cards and ~20 common Jira statuses
already mapped, so most teams only adjust one or two.

**Adding a status takes two saves:** type it into **Add statuses** and save, then
pick its bucket and save again. The dropdown is built from statuses the app
already knows about.

**The percentages are not meant to total 100%.** The denominator is every
non-epic issue, so a gap means something is unmapped — that is the signal, not a
bug.

---

## Configuration

All configuration is environment variables, or a local `.env` (gitignored). See
[`.env.example`](.env.example) for the annotated list.

| Group | Needed for | Notes |
|---|---|---|
| `SECRET_KEY` | signing sessions | Required once `APP_PASSWORD` is set |
| `APP_PASSWORD` | the login screen | Leave empty to disable the gate |
| `JIRA_*` | **Fetch from Jira** | Atlassian email + [API token](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `ZOHO_*` | **shared storage** | A Zoho Sheet holding projects, mappings, queries and channels |
| `CLIQ_*` | **Post to Cliq** | Zoho self-client — see scopes below |

### Zoho Cliq scopes

```
ZohoCliq.Channels.READ, ZohoCliq.Chats.ALL,
ZohoCliq.Messages.CREATE, ZohoCliq.Webhooks.CREATE
```

`Webhooks.CREATE` is the one people miss. The file-upload endpoints are gated on
it, so without it the message posts and every attachment fails with
`oauthtoken_scope_invalid`.

Generate the self-client in the API console for **your** Zoho data centre
(`accounts.zoho.com`, `.in`, `.eu`, …) and set `ZOHO_LOCATION` / `CLIQ_LOCATION`
to match. A client from the wrong region returns `invalid_client`.

### Storage

With `ZOHO_*` set, configuration lives in one worksheet of a Zoho Sheet with
four columns: `store`, `project`, `data`, `updated_at`. Without it, the app
falls back to JSON files under `data/`, **which do not survive a container
restart**. The banner on every page shows which mode is active.

The store is append-only: saving adds a row rather than replacing one, and the
newest row for a key wins. Nothing is lost, but the sheet grows — worth
compacting occasionally on a long-lived install.

### Access control

Setting `APP_PASSWORD` turns on a login screen: a shared password plus an email
on the allowed domain **and** on the allowlist. Addresses in
`AUTH_ALLOWED_EMAILS` always work and cannot be removed from the UI — they are
the way back in. Everyone else is managed from the **Access** page.

Be clear about what this is: **the password is the control.** The email is
recorded, not verified. The allowlist limits *who* can enter and lets you revoke
one person, but it is not proof of identity. Keep the app on an internal network
or behind an authenticating proxy. `/healthz` stays public so monitoring needs
no credentials.

---

## Scheduled reports

Each project can post automatically. On **Settings → Schedule**, set the days,
which files to post, and whether to include the summary message. **Run now**
triggers it immediately, which is the quickest way to test the whole path.

A project can only be scheduled once it has a saved Jira query, a channel and
saved sprint details. The UI names whatever is missing.

The timer runs **inside the web process**, so there is nothing extra to deploy.
Keep the deployment at **one replica with `WEB_CONCURRENCY=1`** — a timer in the
app fires in every worker and every replica. Three guards make duplicates
unlikely (a once-a-day check, a claim-then-verify write, a per-host lock), but
one instance is the clean answer.

For a cron or CI runner instead:

```bash
python run_scheduled.py --due                  # whatever is due now
python run_scheduled.py --project "ABC" --force
python run_scheduled.py --all --dry-run        # build, post nothing
```

If you scale to several pods, set `SCHEDULER_ENABLED=false` on the web
deployment and run `run_scheduled.py --due` from a scheduled job instead. The
logic is shared, so it is a configuration change rather than a rewrite.

---

## Deployment notes

| | |
|---|---|
| Stack | Python 3.12 + Flask, served by gunicorn |
| Listens on | **8501** (`PORT` overrides) |
| Health check | `GET /healthz` → 200, no auth required |
| Persistent volumes | **None** — state is in the Zoho Sheet; scratch files go to temp |
| Database | None |
| Outbound access | HTTPS to your Jira host and to `accounts.zoho.*`, `sheet.zoho.*`, `cliq.zoho.*` |
| Footprint | ~400 MB image; 300–500 MB RAM; peak ~110 MB during a report build |

Credentials are never in the repository or the image — `.env` is excluded by
both `.gitignore` and `.dockerignore`. Inject them however your platform does
secrets; anything that ends up as environment variables works.

Updating is `git pull` then rebuild. No migrations, no schema. Restarting is
safe at any time — anyone mid-report just regenerates it.

Behind a reverse proxy, forward `Host` and `X-Forwarded-*` as usual, and set
`SESSION_COOKIE_SECURE=true` when serving over HTTPS.

---

## Helper scripts

```bash
python check_creds.py              # verify Zoho credentials, scopes, channel membership
python check_creds.py --writetest  # also prove the sheet is writable
python test_schedule.py            # show what a scheduled run would do, build nothing
python test_schedule.py --post     # run it for real
```

`check_creds.py` prints scopes and pass/fail only — never a credential value.

---

## How it works

```
Jira  ──►  AutoSprint  ──►  .xlsx / .pdf / .png  ──►  Zoho Cliq channel
             │   ▲
             │   └── Zoho Sheet (settings, mappings, channels)
             └────── scheduled run, no browser needed
```

1. Fetch issues via the Jira REST API (`POST /rest/api/3/search/jql`), paginated.
2. Normalise into a table whose column names match Jira's CSV export, so the API
   and CSV paths converge on one shape.
3. Build an epic → story → sub-task hierarchy. Nothing is dropped: issues whose
   parent is missing appear under `[EXTERNAL EPIC]`, parentless ones under
   `UNLINKED / STANDALONE ITEMS`.
4. Count each status into its bucket, compute the KPI percentages.
5. Render the three files and post them.

The PDF is written without a PDF library — `modules/pdf_generator.py` emits the
page objects and cross-reference table directly.

---

## License

No license has been chosen yet, so default copyright applies: you may view this
code, but not reuse it. Add a `LICENSE` file if you intend otherwise.
