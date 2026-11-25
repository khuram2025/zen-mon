# Trial downloads for zentryc.com

Sources of the gated ZenPlus appliance download, in the same spirit as `integrations/zentryc_ota/`, `inventory-kb/` and
`udt-kb/`: code for the zentryc.com Django website kept under review in this repository.

**Nothing here is published.** The website source is not in this repository and not on the development workstation.
Publishing follows section 8.4 of `Documentation/26-GITHUB-MULTI-MACHINE-AND-PUBLISHING-RUNBOOK.md` (backup first, staging
directory, `manage.py check`, tests, restart, external smoke checks) and is an owner-approved step.

## What changes for a visitor

Today anyone, and any script, can fetch the appliance image by address; Zentryc does not learn who is evaluating the
product and cannot help them. With this app:

1. **Downloads page** (`/downloads/`): one clear action, *Start free trial*. The Windows agent stays an open download,
   because running deployments need it.
2. **Request form** (`/downloads/trial/`): first name, last name, work email, company, job title, country, phone, planned
   devices, hypervisor; the current monitoring tool is optional. Terms and privacy consent is required, product updates by
   email are a separate, unticked choice.
3. **Email with a personal link.** Opening it proves the address works. The link must be opened within 72 hours.
4. **Personal download page** (`/downloads/trial/d/<link>/`): the packages, checksums, verification commands and first
   setup steps. It works for 7 days and 12 downloads; resumed downloads are not counted again.
5. **Sales is told once**, when the address is confirmed, with the details and a reply-to of the requester. Every request
   and every download is in the Django admin, with a status for follow-up and a CSV export.

## What is checked

| Detail | Rule |
|---|---|
| Work email | Valid syntax; not a throw-away mailbox; not a personal mailbox (gmail.com, outlook.com ...) unless `ALLOW_FREE_EMAIL`; the domain must be able to receive mail (MX, else address record). A DNS timeout is not a refusal. |
| Email really works | Only the emailed link opens the downloads. |
| Phone | Parsed with the `phonenumbers` library for the chosen country, must be a valid number, stored as `+9665...`. Without the library only the shape is checked. |
| Company, names, job title | Present, contain letters, not a placeholder such as "n/a" or "test". |
| Country | From the ISO list; `BLOCKED_COUNTRIES` refuses the listed codes (empty by default; see *Decisions for the owner*). |
| Automation | Hidden field, a signed "form opened" time (a post within 3 seconds or without it is dropped silently), 6 requests an hour per address and 3 emails an hour per mailbox. |

Every refusal tells the person what to do instead, and the form always offers *Contact us* for people without a company
email domain.

## Layout

```
integrations/zentryc_downloads/
  README.md
  trial_downloads/            the Django app, copied to /home/net/trial_downloads/
    conf.py                   settings and their defaults (settings.TRIAL_DOWNLOADS)
    catalog.py                the releases offered: versions, files, sizes, hashes
    validators.py, lists/     email, phone and name rules; mailbox provider lists
    models.py, migrations/    TrialLead, DownloadEvent
    forms.py, access.py       the form; personal links and their lifetime
    views.py, urls.py         pages and the file hand-over to nginx
    emails.py, templates/     the link email, the sales notice, the pages
    admin.py                  follow-up list, CSV export, "send a new link"
    management/commands/purge_trial_requests.py
    tests/                    42 tests
  deploy/zentryc-appliance-downloads.nginx.conf
  testproject/, manage.py     stand-in project to run the tests here; never deployed
```

Templates extend `core/base.html` and use only classes the site stylesheet already has (`page-hero`, `card`, `grid`,
`form-group`, `btn` ...); the few additions are in `templates/trial_downloads/_styles.html`, prefixed `tdl-`. Links to
other pages are written as paths (`/contact/?topic=trial`, `/kb/zenplus/installation/`), as the present release page does,
so no URL name of the website is assumed.

## Tests

```bash
python -m venv /tmp/trial-venv && /tmp/trial-venv/bin/pip install "Django>=4.2" phonenumbers dnspython
cd integrations/zentryc_downloads && /tmp/trial-venv/bin/python manage.py test trial_downloads
```

Passing on Django 4.2 and 5.2. They cover the validation rules, the whole request, email, link and file path, link
forgery and expiry, the download limit, rate limits, the admin export and the cleanup command. The tests never use the
network: the mail-server lookup is replaced.

