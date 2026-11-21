# Entire — a beginner's guide for this repo

A practical map of [Entire](https://entire.com) for whoever picks up tonearm next,
written against what is actually set up on this box. Facts marked
**(verified)** were exercised while setting this up; the rest comes from the
docs at `docs.entire.com`.

- Project: `cloudcompile` (account-owned, `us` region)
- Repo: `/et/cloudcompile/tonearm` — private, on `aws-us-east-2.entire.io`
- Clone URL: `entire://aws-us-east-2.entire.io/et/cloudcompile/tonearm`
- Local remote name: `entire`

## Does Entire have releases, issues, and milestones?

Short answer: **no, not as separate objects.** This is the biggest mental-model
shift coming from GitHub.

| GitHub | Entire | Notes |
|---|---|---|
| Issues | **Trails, intent-only** | `entire trail create --no-branch` creates title/description with no code attached |
| Milestones | **—** | Nothing equivalent. Grouping is by project; use trail type/priority/assignee |
| Releases | **—** | Git tags exist (verified: 14 pushed). `entire.com/releases` is *Entire's own* product changelog, not your repo's |
| Pull requests | **Trails** | A trail holds changes across branches *and* repositories |
| PR review | **Agent evals + gates** | Review agents score every push; gates block the merge |
| CI | **CI plugins / mirrors** | Depot, Buildkite, Dagger, RWX (public preview); GitHub Actions still runs through a mirror |

The docs index has 265 pages and **zero** mention issues or milestones
(verified by grepping the sitemap). So:

- **Want a bug report filed?** `entire trail create --type bug --priority high --no-branch --title "..."`
- **Want a milestone?** Don't. Use a project, or trail `--type`/`--priority`/`--assignee` and filter.
- **Want release notes with binaries?** Use git tags for the version and publish
  artifacts elsewhere (GitHub Releases, or this repo's own build outputs). Entire
  stores tags but does not have a release object with attached assets.

A trail is one object that plays issue *and* PR: it has intent (title, body,
type `bug|feature|task`, priority `urgent|high|medium|low|none`, assignees,
status `open|closed`), and it can accumulate branches ("changes") with their
commits, checkpoints, agent sessions, review findings, CI results and human
approvals.

## Setup on this box (already done)

```sh
# 1. Install — stable channel, v0.13.4 (verified)
curl -fsSL https://entire.com/install.sh | bash
# installs ~/.local/bin/entire and ~/.local/bin/git-remote-entire
```

`~/.local/bin` is **not** on PATH for non-interactive shells here, so prefix
commands with `export PATH="$HOME/.local/bin:$PATH"` in scripts.

```sh
# 2. Token storage — this VPS has no OS keyring (verified: login fails without this)
mkdir -p ~/.config/entire
printf '{\n  "token_store": "file"\n}\n' > ~/.config/entire/config.json
# tokens then live at ~/.config/entire/tokens.json (0600)

# 3. Log in (device flow; approve at the printed URL)
entire login --device
entire auth status          # expect: Logged in · github:CloudCompile

# 4. Point a remote at the Entire copy
entire repo remote add entire /et/cloudcompile/tonearm
git push entire main --tags
```

Pushes are recorded by **gittuf** — the remote shows `RSL: entry #N recorded`
and refs `refs/gittuf/policy` / `refs/gittuf/reference-state-log` (verified).
That's supply-chain signing, not an error. Don't delete those refs.

## Core concepts

**Checkpoint** — the reason we're here. A commit plus the session context behind
it: prompts, transcript, tool calls, files touched, token usage. Link is made by
an `Entire-Checkpoint` git trailer added by a git hook; the context is stored in
its own git ref, so your working branch stays clean.

**Session** — one agent conversation. One session can produce several
checkpoints (several commits); one checkpoint can span several sessions.

**Trail** — intent + work. Spans multiple repos and branches. `Change` = one
repo branch inside a trail.

**Agent (eval)** — a review agent that runs on every push in a cloud sandbox and
returns a score + rationale. Entire's own repos use *Security*, *Slop*, and
*Human Review Required*. Each eval shows the current run next to the last five —
the point is looping until the scores stabilize.

**Gates** — merge requirements configured per repo under Settings → Gates:
*Up to date* (branch fresh vs base), *Human Review* (N approvals, whether the
author counts, whether a push resets them), *Findings* (unresolved agent/human
findings above a severity threshold; default `high`), *Checks* (CI). Bypasses are
recorded on the trail. (verified: `entire repo protection list` — nothing
protected yet, and protection ≠ gates.)

**Continuous Memory** — two optional plugins, not installed here (verified:
both error with "plugin is not installed"):
`entire plugin install graph` (entire-graph: symbol/relationship map, claims up
to 71% token savings) and `entire plugin install brain` (entire-brain: shared
project memory across sessions and agents).

**Dispatch** — turns recent agent work into a shareable summary, for handoffs.

## The daily loop

```sh
entire enable -y --agent opencode    # once per repo; writes .entire/settings.json
                                     # + .opencode/plugins/entire.ts
```

Then just work normally. Start the agent from the repo root, let it change files,
and `git commit`. Entire prompts `Link this commit to session context?`. To stop
being asked:

```json
// .entire/settings.local.json  (just you) or .entire/settings.json  (the team)
{ "commit_linking": "always" }
```

Work a trail:

```sh
entire trail create --title "Fix queue reorder" --type bug --priority high   # publishes branch
entire trail ready                                                          # ask for review
entire trail timeline                                                       # commits, findings, approvals
entire trail finding list
entire trail merge
```

Inspect what happened and why:

```sh
entire status
entire checkpoint list --json
entire checkpoint explain <id|sha> --full     # full session transcript
entire search "queue reorder"                 # semantic + keyword, --all-repos
entire why <file>:<line>                      # which checkpoint/prompt made this line
entire blame <file>                           # lab: lines from Entire checkpoints
```

Trails also work from inside an agent. Good prompt shape:

> Run `entire agent-help`, then inspect the Entire trail linked to this branch.
> Summarize the intended outcome and open findings. Do not merge anything.

`entire agent-help` is the source of truth for flags — it reflects the installed
binary, so prefer it over guessing.

## What is NOT set up yet

- **`entire enable` has not been run** — no git hooks, no `.entire/`, so commits
  made so far have no checkpoints. Run it before your next agent session.
- No review agents, no gates, nothing protected.
- No graph/brain plugins.
- Only `main` + tags are on Entire; local branches `fix-clients-*`,
  `v0.3-store-core`, `v0.4-*`, `rename-tonearm` are still GitHub-only.

## Gotchas specific to tonearm

- **`launch/` is never committed.** It is also *not* in `.gitignore`, so a bare
  `git add -A` will sweep your marketing work in. Stage explicitly.
- **`visualizer/` is 134 MB** (89 MB mp4 + 37 MB wav). Keep it out of git — the
  repo already had to drop 235 MB of `node_modules` in #225. If it must be
  versioned, that's Git LFS or `share`, not a plain commit.
- Big media + agent checkpoints both add git traffic; Entire's pitch is fast
  distributed hosting, but there is no reason to make the repo huge anyway.
- Mirrors are the low-risk option if GitHub must stay the source of truth:
  `entire repo mirror` keeps GitHub authoritative while Entire serves clones from
  a nearby region. This repo is Entire-native (`/et/...`), so **this** copy is
  now a separate source of truth — the two will drift. Push to both, or pick one.

## Cheat sheet

```sh
entire version | auth status | status | agent-help
entire enable -y --agent opencode       # turn on capture in this repo
entire checkpoint list|explain|search|tokens
entire search "query" [--all-repos]
entire trail create|list|show|link|ready|merge|timeline|approvals|finding
entire repo list --project et/cloudcompile
entire repo view /et/cloudcompile/tonearm
entire repo protection list|add|remove
entire project list | org list | cluster list
entire labs review|blame|why|experts|tokens|import claude-code
entire plugin install graph|brain
```
