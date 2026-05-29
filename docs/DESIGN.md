# configme — design specification

`configme` is a centrally-installed Python package (installed like
[`runme`](https://github.com/fesmc/runme)) that configures the build of the
yelmox / climber-x model stacks and their component packages from a **single
source of machine- and compiler-specific truth**.

It replaces, and over time deprecates, two pieces of friction that exist today:

- the per-repository `config.py` + machine/compiler config files, which must be
  re-specified for every package individually; and
- the orchestrator-specific `install.py` (currently only in yelmox), whose
  clone / configure / link / runme-setup logic is bespoke and not reusable for
  climber-x.

This document is the agreed design. It is the input to the issue breakdown
(`docs/` → GitHub issues) and the reference for implementation.

---

## 1. Goals and non-goals

**Goals**

- One place to define a machine (netCDF location, CPU flags, link extras) and a
  compiler (FC, FFLAGS, DFLAGS). Every package on that machine is configured
  with the *same* information.
- A single command to clone, configure, and link a whole stack
  (`configme install yelmox`) or to (re)configure one package
  (`configme config yelmo`) or all of them (`configme`).
- Auto-detect the netCDF installation (`nf-config` / `nc-config`) so users no
  longer have to set `NC_FROOT` / `NC_CROOT` in `.bashrc` / `.zshrc`.
- Support multiple **orchestrators** (yelmox, climber-x) that compile different
  sets of component packages.
- Robust and failsafe behaviour; clear, extensible, data-driven methods.

**Non-goals**

- configme does **not** compile (`make`) the orchestrator or template packages.
  Compilation stays an explicit user step (`make yelmox`).
- configme does **not** depend on or call any package's `config.py` /
  `install.py`. Those are deprecated.
- configme does not own a package's *dependency wiring* (how it links its own
  sub-libraries). That stays in the repo.

---

## 2. Core architecture

The Makefile of each package is assembled from three fragments:

```
  compiler.mk      (central — owned by configme)   FC, FFLAGS, DFLAGS, OpenMP flag
  machine.mk       (central — owned by configme)   CPU flags, link extras, netCDF override
  common.mk        (repo    — owned by the package) dependency roots, INC_/LIB_, LFLAGS, toggles
```

inserted into the package's existing `config/Makefile` template at the
`<COMPILER_CONFIGURATION>` placeholder, in the order **compiler → machine →
common** (later fragments may override earlier variables; `common.mk`
references variables the other two set).

This is exactly the two-axis layout yelmox already uses; configme centralises
the compiler/machine axes and leaves `common.mk` with each repo.

- **netCDF (`INC_NC` / `LIB_NC`)** is normally *not* stored in a machine
  fragment — it is detected fresh (§5) and baked in as literal values.
- **Externals not managed by configme** (e.g. PETSc): their root comes from a
  central machine fragment or from the shell environment.
- **Managed packages**: their root is assumed to live inside the orchestrator
  directory.

### Transition / legacy support

The per-repo `config.py` continues to work during the transition. As each repo
is onboarded its embedded compiler/machine content becomes vestigial and may be
deleted once configme is trusted as the sole tool.

---

## 3. Orchestrators and the manifest

There are two orchestrators today — **yelmox** and **climber-x** — each
compiling a different set of component packages.

Each checkout carries a `.configme/manifest.toml` that names what it is:
`package = "<primary>"` (an orchestrator or a package, resolved by name) plus
`deps = [...]`, the packages it pulls in. This makes a checkout
**self-describing independent of its directory name** — an install into a
differently-named directory (e.g. `--dir check`) is still recognised. configme
validates that it supports the named package and every dep. The manifest names
packages only; it does **not** describe per-package needs (org, link
dependencies, config style) — configme centralises those (§10).

The manifest is **portable** — `package` + `deps` only, no machine/compiler or
install choices (those live in `config.toml`) — so it may be committed into a
package repo. `configme install`/`configme init` therefore **create it only if
missing**: a committed manifest is left untouched. They write a
`.configme/.gitignore` covering `config.toml` (install-local) but not
`manifest.toml`.

**Manifest precedence:** local `.configme/manifest.toml` (if present) wins;
otherwise configme falls back to directory-name matching, then its own shipped
seed manifest for that orchestrator.

**Component ref pins.** A `deps` entry may pin a git ref with the same
`name:ref` syntax the orchestrator uses (e.g. `"yelmo:climber-x"`). This lets a
checkout — and the package owner editing it — declare the exact ref to build,
including pointing a component at a development branch. Resolution precedence:
the manifest pin **wins over** the orchestrator's shipped default
(`component_refs`); a bare name carries no pin and falls back to that default
(so removing a pin and re-running reverts to the canonical branch). The
orchestrator default therefore only governs when no manifest ref is written.

configme **reconciles** each checkout to its resolved ref before that package's
Makefile is (re)generated — the Makefile template lives inside the checkout and
can differ between refs, so the ref must be correct first. `install` switches a
freshly cloned / just-present tree automatically and marks any non-`main` ref in
its summary (e.g. `yelmo@climber-x`); `config` and `upgrade`, which act on an
existing working copy with a possibly hand-edited manifest, **prompt** before
switching a clean checkout (`switch <pkg> from <cur> to <ref>?`, default yes). A
checkout with uncommitted tracked changes is never clobbered — the switch is
skipped and reported.

---

## 4. Command surface

```
configme install <target> [options]   # clone/use-existing + configure + link + build
configme update [options]              # self-update: pip install -U configme
configme upgrade [<target>] [options] # git pull existing checkouts + reconfigure
configme config [<target>] [options]  # config-only: (re)generate Makefile(s)
configme status [<target>] [options]  # read-only: what is present / still pending
configme [-m M] [-c C]                # alias for `configme config` on the current dir
configme show <name>                   # print a machine/compiler fragment to stdout
configme new machine|compiler <name>   # scaffold a fragment from an existing one
configme netcdf                        # detect & print NC_FROOT / NC_CROOT
configme init                          # scaffold/validate a .configme/ folder
configme list                          # supported packages / machines / compilers
```

`install` and `config` share **one** target grammar (`build_plan`) and **one**
Makefile-generation path (`configure_makefile`); `config` is exactly the
`install` configure step without the surrounding clone / link / extras / build.
So any `<target>` valid for one is valid for the other, and `--only` behaves
identically for both.

### `configme install`

```
configme install yelmox                # orchestrator + its full default set
configme install yelmo                 # one package + the sub-packages it needs (auto-resolve)
configme install yelmox+yelmo          # exactly those two, literal, no auto-resolve
```

Options (mirroring today's `install.py`):

- `-d, --download {ssh|https|no}` — how to obtain repos (default `ssh`):
  - `ssh` — clone over SSH (`git@github.com:...`); needs a GitHub SSH key.
  - `https` — clone over HTTPS (`https://github.com/...`); no SSH key needed.
  - `no` — don't clone; configure whatever checkout is already on disk. A
    directory only counts as a checkout if it holds the primary's Makefile
    template (under `config/` or `.configme/`); against an empty or partial
    directory, `-d no` fails fast rather than writing a misleading
    `.configme/` stub.
- `--dir DIR` — where to install (default `./<orchestrator>`, e.g. `./yelmox`).
- `-m, --machine NAME`, `-c, --compiler NAME` — selection (see §6).
- `--overwrite` — re-clone, moving existing copies aside.
- `--build-deps` — also run `build.py`-style package builds (e.g. fesm-utils);
  off by default (§9).
- `--only` — install only the named target: no orchestrator expansion and no
  dependency resolution (the single-target equivalent of a `+`-list).
- `--dry-run` — print the full plan (and the `.install.sh` it would write)
  without executing (§12).

Selection semantics:

- A **single name** auto-resolves and installs the sub-packages it needs.
- A **`+`-separated list** is literal: exactly those packages, no
  auto-resolution. (Missing dependencies are handled per §12d.)
- An **orchestrator name** pulls its full default set.
- **`--only`** collapses any of the above to exactly the named target.

`configme install` may be run from anywhere (uses `--dir`) or from *inside* an
already-cloned orchestrator/package, in which case it skips cloning that root
and performs the remaining steps.

### `configme config [<target>]` (config-only)

Regenerates Makefile(s) from template + central machine/compiler + repo
`common.mk`. No clone, no link, no extras, no build — it is the `install`
configure step in isolation, over the same `build_plan` target set. So
`configme config yelmox` expands to the orchestrator + its subpackages,
`configme config yelmox+yelmo` is the literal two-package list, and
`configme config fesm-utils` configures its `fesm-utils/utils` subpackage's
Makefile (the autotools build of fesm-utils itself, and the utils compile, are
`install`-only steps, noted but not run). `--only` and `--dry-run` work exactly
as for `install`.

With **no target** it reflects the current directory: inside an orchestrator it
configures that orchestrator + subpackages, inside a single package's directory
it configures that package (+ the deps it needs, unless `--only`). Naming
packages without the `config` verb (`configme yelmo`) is rejected with a hint,
to keep it distinct from `configme install yelmo`; bare `configme` is an alias
for `configme config` on the current directory.

### `configme status [<target>]` (read-only inspection)

Where `install`/`upgrade` report what a run just *did* (an action log of that
invocation), `status` answers what is true on disk *now* — a property of the
checkout, not of any run. It reconstructs the same `build_plan` and probes the
disk, then reports per-component state across four categories:

- **repo** — each cloned component is a real git checkout (`.git` present).
  Optional components (e.g. climber-x's `bgc`/`vilma`) that are absent are
  `pending`, not `missing`.
- **link** — each inter-component build symlink resolves (`ok` / `broken` for a
  dangling link / `missing` when the dep is on disk but unlinked / `pending`
  when the dep itself is absent), reusing `install`'s link resolution.
- **build** — each declared `[package.artifacts]` file exists, per variant. All
  present is `ok`, none is `pending` (not built — possibly deferred on purpose),
  some is `partial` (a genuinely half-finished build).
- **extra** — each orchestrator extra is present: a `git_repo` (e.g. the
  climber-x `input` data repo), a `data_link`, a `runme_config`. `pip_package`
  is skipped (an installed pip package cannot be reliably probed from disk).

The probe is **pure** — no clone, configure, link, build, prompt, or config
write — and is driven entirely off the registry metadata that already drives
`install`, so a new package/orchestrator/extra is covered with no extra code.
By default only not-ok rows are shown (a fully-ok category collapses to a count;
`-v` shows everything), and the report ends with the exact commands to run by
hand for whatever is outstanding. Exit status is non-zero only for a hard
problem (`missing`/`broken`/`partial`); `pending` (deferred) items do not fail.

The same inspection (`status.pending_block`) is appended to the `install` and
`config` summaries, so a routine run also surfaces pre-existing gaps — a skipped
optional/data repo, an unmade link, a deferred build — without a separate
command. The build-completeness probe relies on the per-package, variant-keyed
`[package.artifacts]` table in the registry (§9), which lists the library files
a finished build produces (paths relative to the package's checkout); it is
build-style-agnostic, working the same for a `build.py` package (`fesm-utils`'s
LIS + FFTW) and a `make` package (`fesm-utils/utils`'s `libfesmutils`).

`install` reuses the same probe before each build step: a package whose
artifacts are all present is reported as already built and its rebuild prompt
defaults to **no** (declining keeps the existing build rather than marking it
deferred), so a re-run on a finished checkout neither prompts unnecessarily nor
mislabels a built package as pending. `--build-deps` still forces a rebuild, and
`upgrade` ignores the existing artifacts (it only builds after a pull that
advanced HEAD, so they are stale by definition).

---

## 5. netCDF detection

- **Fresh detection on every run** via `nf-config` / `nc-config`, using all
  useful information they expose (`--fflags`, `--flibs`, `--cflags`, `--libs`,
  `--static`, plus `--prefix` for the `NC_FROOT` / `NC_CROOT` roots).
- Detected `INC_NC` / `LIB_NC` are baked into the generated Makefile as
  **literal** values — zero environment dependency at build time.
- `configme netcdf` prints `NC_FROOT` / `NC_CROOT` when invoked directly;
  otherwise detection is used internally during Makefile generation.

**Precedence:**

1. explicit machine-fragment netCDF override (deliberate hard-pin, e.g. a quirky
   HPC) —
2. `nf-config` / `nc-config` detection —
3. `NC_FROOT` / `NC_CROOT` from the environment or parsed from `.bashrc` /
   `.zshrc`.

If all three fail, configme errors with an actionable message (load the netCDF
module, or set `NC_FROOT` / `NC_CROOT`) rather than emitting a broken Makefile.

---

## 6. Registry and selection

### Fragment lookup (most-specific wins)

1. **Orchestrator** `.configme/{machines,compilers}/*.mk` — project-local;
   ad-hoc new definitions land here and apply to *all* the orchestrator's
   components.
2. **User** `~/.configme/{machines,compilers}/*.mk` — per-user across projects.
3. **Shipped** fragments inside the installed configme package — curated base.

→ **orchestrator > user > shipped.**

### Machine + compiler selection (most-specific wins)

1. explicit `-m` / `-c` flag
2. orchestrator `.configme/config.toml` (`machine`, `compiler`)
3. user-global `~/.configme/config.toml`
4. hostname auto-detection (configme ships a `hostname-pattern → machine` map)
5. per-machine default compiler (configme ships a `machine → compiler` table in
   `data/compiler_defaults.toml`: HPC environments default to `ifx`, personal
   Linux/macOS to `gfortran`) — proposed, never silently forced
6. interactive prompt (TTY only)

When a machine is known (steps 1–4) and a default compiler exists for it
(step 5), configme **proposes** the complete pair and asks `Keep this
configuration? [Y/n]`; accepting skips the prompt, declining picks both by
hand. With no proposable pair, the interactive prompt is a **single combined
question** — type `<machine> <compiler>` as two words; the machine list
includes `<new_machine>` to hint that a not-yet-supported name can be typed (it
triggers the escape valve below). On a non-interactive (no-TTY) run a complete
proposed pair is used silently; an incomplete one errors with an actionable
hint to pass `-m`/`-c`.

The selected machine + compiler are recorded in the orchestrator's
`.configme/config.toml` so every subsequent `configme <pkg>` inside that
orchestrator applies the **same** pair to all components — this is what removes
the per-package repetition that `config.py` forces today.

### Authoring fragments: `show`, `new`, and the new-machine escape valve

A user on a not-yet-supported machine needs their own fragment. Two helpers:

- **`configme show <name>`** prints a resolved machine/compiler fragment to
  stdout (auto-detecting the kind; `--machine`/`--compiler` to force) with an
  origin header, so it can be read or copied into a local `.configme/`.
- **`configme new machine <name>`** (and `new compiler`) scaffolds `<name>.mk`
  seeded from an existing fragment (`--from`, default `linux` for machines,
  `gfortran` for compilers). It writes the **project tier** (the primary copy,
  intended for eventual upstreaming into configme) *and* the **user tier** (a
  durable backup that survives deleting the project), then leaves it for the
  user to edit. Refuses to overwrite without `--force`.

**Escape valve:** if the interactive prompt receives an *unknown machine* name,
configme offers to scaffold it from `linux` on the spot (project + user tier,
default yes) and proceeds with it. An unknown *compiler* simply re-prompts with
a hint to `configme new compiler` — the compiler set is small and fixed, so no
auto-creation. During a fresh `install` (before the orchestrator is cloned)
there is no project yet, so the escape valve writes only the user tier.

### Contribute-back nudge

When configme resolves a machine/compiler fragment that exists only locally
(orchestrator or user tier) and is absent from the shipped registry, it prints
an **advisory-only** nudge ("machine `foo` is not in the central registry —
consider contributing it at <configme repo URL>"), pointing at the local `.mk`.
No `gh`/network/auth coupling, no auto-PR. Optionally noted in `.install.sh`.

---

## 7. Per-package onboarding

To be configured by configme, a repo needs:

1. its template `config/Makefile` (already present everywhere); and
2. a **separated dependency fragment** `common.mk` (only yelmox has this today).

configme reads both from **`.configme/` first, falling back to `config/`**. The
dependency fragment is **repo-owned**; configme does not centralise it.

**Crossover for legacy repos** (yelmo, FastIsostasy, rembo1, coordinates, and
climber-x's set), whose flat `config/<machine>_<compiler>` files fuse all three
concerns:

- **Migrate on onboarding (preferred):** factor each flat file into its
  dependency third (→ a new `common.mk` committed to the repo) and deduplicate
  its compiler/machine thirds up into configme's shipped registry. Old flat
  files stay for the deprecated `config.py`.
- **Legacy fallback (labeled stopgap only):** if a repo has no dependency
  fragment, configme drives the old path — the existing flat
  `config/<machine>_<compiler>` file is used as the whole
  `<COMPILER_CONFIGURATION>`, with detected netCDF injected. That repo gets no
  centralisation until migrated.

---

## 8. Compile boundary and fesm-utils

- configme **configures only** — it never runs `make` for the orchestrator or
  template packages. Compilation is an explicit user step. The one exception is
  a package that declares a `[package.build]` spec (see `fesm-utils/utils`
  below), which configme builds via `make` after configuring it.
- **fesm-utils** is the structural outlier. Its checkout holds two distinct
  things, modelled as two packages:
  - **`fesm-utils`** itself — no `config/Makefile` template; the slow autotools
    build of LIS/FFTW (~10–30 min) driven by its own `build.py` +
    `machines/*.toml` registry. configme treats it as a **`build.py`-style
    package**: default is clone + link + **print** the exact
    `build.py --variant both -m <machine> -c <compiler>` command; `--build-deps`
    actually invokes `build.py`. configme forwards the resolved
    machine/compiler names and detected netCDF roots; it does **not** centralise
    fesm-utils' machine registry (autotools needs different variables/modules).
  - **`fesm-utils/utils`** — the shared utility library (`libfesmutils`), a
    makefile-template package contained in the fesm-utils checkout. It is
    declared as a `subpackage` of `fesm-utils` (so it rides along wherever
    fesm-utils is installed), marked `clone = false` (it arrives with the parent
    checkout and cannot be an install primary), and carries a `[package.build]`
    spec. configme generates its Makefile from the central machine/compiler
    fragments and, on `configme install fesm-utils` (with `--build-deps` or an
    interactive yes), builds it with `make openmp={0,1} fesmutils-static` in the
    inherited environment.
- **Subpackages** are the general mechanism for a component that shares another
  package's checkout: listed in the parent's `subpackages`, expanded into the
  plan right after the parent, and located relative to the parent's dest (so
  they follow any orchestrator `component_paths`).

---

## 9. Distribution and central data model

- pip-installable (`pip install git+https://github.com/fesmc/configme`),
  `src/configme/` layout, `[project.scripts] configme = "configme.cli:main"`,
  stdlib-only where possible (`tomllib`, `argparse`); `nf-config` / `nc-config`
  invoked as subprocesses. Curated fragments + seed manifests shipped as
  package-data.
- The central per-package and per-orchestrator knowledge is stored as **shipped
  TOML data files**, not hardcoded Python:
  - `configme/data/packages/*.toml` — per supported package: upstream
    org/repo, clone dir name, config style (`makefile-template` |
    `build.py`), inter-package link dependencies.
  - `configme/data/orchestrators/*.toml` — per orchestrator: default package
    set + declared extras (§13). Same shape as a `.configme/manifest.toml`. A
    `default_packages` entry may pin a git ref as `name:ref` (branch/tag/commit),
    e.g. climber-x's `yelmo:climber-x`; the ref is checked out after cloning.
    An optional `host` (on a package or orchestrator) selects a non-GitHub git
    host. An `optional_packages` list names components that are *attempted* on
    install but allowed to fail softly (private repos a given user may lack
    access to — climber-x's `bgc`/`vilma`): a clone failure is recorded as
    "unavailable" in the summary, not a hard failure, and subsequent steps skip
    the absent checkout.
  - Per-package flags of note: `optional` (soft clone failure, as above),
    `submodules` (run `git submodule update --init --recursive` after clone,
    e.g. bgc's M4AGO), and `config_style = "none"` for a clone-only component
    that configme places but does not configure or build (it is compiled by the
    orchestrator, or ships a prebuilt library — e.g. vilma).
- Onboarding a new package or orchestrator is a **data edit** (a natural PR
  target); the CLI logic stays generic and small.

---

## 10. On-disk `.configme/` contract

An orchestrator's `.configme/` holds:

- **`manifest.toml`** — `package` (the primary) + `deps` (validated against
  configme support). Portable; committable. Created only if missing.
- **`config.toml`** — resolved selections: `machine`, `compiler`, and the
  install choices (download mode, install dir, options) so re-runs are
  consistent. Also holds user/machine-specific extras values (§13).
  Install-local; gitignored.
- **`.gitignore`** — keeps `config.toml` out of the package repo while leaving
  `manifest.toml` and project-tier fragments below trackable.
- **`machines/`, `compilers/`** *(optional)* — local fragment overrides (top of
  the §6 precedence).

The template `config/Makefile` and the dependency `common.mk` stay in the
repo's `config/` (with the `.configme/`-first fallback of §7); they are not
duplicated into `.configme/`.

`configme init` scaffolds a starter `.configme/` (a commented `manifest.toml`
listing what configme supports, a `config.toml` with placeholders, a short
README comment). Re-running `init` **validates rather than clobbers**: reports
missing/invalid entries and fills only genuinely-missing files (the
`runme --init` pattern).

### Reproducibility log

configme writes an `.install.sh`-equivalent to the **orchestrator root** —
the exact clone / configure / link sequence, the **detected netCDF values** (so
a later rebuild is deterministic even if `nf-config` changes), and the printed
fesm-utils `build.py` line. It records the reproducible steps only (not one-shot
cleanups such as `--overwrite` moves). Hand-editable, replayable.

---

## 11. Robustness

- **Fail-fast validation** before any destructive/slow work: orchestrator and
  packages supported, machine + compiler resolve to fragments, manifest
  well-formed.
- **Atomic Makefile writes** (temp file + rename): an interrupted run never
  leaves a corrupt or half-written `Makefile`.
- **Idempotent re-runs:** existing clones skipped, Makefiles regenerated,
  existing links left as-is — always safe to re-run.
- **Per-package all-or-nothing; continue-and-summarise across the set:** a
  failing package does not abort the whole install; configme collects results
  and prints a final summary (configured / pending / failed), exiting non-zero
  if anything failed.
- **Missing dependency in explicit `+` mode:** create the (relative) link anyway
  so a later `configme install <dep>` just works, but warn loudly and mark it
  pending — never a silent dangling absolute link, never a hard failure.
- **netCDF detection failure** (§5): clear, actionable error.
- **`--dry-run`:** print the full planned sequence (and the `.install.sh` it
  would write) without executing.

---

## 12. Orchestrator extras (typed)

Post-config steps beyond clone/configure/link are modelled as a **small, closed
vocabulary of typed extras** with built-in handlers, declared per orchestrator
in its TOML — **not** as arbitrary shell hooks.

Initial types (those yelmox needs today):

- `pip_package` — `pip install -U` a command (e.g. `runme`); pip installs it if
  missing or upgrades it if out of date.
- `runme_config` — create/patch `.runme_config` (hpc/account).
- `data_link` — link runtime data (e.g. `ice_data`, `isostasy_data`). An
  existing link (or real dir) is kept untouched and not re-prompted; only a
  missing one is asked for, so re-running on a configured tree is quiet.
- `git_repo` — clone an auxiliary repo (any git host) into a named dir, e.g.
  climber-x's `input` from GitLab. Each entry is `{dir, org, repo, host?, ref?,
  protocol?}`; `host` defaults to GitHub, `ref` is checked out after cloning,
  and the install download mode (ssh/https/no) is honored. A per-entry
  `protocol` (`"https"`/`"ssh"`) pins the transport for that repo (overriding
  the download mode, e.g. a host where only HTTPS login is configured); `-d no`
  is never overridden. Because these repos can be large, each fresh clone is
  confirmed first (default **skip**); a declined clone is deferred and its
  command echoed in the install summary.

User/machine-specific values (data paths, hpc/account) are prompted or read
from `.configme/config.toml` — never shipped. climber-x reuses whichever apply
and declares its own. Adding a *new* extra type is a (rare) code change;
enabling/configuring existing types per orchestrator is pure data. Genuinely
one-off steps fall back to a printed reminder rather than a shell-hook escape
hatch.

---

## 13. Implementation order (tracer-bullet slices)

Rough sequencing for the issue breakdown:

1. **Skeleton package** — `src/configme/`, `pyproject.toml`, `configme` entry
   point, `list`, stub data dirs.
2. **netCDF detection** — `configme netcdf` + the detection/precedence logic
   (reused everywhere downstream).
3. **Config-only path for one package** — central compiler/machine registry +
   fragment assembly + atomic Makefile write, proven end-to-end on yelmox.
4. **Migrate one sibling** (e.g. yelmo) → `common.mk`; configure it via
   configme.
5. **Manifest + multi-package config** — `.configme/` contract, `configme init`,
   bare `configme` across an orchestrator.
6. **`configme install`** — clone + link + reproducibility log; selection
   semantics; robustness model.
7. **fesm-utils** `build.py`-style handling + `--build-deps`.
8. **Extras** (`pip_package`, `runme_config`, `data_link`).
9. **Onboard remaining siblings + climber-x orchestrator.**
