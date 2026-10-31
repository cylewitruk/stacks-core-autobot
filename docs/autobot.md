# Autobot v1

An opt-in pipeline for `cylewitruk/stacks-core-autobot`. It never writes to
`stacks-network/stacks-core`. The upstream contribution policy does not accept
AI-generated code; this sandbox is not an upstream submission pipeline.

## What v1 does

1. A current repository writer enrolls a feature request by applying `autobot` to
   an issue. Intake checks actionability, risk and fit against the versioned
   project charter. Clarification, rejection and escalation stop implementation.
2. A planner proposes bounded files, steps and observable tests. Every participating
   provider/model identity reviews the **same exact design**, with a separate
   retained session per reviewer during cooperative revisions. The roster includes planner,
   implementer, intake QC, local reviewer and all configured PR reviewers;
   duplicate provider/model pairs vote once per round. Reviewers do not see peers' votes.
3. All roster members must approve cooperatively, then approve again in separate
   fresh sessions without prior votes or objection responses. One dissenter in
   either phase returns consolidated objections
   to the planner; a revision invalidates every prior vote. High risk escalates;
   rejection and missing intent stop. Manual mode adds writer approval after
   consensus and cannot waive the charter or unanimous design gate.
4. When approved implementation starts, the controller creates a branch at the
   approved base commit and links it in the issue's native **Development** section.
   The implementer proposes edits in a credential-free local workbench. Real checks
   and a fresh local review must pass before candidate Git objects are created,
   the branch advances, or a PR opens. The App's publication triggers hosted PR CI.
5. Claude and Codex independently review the published commit. Optional Copilot
   is an additional required reviewer. Implementation defects enter bounded fix
   rounds. Requirements or design defects reopen planning and require fresh
   unanimous approval before any more implementation or publication.
6. The controller waits for deterministic CI, review and protection gates.
   Autonomous merge remains off by default and is limited to documentation and
   changelogs when explicitly enabled. Rust changes always require human merge.
7. Bot-owned GitHub state retains success, failure and escalation evidence.
   Future planning reads a bounded set of relevant outcomes as untrusted evidence.
   Lessons never edit or override governance, and do not imply model training.

Agent verdicts are structured reports in the issue state, not native GitHub
approval reviews. Human review threads and required approvals remain effective.
Local execution results come from the workbench, never an agent's assertion.
Hosted CI independently checks the published PR.

## Charter, roles and reusable skills

`.autobot/charter.json` is trusted, versioned fork governance, grounded in
`CONTRIBUTING.md`: correctness, focused quality and developer/operator usability
have stable goal IDs. Stable non-goal IDs exclude autonomous consensus changes,
governance edits, speculative work and upstream AI-code submissions. Acceptance
standards require compatibility, simple scoped changes and actual validation.

Scope judgments must cite known goal IDs and explain the fit. Deterministic
validation rejects unknown IDs, missing fit, unresolved concerns and acceptance
with non-goal conflicts. High-risk judgments escalate. These checks establish
report completeness and consistency; they cannot prove semantic alignment or
prevent an incorrect unanimous model judgment.

Provider-independent roles live in `.autobot/roles.json`. Each references one
repository-local `.agents/skills/autobot-*/SKILL.md` and an external strict JSON
contract in `.autobot/schemas/`. Shared instructions occur once in
`.autobot/instructions.md`. The inference prompt contains shared governance and
only the selected stage's skill. The skill text is also reusable directly with
other providers. All six skills link to shared instructions and the charter for
standalone use; the implement skill requires exact-plan unanimous approval and
asks for missing evidence before edits. In pipeline mode, the controller supplies a
compact verified authorization attestation separately from untrusted issue/source
evidence: exact plan/input/bundle/generation binding, required identities and
successful current vote bindings. This attestation meets the implementation
approval prerequisite without requiring the model to recompute hashes. It is
withheld from independent design-review sessions. Ad hoc use does not confer
publication authority.
Python adapters contain CLI transport/parsing, rather than the workflow's prompt
prose. The controller retains deterministic permissions, validation and budgets.

The allowlisted bundle loader rejects traversal, symlinks, unapproved resources
and oversized files. It freezes/hash-binds the charter, manifests, shared
instructions, skills and schemas. Both the host and immutable inference image
must carry the same bundle. All votes bind the full plan, requirements, base,
governance, participating model identities, image IDs and design generation.
Trusted bundle or configuration drift blocks active work for operator reconciliation.
Candidates cannot edit the governance paths, contribution policy, automation or
this operations guide. Policy changes require an operator-reviewed change.
`Autobot policy` verifies bundle packaging as well as the offline lifecycle suite.

