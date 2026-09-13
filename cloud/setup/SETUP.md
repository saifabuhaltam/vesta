# Setting up Vesta in the cloud

Written for the machine you actually have. You do not have Node, npm or Homebrew,
and you do not need them: everything below is done in a web browser, plus two
`python3` commands that use the Python already on macOS.

Budget: $0 CAD. Nothing here asks for a card except Cloudflare, which asks for one on
signup but does not charge for anything Vesta uses.

Time: about 40 minutes, most of it waiting for things to provision.

After each stage run this to check your work:

```
cd "/Users/saifabuhaltam/Desktop/ARIA AI ASSISTANT/projects/vesta"
python3 cloud/setup/verify.py
```

It tells you what is working and what is not, in plain language.

---

## The short version

Nothing auto-connects. You wire the three services by copying four values between
them:

```
Supabase URL + anon key  ──►  config.js   and  Worker secrets
Supabase service_role key ─►  Worker secrets only  (never config.js)
Worker URL               ──►  config.js
Pages URL                ──►  Worker ALLOWED_ORIGIN
```

1. Terminal: `cat cloud/setup/all_in_one.sql | pbcopy` (this puts the SQL on your
   clipboard) → Supabase → SQL Editor → Cmd+V → Run
2. Supabase → Project Settings → API → copy the **Project URL** and the **anon** key
3. Terminal: `cp cloud/app/config.example.js cloud/app/config.js` then paste those two in
4. Cloudflare → sign up → **R2** → Create bucket → name it `vesta-files`
5. Cloudflare → **Workers** → Create → Hello World → Deploy → name it `vesta-api`
6. Edit code → delete it all → paste `cloud/worker/src/index.js` → Deploy
7. Worker → Settings → Bindings → R2 bucket → variable `FILES`, bucket `vesta-files`
8. Worker → Settings → Variables → add the 5 secrets and 2 text vars (stage 3.5 below)
9. Worker → Settings → Trigger Events → Cron → `23 5 * * *`
10. Copy the Worker URL into `workerUrl` in `config.js`
11. Terminal: `python3 cloud/build.py`
12. Cloudflare → **Pages** → Upload assets → drag everything inside `cloud/dist` → Deploy
13. Copy the Pages URL into the Worker's `ALLOWED_ORIGIN` → Deploy the Worker again
14. Open the Pages URL and sign in
15. Terminal: run the export in stage 5, paste the result into the SQL editor

Check yourself at any point with `python3 cloud/setup/verify.py`.

**Do not connect GitHub to Cloudflare Pages.** `config.js` is gitignored, so a
build from your repo would deploy with no credentials and the app would load with
nothing to connect to. Upload the folder directly instead.

---

## Stage 1 — Supabase, the database and logins

**1.1** Go to https://supabase.com and sign up. Signing up with GitHub or Google is
fine and is the fastest route.

**1.2** Click **New project**.

- Name: `vesta`
- Database password: click **Generate a password** and then **Copy**. Paste it
  somewhere safe now. You will almost certainly never need it, but it cannot be
  shown again.
- Region: pick the one nearest you. **West US (North California)** is the closest
  to Vancouver.
- Plan: **Free**.

If you are shown three **Data API** switches, set them like this:

| Switch | Set it to | Why |
|---|---|---|
| Enable Data API | **On** | This is how the app reaches the database. Nothing works without it. |
| Automatically expose new tables | **Either** | Vesta's SQL grants every role explicitly, so this changes nothing for you. Off is Supabase's own recommendation and is slightly tighter. |
| Enable automatic RLS | **On** | A safety net. Vesta already turns Row Level Security on for every table it creates; this makes sure any table added later gets it too, instead of being readable by everyone until somebody notices. |

Click **Create new project** and wait. It takes two or three minutes.

**1.3** When it is ready, click **SQL Editor** in the left sidebar, then
**New query**.

