# Fork maintenance runbook

How this fork of `makeplane/plane` stays in sync with upstream while keeping
the OIDC patch on top, how images are rebuilt, and what to do when a workflow
goes red. **You only need to read the section you're in trouble with**;
everything that works is automated.

---

## 1. What's automated (the "happy path")

Two workflows in `.github/workflows/`:

| Workflow                | Runs when                                           | What it does                                                                                                                                            |
| ----------------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`sync-upstream.yml`** | Daily at 06:00 UTC + manual dispatch                | `git fetch upstream master` → `git rebase upstream/master` on `oidc-on-master` → `git push --force-with-lease`.                                         |
| **`build-images.yml`**  | On every push to `oidc-on-master` + manual dispatch | Builds `plane-backend`, `plane-frontend`, `plane-admin` → pushes to `ghcr.io/walzen-group/plane-*` with tags `latest`, `oidc-on-master`, `<short-sha>`. |

**The OIDC patch is the single commit `feat(auth): add OIDC SSO provider` sitting on top of `upstream/master`.** Rebase keeps it on top; force-push moves the branch tip; the build workflow triggers off that push.

If nothing fails, you do nothing. You'll see a green sync run every morning and (if upstream had any commits) a green build run a few minutes later.

---

## 2. Deploying a new build to prod

When you want to pick up the latest tested build:

```bash
# on the prod host, in the dir with docker-compose.prod.yml + .env
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f migrator   # wait for migrations to finish
```

Two strategies for which build:

- **`IMAGE_TAG=latest`** in `.env` (default) — auto-rolls forward to whatever was last built. Easy, but every successful sync becomes prod-live the next time you `pull`.
- **`IMAGE_TAG=<short-sha>`** in `.env` — pin to a specific build (find SHAs in **GitHub → Packages → plane-backend → tags**). Recommended for real prod — you control when changes land.

Either way, you only run `pull && up -d` when you actually want to roll.

---

## 3. When `sync-upstream` fails (the common case)

You'll get an email like _"Sync upstream master + rebase OIDC patch failed"_.

99% of the time this is a **rebase conflict** because upstream changed a file
the OIDC patch also touched. Fix locally — the workflow can't resolve
conflicts on its own.

```bash
# 1. Make sure your local branch matches the remote
git checkout oidc-on-master
git fetch origin && git reset --hard origin/oidc-on-master

# 2. Add upstream if you haven't
git remote -v   # if no 'upstream', add it:
# git remote add upstream https://github.com/makeplane/plane.git

# 3. Replay the rebase that the workflow tried
git fetch upstream master
git rebase upstream/master
```

You'll see something like:

```
CONFLICT (content): Merge conflict in apps/api/plane/authentication/adapter/base.py
error: could not apply <sha>... feat(auth): add OIDC SSO provider
```

The OIDC patch touches a known set of insertion points — most conflicts
happen in one of these:

| File                                                               | What the OIDC patch adds                                                                              | How to resolve                                                                                        |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `apps/api/plane/authentication/adapter/base.py`                    | One line: `"oidc": "ENABLE_OIDC_SYNC",` in the `provider_config_map` dict inside `check_sync_enabled` | Keep both — the new upstream entries **and** our oidc line.                                           |
| `apps/api/plane/authentication/adapter/oauth.py`                   | One branch in `authentication_error_code()` for `oidc`                                                | Keep both.                                                                                            |
| `apps/api/plane/authentication/adapter/error.py`                   | Two int constants (`OIDC_NOT_CONFIGURED`, `OIDC_OAUTH_PROVIDER_ERROR`)                                | If upstream took your numbers, renumber ours to the next free ints. Update `oidc.py` if you renumber. |
| `apps/api/plane/authentication/urls.py`                            | 4 url routes                                                                                          | Keep both.                                                                                            |
| `apps/api/plane/authentication/views/__init__.py`                  | 2 import lines                                                                                        | Keep both.                                                                                            |
| `apps/api/plane/utils/instance_config_variables/core.py`           | One `oidc_config_variables` list + one `*oidc_config_variables` in the aggregate                      | Keep both.                                                                                            |
| `apps/api/plane/license/management/commands/configure_instance.py` | `"IS_OIDC_ENABLED"` added to the `keys` list + an auto-detect `if key == "IS_OIDC_ENABLED"` block     | Keep both — be careful if upstream restructured the keys block.                                       |
| `apps/api/plane/license/api/views/instance.py`                     | Read `IS_OIDC_ENABLED` + emit `data["is_oidc_enabled"]`                                               | Keep both.                                                                                            |
| `apps/admin/hooks/oauth/index.ts`, `core.tsx`                      | One `oidc` entry + import                                                                             | Keep both.                                                                                            |
| `apps/admin/app/routes.ts`                                         | One `route("authentication/oidc", ...)` line                                                          | Keep both.                                                                                            |
| `apps/web/core/hooks/oauth/core.tsx`                               | `is_oidc_enabled` in the OR-chain + an `oidc` option in `oAuthOptions`                                | Keep both.                                                                                            |
| `packages/types/src/instance/auth.ts`, `base.ts`                   | `oidc` added to several type unions + `is_oidc_enabled` field                                         | Keep both.                                                                                            |