## Trust boundaries

The controller runs only trusted default-branch code on a dedicated Linux runner.
It reads candidate files through GitHub's Git database API and downloads source
archives by immutable commit for inspection. A separate execution
container extracts the pinned archive, creates local Git checkpoints, and runs
checks. Candidate code never runs directly on the controller host. Remote commits
are created through the Git database API only after local checks and review pass.

Each model runs in a fresh, non-root, read-only Docker container. It has no host
bind mounts, Docker socket, GitHub controller token, SSH agent or personal home.
A disposable named volume supplies a read-only repository snapshot at the pinned
base commit, or the published head for follow-up design and PR reviews. Local
implementation and review overlay the current workbench files on that parent,
so inspection sees the current candidate rather than stale base contents.

Planner, cooperative design reviewer and implementer sessions retain their own
CLI transcript volume across turns. Scope includes repository, issue, role,
provider/model, requirements, base, governance, image and reopen cycle; a changed
scope starts a new session. Local code reviews and final design/PR reviews remain
fresh. The entry point appends changed context through explicit CLI session IDs,
never a global "latest session" lookup. Session files are private runner data;
only scope digests, turn counters and hashed completion receipts enter public audit state. No GitHub credential
is mounted or forwarded, and repository MCP tools cannot read session storage.

Completed turns can replay their cached response after a lost transport reply.
A failed or ambiguous turn, mismatched counter or missing resumed volume starts
that role in a new isolated session with full current context. Recovery retries
once immediately, charges the same model-call budget, and preserves approval
gates. Two failures block the task with a fresh session prepared for `/autobot retry`.
Completed turn receipts are checkpointed before local checks can be interrupted.
Transcripts are limited to 64 MiB at turn admission and isolated per role. They
are retained for diagnosis; operators can list only these volumes with
`docker volume ls --filter label=stacks-autobot=session` and remove selected
inactive volumes after the issue finishes. Do not prune unrelated Docker data.
Losing this cache cannot authorize progress. Prefix-cache savings are not assumed;
resuming sessions preserves context but actual cache hits depend on the provider.

All providers receive the same bounded MCP tools: `list_files`, `read_file` and
literal `search`. Protected governance and reference files remain readable; the
`editable_files` list describes only candidate edit eligibility. Archives omit
links and Git metadata; traversal, special files and excessive expansion are
rejected. Reads reject links, binary and oversized text, paginate results and
share an 80-call/1-MB retrieval budget per session. Search reports truncation and
skipped files. Tool results are untrusted evidence, never governing instructions.

The trusted MCP server starts with an empty environment and has no execution or
write methods. Only the provider CLI receives its inference credential. Claude
built-in tools are disabled; Copilot restricts tool availability to this server;
Codex disables shell tools and web search and uses the read-only sandbox. Each CLI
starts outside the snapshot in an empty directory with explicit MCP configuration,
so candidate hooks, skills and configuration cannot be automatically loaded.
The controller prepares snapshots offline without credentials and removes only
its own disposable volume when the model exits. Candidate code still runs solely
in the separate credential-free execution workbench.

Container isolation is not a VM boundary. The container can contact its model
service and holds that provider's credential. Run it on a dedicated, disposable
host with no unrelated workloads or credentials. Do not expose the Docker API.
The controller host and its trusted workflow have access to the configured secrets.
Use runner-group workflow restrictions for this exact workflow on `refs/heads/main`
where supported. Otherwise, do not share this runner with repositories or writers
you do not trust. Labels and workflow `if` expressions are not runner access controls.

The App token is limited to this repository. It needs Contents, Issues and Pull
requests **write**, Checks and Administration **read**, plus implicit Metadata
read. Do not grant Actions, Workflows or Administration write. Do not put the App
on a protection bypass list. A GitHub App token is used because writes using
`GITHUB_TOKEN` generally do not trigger subsequent PR workflows.

All issue text, comments, source and model responses are untrusted. Plan approval,
path validation, size limits, publication, budgets and merge decisions are code,
not model instructions. Workflow/config files, hidden paths, dependency manifests,
`build.rs`, agent policy files, executables, symlinks, binary files and traversal
paths cannot be edited. A plan names at most eight files. Each replacement is at
most 128 KB, a response 256 KB, and context 350 KB. Oversized tasks stop safely.