**1.4** Put the SQL on your clipboard. In Terminal:

```
cd "/Users/saifabuhaltam/Desktop/ARIA AI ASSISTANT/projects/vesta"
cat cloud/setup/all_in_one.sql | pbcopy
```

Now click into the Supabase query box, press **Cmd+V**, and click **Run**. You are
pasting 815 lines of SQL, not a filename: the box should fill with text starting
`-- Vesta: complete database setup`.

It should finish in a few seconds and print a table listing every Vesta table with
`rls_enabled` set to `true`. That list is the confirmation that it worked. Your own
email is already in the file, so you are on the invite list.

> If you see a notice mentioning `pg_cron`, that is fine and expected on some
> projects. Go to **Database → Extensions**, search `pg_cron`, switch it on, then
> run the file again. Without it the 30-day Trash never empties, which is not
> urgent.

**1.5** Go to **Project Settings** (the gear at the bottom left) → **API**. You need
two values from this page:

- **Project URL**, which looks like `https://abcdefgh.supabase.co`
- **Project API keys → `anon` `public`**, a long string starting `eyJ...`

Do not touch the `service_role` key yet, and never put it in the app.

**1.6** On your Mac, make your config file:

```
cd "/Users/saifabuhaltam/Desktop/ARIA AI ASSISTANT/projects/vesta"
cp cloud/app/config.example.js cloud/app/config.js
open -e cloud/app/config.js
```

Paste the Project URL into `supabaseUrl` and the anon key into `supabaseAnonKey`.
Leave `workerUrl` alone for now. Save and close.

**Check your work:** `python3 cloud/setup/verify.py`

You want to see "The project is reachable" and "Signed-out visitors are refused".
That second one means Row Level Security is on and strangers cannot read your data.

---

## Stage 2 — Google sign-in

Free. Skip this if you would rather just use email and password; that already works.

**2.1** In Supabase go to **Authentication → Sign In / Providers → Google**. Switch
it on. Leave the page open: it shows a **Callback URL** you need in a moment.

**2.2** In another tab open https://console.cloud.google.com and sign in.

**2.3** Create a project: click the project dropdown at the top, **New Project**,
name it `Vesta`, **Create**. Wait for it, then make sure it is selected.

**2.4** Go to **APIs & Services → OAuth consent screen**.

- User type: **External**, then **Create**
- App name: `Vesta`
- User support email: your email
- Developer contact email: your email
- Save and continue through the remaining steps, then **Back to dashboard**
- Under **Test users**, click **Add users** and add your own email plus any friends

Leaving it in Testing mode is correct. You do not need Google to review anything,
because only the test users you list can sign in.

**2.5** Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**.

- Application type: **Web application**
- Name: `Vesta web`
- Under **Authorised redirect URIs**, click **Add URI** and paste the Callback URL
  from the Supabase tab. It looks like
  `https://abcdefgh.supabase.co/auth/v1/callback`
- **Create**

**2.6** Google shows a **Client ID** and **Client secret**. Paste both back into the
Supabase Google provider page and click **Save**.

---

## Stage 3 — Cloudflare, files and the AI key

**3.1** Go to https://dash.cloudflare.com and sign up. It asks for a card during
signup. Nothing Vesta uses leaves the free tier.

**3.2 — Make the bucket.** In the left sidebar go to **R2 Object Storage**, click
**Create bucket**, name it exactly `vesta-files`, leave the location automatic, and
create it. You do not need to configure CORS: the browser never talks to the bucket
directly, only to the Worker.

**3.3 — Make the Worker.** In the sidebar go to **Compute (Workers)** →
**Create** → **Start with Hello World** → **Deploy**. Name it `vesta-api`.

Once it deploys, click **Edit code**. Delete everything in the editor.

Put the Worker code on your clipboard. In Terminal:

```
cat cloud/worker/src/index.js | pbcopy
```

Click into the Cloudflare editor, press **Cmd+V**, and click **Deploy**.

