# Always-on setup (GitHub Actions + Pages)

No terminal. No laptop staying awake. A real HTTPS link that works anywhere.

**How it works:** a scheduled GitHub Action runs the scraper twice a day,
commits the reading back to the repo, and GitHub Pages serves the app reading
that committed data. You install it from Safari once and it just stays current.

---

## 1. Create the repo

On github.com: **New repository** -> name it `farewatch` -> **Public** ->
Create. (Public matters: Pages and Actions minutes are free on public repos.)

## 2. Upload the files

On the new repo page click **uploading an existing file**, then drag in
*everything* from the unzipped folder, keeping the structure:

```
collect.py
notify.py
farewatch.py
doctor.py
app.py
requirements.txt
.github/workflows/track.yml
docs/index.html
docs/app.css
docs/app.js
docs/sw.js
docs/manifest.json
docs/icon-192.png
docs/icon-512.png
```

Drag-and-drop preserves folders. If `.github` doesn't upload (browsers
sometimes hide dot-folders), create it manually: **Add file -> Create new
file**, type `.github/workflows/track.yml` as the name, paste the contents.

Commit.

## 3. Turn on Pages

**Settings -> Pages.** Source: *Deploy from a branch*. Branch: `main`,
folder: `/docs`. Save.

Your URL appears within a minute or two:
`https://<your-username>.github.io/farewatch/`

## 4. Let Actions write to the repo

**Settings -> Actions -> General -> Workflow permissions** ->
select **Read and write permissions** -> Save.

Without this the job runs but can't commit, and your data never updates.

## 5. Take the first reading

**Actions** tab -> **Track fares** -> **Run workflow**.

Watch it run. When it finishes green, `docs/data.json` exists and your site has
data. From then on it runs at ~8am and ~8pm Central automatically.

## 6. Install on your phone

Open the Pages URL in Safari -> **Share -> Add to Home Screen**.

Because it's real HTTPS, the service worker works properly here — it opens
offline showing the last reading, unlike the LAN version.

---

## If the workflow goes red

**"Permission denied" on push** -> step 4 above, workflow permissions.

**"no fares returned"** -> the most likely failure, and worth understanding:
GitHub's runners use datacenter IPs, and Google sometimes serves those a
challenge page instead of results. The job is deliberately written not to fail
the build on this — a gap in the series beats a red X every morning. If it
happens *every* run rather than occasionally, the scraper can't work from
Actions. Swapping data sources is no longer an option — Amadeus shut its free
Self-Service tier down on 17 July 2026 — so run the collector at home instead:
`install-task.ps1` registers the Windows task, and your residential IP gets
real answers where the runner gets a challenge page.

**Scraper broken after working** -> Google changed format. Pin a newer version
in the workflow's install step: `pip install fast-flights==<newer>`.

## Changing settings

Edit the `TRIPS` block at the top of `farewatch.py` directly on GitHub (pencil
icon), commit, and the next run picks it up. Target price, deadline, and the
bachelor trip toggle all live there.

---

## 7. Email alerts from the scheduled job

**Settings → Secrets and variables → Actions → New repository secret**, three
times:

| Name | Value |
|---|---|
| `FAREWATCH_TO` | sebastianrhoton@gmail.com |
| `FAREWATCH_SMTP_USER` | sebastianrhoton@gmail.com |
| `FAREWATCH_SMTP_PASS` | your 16-character Gmail app password |

Get the app password at <https://myaccount.google.com/apppasswords> (2-Step
Verification must be on). Secrets are write-only once saved and are not exposed
to forks.

Without these the workflow still runs and still records fares — it just skips
the email and says so in the log. `docs/notified.json` is what stops it
re-announcing a fare it already told you about; the workflow commits it back
alongside the history, so don't delete it.

**Never put the app password in `farewatch.env` in a public repo.**
`.gitignore` already excludes that file — keep it that way.