## Credentials and model selection

Verified against official documentation on 2026-10-29:

| Provider | Supported credential in this pipeline | Model selection |
| ---- | ---- | ---- |
| Codex | `CODEX_API_KEY`; API-backed only for this public repository | `models.codex` passed as `--model` |
| Claude Code | Exactly one of `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | `models.claude` passed as `--model` |
| Copilot CLI | Separate `COPILOT_GITHUB_TOKEN`, with Copilot Requests access and no repository write permissions | `models.copilot` passed as `--model` |

OpenAI documents advanced account-backed CI, but explicitly says not to use that
workflow for public/open-source repositories and recommends API keys for
automation. This implementation never mounts or copies Codex account `auth.json`.
Claude documents `claude setup-token` for subscription OAuth use in CI; such tokens
still have plan usage and expiry limits. Copilot documents programmatic CLI use
and model selection. CLI support does not guarantee model entitlement, unlimited
quota, or compatibility with every organization's policies.

Sources: [Codex non-interactive authentication](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude authentication](https://code.claude.com/docs/en/iam),
[Claude programmatic mode](https://code.claude.com/docs/en/headless),
[Copilot programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference),
[Copilot piped input](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically).

Edit `.autobot/config.json` only on the trusted branch. Any supported provider
can fill the planner, implementer, QC or local-review role. Defaults select Claude
planning, Codex implementation/QC and Claude local review. Claude and Codex remain
mandatory PR reviewers. Add `"copilot"` to `reviewers` to require its PR approval.
`qc_model` and `local_review_model` can select separate role models; those distinct
identities also join the design roster. Copilot selection is passed through as
`--model` for all roles.

**Live inference requires an explicit nonempty model ID for every participating
role.** The configured Codex/Claude IDs target the supervised pilot; Copilot
remains unset until enabled deliberately. Confirm access for each selected model
before activation. A CLI model ID records the requested model; it does not attest
the service's actual model weights or freeze a server-side alias. Offline fake
transports can use placeholders to test policy without provider calls.

`autonomous=true` discovers writer-enrolled issues without start commands.
`autonomous=false` requires `/autobot start` and a writer's approval after the
same intake and unanimous design gates. Neither a human command nor QC can waive
path restrictions, check requirements, branch protection or merge restrictions.

## Local development loop

The model proposes bounded edits and check identifiers; the controller executes
those requests and returns diagnostics on the next turn. This gives the agent an
edit/test/debug loop without giving its credential-bearing process a shell over
candidate code. Local commits are unsigned, disposable checkpoints with no remote.
The controller publishes the validated final changes as a separate remote commit.

The execution container has no provider or GitHub credentials, host directory
mounts, Docker socket, or network during candidate checks. Source is read-only
while tests run; build output and Cargo caches use per-task volumes. Local Git
metadata is mounted only into the trusted checkpoint helper, never into tests.
Before edits, a separate credential-free `cargo fetch --locked` step uses network
access to populate the task's dependencies. Candidate commands then run offline.
Caches persist across local fixes but are not shared with other tasks or the host.

Every candidate runs `git diff --check`. Plans containing Rust also require
`cargo fmt --all -- --check`, `cargo check --workspace --locked --offline`, and
`cargo nextest run --workspace --lib --locked --offline`. The agent can request
these fixed checks by identifier; arbitrary shell commands are not accepted.
Tests needing network access or writes to source fail closed and require operator
attention. This suite does not cover every consensus or integration scenario.
Non-Rust plans offer only whitespace and formatting checks. Dependency-consuming
checks require Cargo preparation before candidate edits; unprepared requests
stop with an explicit setup error rather than entering a repair loop.

`local_reviewer` selects one fresh reviewer (Claude by default);
`local_review_model` overrides its model, including when Copilot is selected.
Failing checks or review findings return to the implementer before any publication.
The default allows two local repair rounds per PR revision, 15 minutes per check,
and 40 minutes for the local stage. The shared 48-call budget includes every local implementation and review attempt. Expensive independent PR reviews start
only after local acceptance and publication.
Timeouts, output exhaustion and killed check processes stop immediately for
operator diagnosis; they do not trigger additional model repair turns. Cold
workspace builds under the 4-CPU/8-GB container limits have not been qualified
against these time limits. Validate that capacity before enabling Rust tasks;
the supervised pilot remains documentation-only. Per-task caches are not shared
across distinct tasks to avoid importing untrusted build state.

The issue state records budgets, checkpoint/tree identities and successful check
and review evidence. GitHub's native Development link appears when approved
implementation starts, while the remote branch still contains only the base.
A failure or design reopen can leave that base-only branch linked for inspection;
the controller does not automatically delete it. Actions artifacts retain candidate edits and bounded check
output for seven days. Treat test output as untrusted data. A crashed runner may
lose unuploaded diagnostics and local checkpoints; retries start from the last
published parent with the remaining budget. Cleanup is best effort: an abruptly
killed runner can leave task volumes behind. Use a dedicated disposable host.

## Local checks

From the repository root:

```bash
python3 -m unittest discover -s tools/autobot/tests -v
python3 -m compileall -q tools/autobot
actionlint -config-file .github/actionlint.yaml \
  .github/workflows/autobot.yml .github/workflows/autobot-ci.yml