**3.4 — Connect the bucket to the Worker.** Go to the Worker's **Settings** →
**Bindings** → **Add** → **R2 bucket**.

- Variable name: `FILES` (exactly, in capitals)
- R2 bucket: `vesta-files`

Save.

**3.5 — Add the secrets.** Still in **Settings**, find **Variables and Secrets**.
Add each of these as type **Secret**:

| Name | Value |
|---|---|
| `SUPABASE_URL` | your Project URL from step 1.5 |
| `SUPABASE_ANON_KEY` | your anon key from step 1.5 |
| `SUPABASE_SERVICE_KEY` | Supabase → Project Settings → API → `service_role`. Click reveal to copy it. |
| `ANTHROPIC_API_KEY` | from https://console.anthropic.com. Skip if you are not using Headstart yet. |
| `UPLOAD_TICKET_SECRET` | already generated for you: see `cloud/setup/secrets.local.txt` |

Then add these two as type **Text**, not Secret:

| Name | Value |
|---|---|
| `ALLOWED_ORIGIN` | `http://localhost:5055` for now. You will add the real address in stage 4. |
| `ANTHROPIC_MODEL` | `claude-opus-5` |

The `service_role` key is the one that matters. It bypasses every security policy,
so it belongs here and nowhere else. Never put it in `config.js`.

Click **Deploy** after saving the variables.

**3.6 — Schedule the cleanup.** **Settings** → **Trigger Events** → **Add** →
**Cron Trigger** → `23 5 * * *`. This deletes files from R2 once their 30 days in
the Trash are up. Without it nothing breaks, you just keep paying storage on
deleted files.

**3.7** Copy your Worker's URL from the Worker overview page. It looks like
`https://vesta-api.your-name.workers.dev`. Put it into `workerUrl` in
`cloud/app/config.js` and save.

**Check your work:** `python3 cloud/setup/verify.py`

You want "The Worker is deployed and answering", "Uploads require a signed-in user"
and "Forged file links are refused".

---

## Stage 4 — Put the app online

**4.1** Build it:

```
cd "/Users/saifabuhaltam/Desktop/ARIA AI ASSISTANT/projects/vesta"
python3 cloud/build.py
```

That writes `cloud/dist`. It never touches your local app, which keeps working.

**4.2** In Cloudflare go to **Compute (Workers)** → **Pages** tab →
**Create application** → **Upload assets**. Name the project `vesta`.

Open `cloud/dist` in Finder:

```
open cloud/dist
```

Select all the files inside it (not the folder itself) and drag them onto the
upload area. Click **Deploy site**.

**4.3** Cloudflare gives you an address like `https://vesta-a1b.pages.dev`. Copy it.

**4.4** Go back to the Worker's **Settings → Variables and Secrets** and edit
`ALLOWED_ORIGIN` to include it, comma separated:

```
http://localhost:5055,https://vesta-a1b.pages.dev
```

Deploy the Worker again. **If you skip this, uploads will fail** and the browser
console will say the request was blocked by CORS.

**4.5** Open your Pages address. You should see the Vesta sign-in card. Sign in
with Google, or create an account with the email that is on the invite list.

---

## Stage 5 — Bring your existing data across

Do this only after you have signed in at least once, because the import attaches
everything to your account and your account has to exist first.

```
cd "/Users/saifabuhaltam/Desktop/ARIA AI ASSISTANT/projects/vesta"
python3 cloud/migrate/export_sqlite.py --email saifabuhaltam@gmail.com > cloud/migrate/import.sql
```

It reads your local database read-only and cannot damage it. It will report what it
found: 3 classes, 2 meeting times, 7 assignments, 3 notes, 1 file.

Open `cloud/migrate/import.sql`, copy all of it, paste it into the Supabase SQL
editor, and Run. Reload Vesta and your classes will be there.