**Net rule of thumb**: the patch is purely additive — almost every conflict
resolves by keeping **both** sides. Real conflicts (where upstream _renamed_
or _deleted_ something we built on) need code thinking — see §3.1.

After resolving:

```bash
git add -A
git rebase --continue

# Sanity-check before pushing
python3 -m py_compile $(git diff --name-only HEAD~1 -- '*.py')
pnpm install   # if pnpm-lock changed
pnpm check:types

# Push to trigger a fresh build
git push --force-with-lease origin oidc-on-master
```

The next push automatically triggers `build-images.yml`.

### 3.1 What if upstream actually broke our integration?

If a file the OIDC patch depends on was **renamed**, **deleted**, or had its
**signature changed** (e.g. `OauthAdapter.__init__` gets new required args,
or `provider_config_map` is moved to a different file), the rebase will
still apply but the build will fail at `python manage.py check` /
`makemigrations --check` / typecheck.

In that case, treat it like a normal small refactor:

- Read the upstream commit that broke it: `git log upstream/master -- <path>`.
- Adjust our additions to fit the new shape.
- Re-amend the OIDC commit: `git add -A && git commit --amend --no-edit`.
- Force-push.

If you don't have time to fix it right away, **don't** force-push a broken
rebase. The current production `:latest` keeps working. You can:

- Disable the sync workflow temporarily: GitHub → Actions → "Sync upstream
  master + rebase OIDC patch" → ⋯ → **Disable workflow**.
- Re-enable when you're ready to fix.

### 3.2 If you ever need to abort and start over

```bash
git rebase --abort
git fetch origin
git reset --hard origin/oidc-on-master   # back to whatever's on the fork
```

Nothing on the remote changes; you're just back to a clean working tree.

### 3.3 Auto-resolution with a Claude routine (optional, opt-in)

A Claude routine running on your Claude subscription (not the paid API) is
triggered **directly by the failing `sync-upstream` workflow** via API
webhook — fires within seconds, no polling, no daily quota burn on quiet
days. The routine attempts the rebase resolution following this playbook,
opens a PR against `oidc-on-master` (never force-pushes directly), or
opens an issue if it can't resolve cleanly.

**Wiring overview:**

```
sync-upstream.yml fails  ──curl POST──▶  Claude routine webhook
                                              │
                                              ▼
                                  Claude clones, rebases, resolves
                                  conflicts, opens PR / issue
```

To enable:

1. **Install the Claude GitHub App** on `walzen-group/plane`
   (github.com/apps/claude). This authenticates the routine's `git clone` and
   `git push` (to `claude/`-prefixed branches) through Claude's GitHub proxy —
   **no PAT needed** — and is also what the optional "Auto-fix pull requests"
   toggle relies on.