```

The lifecycle tests use fake GitHub/model transports, perform no network writes,
and consume no model quota. They cover autonomous and manual approval, trusted
enrollment, clarification, plan revisions, publication, independent review, repair,
Copilot model selection, guarded merge, restart, lost responses, tampered evidence,
failed checks, local repair/review budgets, human objections and credential isolation.
The optional Docker test runs an actual failing Rust test, commits a repair locally,
and verifies that nextest passes for the new tree; it consumes no model quota.

Build the inference image with explicit versions:

```bash
docker build \
  --build-arg CODEX_VERSION=0.163.0 \
  --build-arg CLAUDE_VERSION=2.1.301 \
  --build-arg COPILOT_VERSION=1.0.37 \
  -f tools/autobot/Dockerfile -t stacks-autobot-agent:v1 .
docker image inspect stacks-autobot-agent:v1 --format '{{.Id}}'
```

Use the returned immutable local image ID as `AUTOBOT_AGENT_IMAGE`, or publish to
your registry and use `repository@sha256:digest`. Mutable tags are rejected.
Build the separate execution image, pin its ID as `AUTOBOT_EXECUTION_IMAGE`, and
run the real Docker integration test before enabling the pipeline:

```bash
docker build -f tools/autobot/Execution.Dockerfile \
  -t stacks-autobot-execution:v1 tools/autobot
export AUTOBOT_EXECUTION_IMAGE="$(docker image inspect stacks-autobot-execution:v1 --format '{{.Id}}')"
AUTOBOT_TEST_EXECUTION_IMAGE="$AUTOBOT_EXECUTION_IMAGE" \
  python3 -m unittest discover -s tools/autobot/tests -p test_local_loop.py -v