## Installing on the website

Confirm these on the host first; they are the only things this package could not see.

| To confirm | Where it matters |
|---|---|
| The context names `core/base.html` reads for the page title and description | `META_TITLE_KEY`, `META_DESCRIPTION_KEY` |
| The directory behind `/downloads/zenplus-appliance/` | `alias` in the nginx file |
| How the visitor address reaches Django behind Cloudflare and nginx | `IP_HEADER` (rate limits and the record) |
| The cache backend | Rate limits need a cache shared by all workers (Redis or Memcached). With a per-process cache each worker counts separately. |
| The sending address and its SPF/DKIM | `FROM_EMAIL`; a link email that lands in spam loses the trial |

Steps, after the runbook backup:

1. Copy `trial_downloads/` to `/home/net/trial_downloads/`.
2. `./zentryc_env/bin/pip install phonenumbers dnspython`.
3. Settings: add `"trial_downloads"` to `INSTALLED_APPS` and
   ```python
   TRIAL_DOWNLOADS = {
       "NOTIFY": ["sales@zentryc.com"],          # who hears about confirmed requests
       "FROM_EMAIL": "Zentryc <downloads@zentryc.com>",
       # "IP_HEADER": "HTTP_CF_CONNECTING_IP",   # only if the origin accepts Cloudflare alone
   }
   ```
4. Root URLs: `path("", include("trial_downloads.urls")),` **above** the `core` include, so `/downloads/` resolves to
   the new page. The old view and template stay in place and can be removed later.
5. `manage.py check`, `manage.py migrate trial_downloads`, and the app's tests against a disposable database.
6. nginx: include `deploy/zentryc-appliance-downloads.nginx.conf` with the two confirmed values, `nginx -t`, reload.
7. Restart `zentryc`. Schedule `manage.py purge_trial_requests` daily (cron or Celery beat).
8. Smoke checks from outside:
   - `/downloads/`, `/downloads/zenplus-appliance/1.25.2/` and `/downloads/trial/` answer 200.
   - `curl -I https://zentryc.com/downloads/zenplus-appliance/1.25.2/zenplus-appliance-1.25.2-vmware.ova` answers 302 to
     `/downloads/trial/`; the `.sha256`, `.sha256.sig` and `zentryc-release.pub` files still answer 200.
   - `/_protected/zenplus-appliance/...` answers 404 from outside.
   - A real request with a company mailbox: the email arrives, the page opens, the file downloads, resumes after an
     interruption and matches its checksum; the request and one download appear in the admin; sales receives one notice.

Rollback: remove the nginx include and the URL include, restart. The table stays; no existing table is changed.

## Publishing a new appliance release

Place the files, then change the catalogue: either `DEFAULT_CATALOG` in `catalog.py` or a JSON file of the same shape
named by `CATALOG_FILE`. Sizes and hashes must be those of the signed checksum files. A catalogue with an unsafe filename,
a short hash or a bad date is refused at load. No template names a version.

## Privacy

What is stored: the form fields, consent time and terms version, where the visitor came from, network address and browser
of the request and of each download. `purge_trial_requests` deletes requests whose email was never confirmed after 30 days
and removes network address and browser detail after 90 days. The privacy policy (last updated 5 April 2026) names name,
email and company; before launch it should also name **phone number, job title and download records**, the purpose
(delivering the download, evaluation support, sales follow-up) and the retention above.

## Decisions for the owner

- **Personal mailboxes.** Refused by default. Consultants and small firms often have no company domain; they are sent to
  the contact form. Set `ALLOW_FREE_EMAIL` to accept them.
- **Export control.** Zentryc is a United States company distributing software with encryption. `BLOCKED_COUNTRIES` is
  empty; whether to refuse embargoed destinations is a legal decision, not made here.
- **Evaluation terms.** The Terms of Service do not describe a trial or evaluation licence. The consent line says "using
  this software for evaluation"; a short evaluation clause in the terms would back it.
- **The 1.24.14 preview page** links older images directly. With the nginx rule those links lead to the form; retiring
  the page is cleaner.
- **Evaluation registration keys** are still issued by hand through the contact form. Sending one with the confirmed
  request would let every trial receive updates; that belongs to the licensing service and is not part of this package.