2. **Create the routine in Claude**, using the **API trigger** option
   (Routines → New → trigger: API). Paste the prompt block below, make sure it's
   connected to GitHub (the App from step 1), and optionally flip on **Auto-fix
   pull requests**. **No environment variable / PAT is required** — the prompt
   clones, pushes a `claude/…` branch, and opens the PR through the built-in
   GitHub integration, not the `gh` CLI. When saved, the UI gives you a
   **webhook URL** with an embedded auth token.
   - _Only_ if you change the prompt to call the `gh` CLI directly would you need
     to `apt install gh` in a setup script and add `GH_TOKEN=github_pat_…` as an
     env var on the routine's cloud environment (a fine-grained, minimally-scoped
     PAT). Routine env vars are **not encrypted at rest** — scope it and rotate it.

3. **Store that webhook URL as a GitHub secret**:
   - Repo → **Settings → Secrets and variables → Actions → New repository
     secret**
   - Name: `CLAUDE_ROUTINE_WEBHOOK`
   - Value: the full webhook URL from step 2

That's it. The final step in `sync-upstream.yml` already POSTs to
`${CLAUDE_ROUTINE_WEBHOOK}` on failure, gated by `env.CLAUDE_ROUTINE_WEBHOOK
!= ''` — so until you set the secret it's a no-op, and once you do it just
works.

**Routine prompt (paste into the routine's prompt field):**

```
The "Sync upstream master + rebase OIDC patch" workflow on $repo failed.
Webhook payload (in $1 or your runtime's payload variable):
  {event, repo, branch, upstream_branch, run_url}

Your job: attempt the rebase resolution and open a PR. Use your built-in GitHub
integration for clone / push / PR creation — the `gh` CLI is NOT installed, so
do not call it. Push only to a claude/ branch; never to ${branch} directly.

1. Clone the repo (the GitHub proxy authenticates this):
     git clone https://github.com/${repo}.git
     cd plane && git checkout ${branch}
2. Add upstream and fetch:
     git remote add upstream https://github.com/makeplane/plane.git
     git fetch upstream ${upstream_branch}
3. Replay the rebase:
     git rebase upstream/${upstream_branch}    # expect conflicts
4. Resolve following MAINTENANCE.md §3 in that repo. Almost all conflicts
   are "keep both sides" because the OIDC patch is purely additive. Real
   restructures (renames, deletions) need code thinking — see §3.1 there.
5. Verify before committing:
     python3 -m py_compile $(git diff --name-only HEAD -- '*.py')
     pnpm install                       # only if pnpm-lock changed
     pnpm --filter @plane/types --filter web --filter admin check:types
   All three must exit 0.
6. Continue the rebase, then push to a claude/ branch (allowed by default; the
   proxy authenticates the push — no token needed):
     git add -A && git rebase --continue
     git checkout -b claude/auto-rebase-$(date +%Y%m%d-%H%M%S)
     git push origin HEAD
7. Open a pull request from that branch against ${branch} using your built-in
   GitHub tools (NOT the gh CLI), titled
     "auto: rebase OIDC patch onto upstream/master"
   with a body summarizing which files conflicted and how each was resolved,
   plus a link to ${run_url}.

If a conflict is genuinely unresolvable (upstream removed/renamed something
the patch depends on), DO NOT guess — abort and open a GitHub issue instead
(via your built-in GitHub tools), titled
   "sync-upstream rebase blocked — needs manual resolution"
with a body stating exactly what blocks the rebase, which file(s), what to look
at, and a link to ${run_url}:
     git rebase --abort

Never force-push to ${branch} directly. Only the claude/ branch + PR path.
```

**Costs**: one Claude run per failed sync. Real conflicts in this fork
are small (patch is mostly additive), so each run consumes a small slice
of your subscription quota. On days with no upstream churn or a clean
rebase, the routine simply isn't invoked.

**Trust model**: AI-resolved rebases land in a PR titled
`auto: rebase OIDC patch onto upstream/master`. The build workflow doesn't
trigger until you merge the PR, so a bad resolution can't reach prod.
Skim the diff and merge if it looks right; close the PR and resolve
manually if not.

To disable: delete (or rotate) the `CLAUDE_ROUTINE_WEBHOOK` secret — the
sync workflow skips the curl step. The routine can stay parked or be
deleted in Claude's UI.

---

## 4. When `build-images.yml` fails

Less common than rebase failures. Common causes ranked:

1. **A Dockerfile in the matrix doesn't exist on master.** Upstream moved or
   renamed `apps/{api,web,admin}/Dockerfile.*`. Open the matrix in
   `.github/workflows/build-images.yml` and update the `context`/`dockerfile`
   paths to match the new layout.
2. **Disk space on the runner.** GitHub Actions free tier has ~14 GB. If a
   build started OOM-ing on disk, re-run the failed job — usually the cache
   compresses better on retry. If it persists, drop multi-arch (we're
   already amd64 only) or build images one at a time.
3. **GHCR push 403.** Means the `GITHUB_TOKEN` doesn't have `packages: write`
   for the target image. Either:
   - Workflow permissions weren't set → **Settings → Actions → General →
     Workflow permissions → "Read and write permissions"**.
   - First push to a _new_ package name needs **Settings → Packages →
     plane-X → Package settings → Manage Actions access → add the repo with
     Write**.
4. **A test/typecheck/lint runs in the Dockerfile** and a real source bug is
   failing it. Re-run the OIDC re-test locally, fix on `oidc-on-master`,
   amend, force-push.

Find the failing step's logs in the **Actions** tab → click the red run →
the failing matrix leg. The exact error message tells you which of the
above you hit.

---

## 5. Adding more patches later

If you add a second feature on top of OIDC, the cleanest model is to keep
all your patches as commits on `oidc-on-master` (rename the branch then if
"oidc-on-master" no longer fits — the workflows reference it by name in two
places: `sync-upstream.yml` and `build-images.yml`).

```bash
# Make changes on the branch
git commit -m "feat(X): description"
git push origin oidc-on-master   # triggers a new build
```

Next nightly sync will rebase **all** your patches onto `upstream/master`.
Conflicts get a bit more involved with multiple patches (you may need to
resolve a conflict commit-by-commit), but the principle is the same.

---

## 6. Reducing email noise

You'll get an email every time a workflow fails. Options:

- **GitHub → Settings (personal, not the repo) → Notifications → Actions** →
  uncheck "Send notifications for failed workflows only for workflows
  personally triggered". This stops emails for scheduled runs (they're not
  personally triggered) — so you'll only see failures when you push or
  manually dispatch.