```

Repository inspection Docker coverage can be enabled with
`AUTOBOT_TEST_AGENT_IMAGE=<immutable-image-id>` when running the Python suite.
It verifies real read-only mounts, protected-reference reads and path isolation
without provider credentials.

Rebuild and repin the inference image whenever its helper or instruction bundle changes; rebuild the execution image when its helpers change. Image changes
invalidate active work rather than silently replacing its execution environment.
For a live local tick, use a dedicated App installation token in `GH_TOKEN`, the
configured provider credentials, and `AUTOBOT_ISSUE=123`, then run:

```bash
python3 tools/autobot/controller.py
```

This command **writes to GitHub**. Do not run it concurrently with Actions; disable
`AUTOBOT_ENABLED` and wait for any existing job to finish first. Do not use a
personal owner token as the controller: checkpoint artifacts and summary comments
must belong to the configured App bot. Credentials are read from the environment,
never from committed files.

## Supervised local pilot

Keep `merge_enabled=false`, GitHub auto-merge off, and `AUTOBOT_ENABLED=false` at
repository and environment scope. Defer the Actions runner. `tools/autobot/pilot.py`
advances one explicit issue per invocation under a local lock; inspect its GitHub
state before invoking it again. A stage can contain bounded local repair turns.
The GitHub environment reviewer protects future Actions runs; local supervision
comes from the explicit invocation, not from that environment gate.

The pilot selects `gpt-6.1-sol` and `claude-sonnet-4-6`; verify entitlement with the
configured provider credentials before starting. Copilot remains optional and
needs an explicit model and its own inference-only credential when enabled.

Create a private GitHub App owned by `cylewitruk`, installed only on the selected
`stacks-core-autobot` repository. Disable webhooks and user authorization requests.
Grant Contents, Issues and Pull requests write; Checks and Administration read;
Metadata read is implicit. Do not grant workflow/actions writes or protection
bypass. Use the actual App slug plus `[bot]` as `bot_login` before publishing the
configuration. Download its private key and restrict the file to mode 600.

Store local settings outside the repository at
`~/.config/stacks-autobot-pilot/pilot.json` (directory 700, file 600): `app_id`,
`private_key_path`, `credentials_file`, `agent_image`, and `execution_image`.
Both image fields take immutable digests/IDs. These settings contain paths and
identifiers, not API keys. Dedicated provider credentials go into a separate
owner-only JSON file; the helper rejects symlinks, permissive modes, owner GitHub
tokens and ambiguous Claude authentication.

In your own terminal, generate a Claude subscription token with `claude setup-token`
and create a dedicated OpenAI API key. Then capture them without putting values
in shell arguments or history:

```bash
python3 tools/autobot/pilot.py capture-secrets
```

Use `--claude-api-key` instead to capture an Anthropic API key. Add `--copilot` only
when configuring that provider. The command requires an interactive terminal and
refuses to overwrite existing credentials; rotate into a new private filename
with `--credentials` and update `credentials_file` in settings. Never paste secrets
into issues, source files or chat. Existing CLI login state is not copied.

Review, sign and publish the pipeline/configuration to `main` using the normal
maintainer workflow. The local checkout must be clean and its HEAD must equal
remote `main` before the pilot can write. Then run:

```bash
python3 tools/autobot/pilot.py preflight
python3 tools/autobot/pilot.py advance --issue 123
```

Preflight performs read-only GitHub checks and a credential-free image check. It
requires the named-account owner wrapper for administrative reads, disabled
scheduled/automatic merging, no active Actions controller, explicit model IDs,
private credential files and a matching instruction bundle. `advance` verifies
the App slug, requests an installation token scoped to this one repository and
exact permissions, then revokes it on exit. It never uses the owner's token for
controller writes. A failed revocation leaves only GitHub's normal short expiry.
Each invocation creates a private directory beneath `~/.config/stacks-autobot-pilot`
for local check reports and `controller.json` (exit status and bounded captured
output with supplied credentials redacted). The terminal prints the outcome and
directory path, not the captured output. A private per-invocation controller
home and XDG directories keep `gh` device state out of the checkout and prevent
loading personal CLI configuration. Treat these diagnostics as private,
untrusted data; redaction is not a guarantee that arbitrary output is shareable.

Start with a small documentation-only issue, apply `autobot` as a writer, and
observe intake, design consensus, local repair/review, publication, independent
reviews and hosted CI. Leave the PR open when the controller reaches its disabled
merge gate. A separate deliberate human merge can exercise outcome reconciliation.
Do not enable schedules or autonomous merging as part of this pilot.

## Runner and GitHub setup

Start with a dedicated Linux x64 VM, Docker Engine, Python 3.11+, `gh`, and a
GitHub Actions runner labelled `autobot-controller`. Model CLIs are preinstalled
in the inference image. Prepublication Rust checks use the execution image;
independent PR CI also runs on hosted runners. Hetzner is a possible
host; provisioning and billing are intentionally outside this repository's scripts.

1. Review and land the pipeline files on `main`. Keep `AUTOBOT_ENABLED` unset.
2. Create a GitHub App with the permissions above, installed only on this repo.
   Set `bot_login` in `.autobot/config.json` to its actual `<slug>[bot]` login.
3. Create the `autobot` environment. Restrict deployment branches to `main`.
   Initially add an environment reviewer for supervised rollout; remove that
   reviewer only when you deliberately enable unattended operation.
4. Set repository/environment variables `AUTOBOT_APP_ID`, `AUTOBOT_AGENT_IMAGE`,
   and `AUTOBOT_EXECUTION_IMAGE`. Both images must use immutable digests or IDs.
5. Store secrets `AUTOBOT_APP_PRIVATE_KEY`, `AUTOBOT_CODEX_API_KEY`, and exactly one
   of `AUTOBOT_CLAUDE_OAUTH_TOKEN` / `AUTOBOT_ANTHROPIC_API_KEY`. Add
   `AUTOBOT_COPILOT_TOKEN` when enabling Copilot. Use a fine-grained token with
   Copilot Requests permission and no repository access, not your normal `gh` token.
6. Register the dedicated runner with the labels in `autobot.yml`. Restrict its
   allowed workflow where runner groups support that feature. Prefer disposable
   hosts and short-lived registration credentials. Pre-pull both pinned images.
7. Create an `autobot` issue label. A repository writer applies it to a small pilot
   issue. This enrolls the issue for automatic QC and makes dropped events recoverable.
8. Set `AUTOBOT_ENABLED=true`. Dispatch `Autobot controller` to advance an issue
   immediately, or wait for the 15-minute schedule. The controller verifies who
   applied the current label and checks their current write permission. An unknown,
   non-writer, or revoked label actor cannot authorize model calls. Public issues
   outside this queue do not consume model quota.
9. Keep `merge_enabled=false` until a supervised end-to-end pilot passes. To enable
   it, change the config **before starting a new issue**. Configure classic branch
   protection on `main`: require `Autobot policy` and `Autobot Rust`, require branches
   to be up to date, require conversation resolution, and enforce for administrators.
   Keep bypass lists empty. Ruleset-only configurations are not recognized by v1.

| Command | Effect |
| ---- | ---- |
| `/autobot start` | Manual mode only: plan a new issue, or re-plan after clarification |
| `/autobot approve <full-plan-digest>` | Manual mode only: approve exactly the attached plan and base |
| `/autobot stop` | Stop advancement while retaining branch, PR and evidence |
| `/autobot retry` | Resume a stopped/blocked stage, or request reassessment after rejection/escalation; preserve budgets |

Clarification answers belong in normal issue comments. In autonomous mode, a new
or edited answer from the issue author or a current writer automatically resumes
QC. Other commenters cannot repeatedly wake model calls. The controller snapshots
the last 20 author/writer discussion comments; slash commands and bot status are
excluded. Unchanged clarification waits, rejected/escalated tasks, and stopped
tasks consume no further model calls. Manual mode still requires `/autobot start`
after clarification. A manual restart refreshes safe pre-publication requirements
and the current base while retaining spent budgets and invalidating the old plan.
Prepared publication or existing PR ancestry requires manual reconciliation.

Before publication, changed requirements or a moving base trigger intake and
fresh design approval. A prepared publication or a base move after published work
requires manual reconciliation. After publication, authorized requirement changes
can reopen design while preserving the existing PR head. The implementer, local
reviewer and PR reviewers can also return a structured `design_issue` with concrete
evidence instead of attempting changes outside the approved plan.

A reopen archives design/vote digests and objection evidence, invalidates approval
and local publication evidence, preserves the published parent and branch, and
returns to planning. The planner and every roster member see the accumulated PR
changes. The revised plan must include all accumulated changed files; otherwise
it blocks for manual reconciliation. Fresh implementation starts from the preserved
head, updates the same PR without force-pushing, and receives new local checks and
PR reviews. A foreign/closed head always stops further work.
Discussion answers while a published design is already awaiting clarification
refresh its plan and invalidate votes without spending another reopen or revision.
Changing the issue title/body still counts as a new requirements change. The
persistent model-call ceiling applies to every clarification turn.

Default limits are two plan revisions, two design reopens, two PR fix rounds,
two local repair rounds per implementation generation, and **48 total attempted
model calls per task**. Reopens consume both the separate reopen allowance and
shared plan-revision allowance. This increased call ceiling accommodates fresh
multi-model rounds and local repairs; it does not promise convergence or enough
calls for every possible path with extra distinct role models. Calls are debited
in durable GitHub state before invocation, including failures and interrupted
retries. Stop/retry, clarification and manual restart never replenish budgets.
Each stage still has local time/check limits; repeated exhaustion escalates or
blocks rather than converging indefinitely. Even an exhausted reopen/revision
attempt first retains its defect evidence and invalidates prior approvals. No subscription quota behavior is
inferred from these controller limits.

Success, observed external merges, blocked/failure, stopped, rejected and escalated outcomes are recorded
with plan/head/bundle and evidence digests in the App-owned audit artifact. At new
task creation, retrieval reads at most ten recently updated enrolled issues, their
artifacts and the first ten comments on each. Both artifact checkpoints and legacy
comment checkpoints are supported; invalid or unavailable history is skipped.
It selects at most three outcomes with matching title terms. This bounded heuristic
can miss useful history. Relevant records reach intake/planning as
`prior_outcomes_untrusted`; contradictory suggestions cannot grant permissions,
replace the charter, or bypass scope and consensus checks. No candidate auto-edits
skills, workflows or the charter in response to a proposed lesson.

A matching externally merged published head records `observed_merged` completion
evidence, including merge SHA and actor. This observation performs no merge and
does not attest controller merge gates. It captures the normal human-merge path
for Rust PRs even when the issue closes. Wrong-head merges block, and issue closure
without a confirmed merge records a stopped outcome. Closed-issue events wake
reconciliation; the scheduled scan also checks a bounded recent closed-issue window.
Controller-initiated merges and confirmed lost-response recovery retain distinct
completion provenance. Earlier outcome/evidence records remain intact.

State version 2 adds these governance and consensus bindings. Existing version 1
comments fail closed and require operator reconciliation; there is no silent
approval migration. Preserve old state and PR evidence when replacing a task.
The controller never deletes branches or force-pushes automatically. Removing or
reapplying enrollment or revoking its actor's access also invalidates authorization.

## Native GitHub progress and audit state

The current stage is shown by one `autobot:stage:*` label. The controller creates
missing status labels and updates only that namespace, preserving the `autobot`
enrollment label and unrelated human labels. These labels are derived displays:
changing one cannot grant approval, alter budgets or skip checks. A failed label
sync leaves the durable checkpoint intact and is retried on the next invocation.
The labels provide native filtering and can be used when organizing a project
board. Projects integration requires separate access and configuration; milestones
remain available for human-defined release grouping.

The bot's editable summary shows the current reviewer roster, phase and verdicts, with
links to **Autobot implementation plan** (`plan`) and **Autobot audit state**
(`generic`) issue artifacts. It does not repeat full plan steps or the JSON
checkpoint. Stage-label failures show a temporary fallback in the comment, cleared
when synchronization recovers even on idle tasks.

Separate App-authored comments record intake, each model's review in each round,
plan publication and responses to named objections, local validation, PR reviews
and controller decisions. Each review identifies its provider/model, plan or
commit, phase and round. Bot comments do not become issue requirements or peer
review input. The controller never rewrites these events; GitHub users can still
edit or delete comments, so they are a conversation record, not approval authority.
A bounded checkpoint outbox references plans and reports already in state, with
content hashes protecting pending publications. Exact App-authored markers
reconcile lost responses without duplicate comments. Failed or conflicting
publications block work and independently update the summary and attention label;
restore an edited pending event before retrying. Published comments retain full
narrative text, while checkpoints keep bounded excerpts and publication receipts.
Plans, findings, unresolved objections and approval bindings remain intact; resolved
objections and unused votes are pruned. The total checkpoint size limit still applies.

The audit artifact is authoritative and contains exact commit, plan, image and
approval bindings, budgets and interrupted-operation intent. The plan artifact is
a generated view of that checkpoint; human edits are overwritten on the next
invocation, with the edits retained in artifact history. Request plan changes in
issue comments. Editing the view cannot authorize work or change approval bindings.
Both artifacts are public in a public repository. Updates retain GitHub artifact version history.
The [measured API response](autobot-artifact-measurements.md) includes only the latest 30 versions. The audit envelope
pins the verified plan artifact's ID and number so later edits remain repairable
when its first version is no longer returned. Older unbound plans are adopted only
with verified first-version provenance; missing proof stops execution.
The controller uses the issue artifacts REST API with the App's existing Issues
write permission; the preview API must be available to advance a task.

On the next save, an existing expanded or collapsed v2 comment checkpoint is
preserved as the audit artifact's first version before any newer state is written.
Only then is the comment replaced with links and a hidden artifact reference.
Lost responses recover the existing artifact; a stale comment cannot roll back
newer artifact state. Missing/replaced audit artifacts, duplicate bot artifacts,
foreign audit edits or conflicting versions stop execution without resetting
budgets or approvals.

Writes verify creator/editor attribution and read back the exact stored version.
Unchanged artifacts are not rewritten. Plans are verified on each load; repeated
saves of an unchanged view avoid another history read. Edits during an invocation
are repaired on the next invocation. Audit writes always recheck the current
version and confirm readback. GitHub's API provides no documented atomic
compare-and-swap here: the existing Actions concurrency group and local pilot lock
must still enforce one controller writer. Never run the two controllers concurrently.
Do not edit or delete the audit artifact or recovery reference manually.

Checkpoint failures set `autobot:attention` and update a separate App-owned health
notice, without replacing uncertain state or changing the last known stage label.
Reporting is best effort if GitHub is unavailable; repeated failures reuse the
notice. Diagnostics show a stable code and fixed controller text, never raw API or
provider errors. A successful invocation clears the label and marks the notice recovered.
Attention-label events and bot comments cannot trigger controller execution.

A linked Development branch is reserved only after current design approval, at the
approved base SHA, using GitHub's `createLinkedBranch` mutation. Intent is saved
first so a lost response can be reconciled without creating a second branch.
An unlinked same-name branch or an unexpected tip stops work. Candidate code is
published only after local checks and review; a deleted reservation or changed
base-only branch is never overwritten or silently recreated. Opening the PR links
it to the issue through the existing closing reference. Projects are not required
for this native Development link.

Issue label/edit events and author/writer comments can wake the controller.
Stage- and attention-label additions and removals are excluded so display updates
do not advance the controller.
Each invocation advances one stage or one reviewer. The schedule rotates through
labelled open issues and up to ten recently updated labelled closed issues in
issue-number order. It inspects at most `max_open_issues` candidates per run and
advances the first actionable one. A separate bot-owned queue cursor comment on
the oldest enrolled open issue records each visit before dispatch, so waiting
or failing issues do not starve later work. If no open issues remain, a closed
candidate hosts the cursor. A host change reuses that host's cursor, or starts
from the lowest issue number if it has none. Explicit issue invocations
do not read or move the cursor. Duplicate or malformed bot-owned cursors block
scanning for operator reconciliation.
GitHub may delay scheduled runs. Commands remain in issue comments; durable state
lives in issue artifacts, so runner disks and queued events are not the source of
truth. The App maintains one summary comment per issue; duplicate marked comments
stop processing. The queue cursor remains a separate bot-owned comment.

## Gates, CI and recovery

Local checks and local approval must authorize the published revision; missing,
failing or stale evidence blocks publication and merge. All configured PR
reviewers must approve the same controller-produced PR head. The
issue, configuration, approved plan, base and changed-file set must still match.
In autonomous mode, the bound unanimous design approval and current writer-backed enrollment
must also remain valid. The same design consensus gate applies in manual mode. In manual mode, the human approval must remain present
and its author must retain write access.
Required check runs must be unique, successful, complete, attached to that head,
and emitted by GitHub Actions. Skipped, neutral, missing, duplicate and stale checks
block. Unresolved threads (including outdated ones), human changes requests,
drafts, foreign forks, conflicts and non-clean mergeability block. The merge API
receives the reviewed SHA, and server-side strict branch protection guards races.
The controller never uses an administrator bypass.

`Autobot policy` runs the offline pipeline tests. `Autobot Rust` runs formatting,
workspace compilation and `cargo nextest run --locked -p stacks-common --lib`.
This is a smoke baseline, **not the full Stacks consensus/integration suite**.
Do not promote Rust changes to autonomous merge on this baseline. Existing
upstream workflows remain unchanged and retain their existing opt-in switch.

Rust CI caches registry indexes/downloads, Git dependency objects and `target`,
keyed by OS, architecture, toolchain/manifest/lockfile hashes and commit. A separate
cache stores only the pinned nextest executable, keyed by version, runner OS image,
architecture and toolchain; cache misses build it from source. Neither cache stores
credentials. PR caches remain in GitHub's PR cache
scope; privileged controller operations do not consume candidate build caches. Local
execution uses separate per-task Cargo and target volumes.

Publication intent is persisted before updating a branch. Retrying a lost response
reuses the expected commit; unexpected branch tips stop the run. Merge intent is
also persisted before the API call and reconciled against GitHub after interruption.
Provider failures consume the attempt budget. Raw provider output and errors are
suppressed because they may contain credentials. Malformed output, quota exhaustion
or a transport error stops safely and leaves a retryable state. Check provider and
runner health out of band; do not post credentials or raw transcripts to issues.

Disable `AUTOBOT_ENABLED` and stop the active job to halt the service. Rotate the
App/inference credentials if a runner is compromised. Preserve the issue state and
PR for inspection; replacing the host should not require reconstructing progress.

## Repository-specific local authentication

The checkout uses HTTPS with local credential username `cylewitruk` and
`credential.useHttpPath=true`; its exact origin URL is pinned to that username.
The global active `gh` account can remain `cylewitruk-stacks`.

For owner-authenticated administrative commands, use the local wrapper:

```bash
sh tools/autobot/gh-owner repo view cylewitruk/stacks-core-autobot
```

It retrieves the named account's token without printing it or changing global
account selection. Git's credential selection was independently verified as
`cylewitruk`, and `git push -u origin main` succeeded on 2026-10-29 with the remote
already up to date. These local credentials are not runner credentials.
