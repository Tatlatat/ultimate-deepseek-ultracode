# Reasonix ACP ephemeral-session patch

## What it is

A one-line edit to the Reasonix CLI's compiled `dist/cli/acp-*.js` that makes each
`reasonix acp` session ephemeral (no shared history) when the environment variable
`REASONIX_ACP_EPHEMERAL_SESSION=1` is set. `bin/claude-reasonix` exports that variable
for every gateway it spawns.

Apply / revert with `patches/apply_ephemeral.py` (also run automatically by `install.sh`):

```bash
python3 patches/apply_ephemeral.py            # apply (idempotent; no-op if present)
python3 patches/apply_ephemeral.py --revert   # restore stock behavior
```

## Why it's needed

claude-reasonix runs **many concurrent `reasonix acp` lanes** (one per Workflow `agent()`
fan-out lane). Stock reasonix names each acp session by a **minute-granular timestamp**:

```js
session: `acp-${timestampSuffix()}`
```

So every lane that starts within the same wall-clock minute gets the **same session
name**. Reasonix then `loadSessionMessages` for that name on each lane — every lane loads
the **other lanes' history** into its own context. Measured impact:

- `+~10,829` input tokens per lane (history bleed),
- fan-out prompt cache stuck at **60–94%** with high variance (each lane diverges right
  after the shared prefix because its history is different).

This is invisible in unit tests (which run one lane) and only shows up under real
fan-out — which is exactly where claude-reasonix spends its tokens.

## What the patch does

It gates the session name on an env var, so an ephemeral session (`session: null`, no
shared history) is used when the launcher asks for it:

**Stock:**
```js
session: `acp-${timestampSuffix()}`
```

**Patched:**
```js
session: (process.env.REASONIX_ACP_EPHEMERAL_SESSION === "1" ? null : `acp-${timestampSuffix()}`)
```

With `REASONIX_ACP_EPHEMERAL_SESSION=1`, each lane uses an independent session, so no lane
loads another's history. Measured result: steady-state fan-out cache **99.70%** across all
lanes; a true-cold burst **97.32%** (one irreducible cold primer lane).

## Why it lives outside the repo

The patch modifies reasonix's installed `node_modules`, not this repo. A `reasonix`
upgrade (or reinstall) ships a fresh `acp-*.js` and **reverts the patch**. There is no
upstream hook to make this a config option, so the fix has to re-apply after every
reasonix change. That's why:

- `install.sh` runs the patcher on every install, and
- you should re-run `./install.sh` (or `python3 patches/apply_ephemeral.py`) after
  upgrading reasonix.

## Safety

- **Idempotent.** A file already carrying `REASONIX_ACP_EPHEMERAL_SESSION` is left
  untouched.
- **Atomic write.** The patched content is written to a temp file and `os.replace`d, so a
  crash can't leave a half-written `acp-*.js`.
- **Loud on drift.** If reasonix's internals change so the stock `session:` expression is
  gone, the patcher exits non-zero (code 2) and prints what it expected, rather than
  silently doing nothing. Update `STOCK`/`PATCHED` in `apply_ephemeral.py` to match.
- **Reversible.** `--revert` restores the exact stock expression.