- Or set up a Gmail/Outlook filter that routes
  `from:notifications@github.com subject:"workflow run failed"` to a
  dedicated label and skip the inbox.
- Or **Settings → Notifications → "Watching"** → switch this repo from
  "All Activity" to "Ignore" — you'll still see failed-run badges in the
  Actions tab when you check.

Most fork maintainers settle on: keep emails on, but route them to a
"plane-ci" label so they don't bury real inbox items.

---

## 7. Emergency escape hatch

If something goes catastrophically wrong with the patch and you need to run
_pure upstream_ in prod immediately:

```bash
# In .env on the prod host:
IMAGE_TAG=...    # comment out or unset
APP_RELEASE=stable

# Swap docker-compose.prod.yml's ghcr.io/walzen-group/plane-*:${IMAGE_TAG}
# back to makeplane/plane-*:${APP_RELEASE:-stable} for the three patched
# services, then:
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

You lose OIDC login (other auth methods still work). Then fix the patch on
the branch at your leisure, test, and switch back.

---

## Reference: where things live

| Concern                    | File                                                  |
| -------------------------- | ----------------------------------------------------- |
| Nightly upstream sync      | `.github/workflows/sync-upstream.yml`                 |
| Image build & push         | `.github/workflows/build-images.yml`                  |
| OIDC patch (single commit) | top of `oidc-on-master`                               |
| Prod compose               | `docker-compose.prod.yml` + `.env.prod.example`       |
| Local dev compose          | `docker-compose-local.yml`                            |
| Upstream reference compose | `deployments/cli/community/docker-compose.yml`        |
| Published images           | `ghcr.io/walzen-group/plane-{backend,frontend,admin}` |