Your one uploaded file is the exception. Its bytes are on your laptop, so its row
comes across marked as an unfinished upload. Re-upload that file through the app
and it will be stored properly in R2.

---

## Adding a friend

Two lines. In the Supabase SQL editor:

```sql
insert into allowed_emails (email, note) values ('their@email.com', 'Joe');
```

If they are using Google sign-in, also add them under **Test users** on the Google
OAuth consent screen. Then send them the Pages address. They see only their own
data; the database enforces that, not the app.

---

## When something is wrong

Run `python3 cloud/setup/verify.py` first. It checks each piece separately and
tells you which one is broken.

**"That email has not been invited to Vesta."** The address you signed in with is
not in `allowed_emails`. Google accounts often use a different address than you
expect. Check with `select * from allowed_emails;` in the SQL editor.

**Uploads fail, console mentions CORS.** `ALLOWED_ORIGIN` on the Worker does not
include the address you are visiting. Step 4.4.

**The app loads but shows nothing and the sidebar says it cannot reach your
account.** Usually the anon key is wrong or the SQL was never run. The verifier
distinguishes the two.

**Everything worked, then stopped after a few weeks off.** A free Supabase project
pauses after seven days of no activity. Open the Supabase dashboard and click
Restore. Nothing is lost.

---

## Outstanding: prove two accounts are isolated on the live project

Everything else in the multi-user foundation is built and tested. This one check
needs two things only the account owner can do, so it is written down here rather
than left in a chat log.

**Why it cannot be automated.** Adding an email to `allowed_emails` needs either the
service key, which lives only in the Worker's secrets and cannot be read back, or a
Supabase dashboard login. And this project has email confirmation switched on
(`mailer_autoconfirm` is false), so a new account is unusable until someone clicks
the link that arrives in the owner's inbox. Both are deliberate protections.

**Step 1.** In the Supabase SQL editor:

```bash
cat cloud/setup/invite_test_accounts.sql | pbcopy
```

Paste and Run. It adds two plus-addresses of your own Gmail to the invite list.

**Step 2.** Run the check. It creates both accounts, then waits five minutes for you
to click the two confirmation emails, then continues on its own:

```bash
python3 cloud/setup/test_two_accounts.py \
  saifabuhaltam+vtest1@gmail.com TestPass123 \
  saifabuhaltam+vtest2@gmail.com TestPass456 --wait
```

It creates one class as A and checks that B cannot see it, fetch it by id, rename
it, delete it, or create a row owned by someone else, then deletes the class.

**Step 3.** Remove the two test users from Supabase → Authentication → Users, and
delete their rows from `allowed_emails`.

**What already passes.** The same script, unmodified, passes all 14 checks against a
PostgreSQL running this exact `all_in_one.sql`, served over HTTP with every request
executed as the `authenticated` role carrying the caller's token claim, which is how
PostgREST runs a browser query. So the policies and the assertions are both proven.
The live run exists to confirm the deployed project matches, and nothing more.

## Outstanding: bring the cloud schema up to the local app

The local app gained two data-model changes after the cloud migrations were written.
The cloud schema needs matching migrations before it can go live, or these features
will fail there:

- **Inbox.** `notes.class_id` and `files.class_id` must be nullable, so a note or a file
  can exist before it is filed under a class. RLS already keys on `user_id`, so no
  policy change should be needed, but the class-scoped indexes and any joins that
  assume a class must tolerate `null`.
- **One file, many assignments.** Local uses an `item_files (item_id, material_id)` link
  table instead of a single assignment per file. The cloud needs the equivalent table
  with `user_id`, `force row level security`, USING and WITH CHECK on `user_id =
  auth.uid()`, a unique `(item_id, file_id)`, and `PUBLIC` revoked like the others.
- **Search and suggestions** run in `links.py` on Flask. In the cloud they would need
  either Postgres full-text search behind an RPC or a Worker endpoint; neither exists yet.
