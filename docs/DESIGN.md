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
  (`configme yelmo`) or all of them (`configme`).
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

Each orchestrator carries a `.configme/manifest.toml` listing its packages.
configme validates that it supports every listed package. The manifest lists
packages only; it does **not** describe per-package needs (org, link
dependencies, config style) — configme centralises those (§10).

**Manifest precedence:** local `.configme/manifest.toml` (if present) wins;
otherwise configme uses its own shipped seed manifest for that orchestrator. A
freshly-cloned supported orchestrator should carry its own manifest.

---

## 4. Command surface

```
configme install <target> [options]   # clone/use-existing + configure + link
configme [pkgs…]                       # config-only: (re)generate Makefile(s)
configme netcdf                        # detect & print NC_FROOT / NC_CROOT
configme init                          # scaffold/validate a .configme/ folder
configme list                          # supported packages / machines / compilers
```

### `configme install`

```
configme install yelmox                # orchestrator + its full default set
configme install yelmo                 # one package + the sub-packages it needs (auto-resolve)
configme install yelmox+yelmo          # exactly those two, literal, no auto-resolve
```

Options (mirroring today's `install.py`):

- `-d, --download {clone-ssh|clone-https|no}` — how to obtain repos
  (`no` = use existing dirs/symlinks).
- `--dir DIR` — where to install (default `./<orchestrator>`, e.g. `./yelmox`).
- `-m, --machine NAME`, `-c, --compiler NAME` — selection (see §6).
- `--overwrite` — re-clone, moving existing copies aside.
- `--build-deps` — also run `build.py`-style package builds (e.g. fesm-utils);
  off by default (§9).
- `--dry-run` — print the full plan (and the `.install.sh` it would write)
  without executing (§12).

Selection semantics:

- A **single name** auto-resolves and installs the sub-packages it needs.
- A **`+`-separated list** is literal: exactly those packages, no
  auto-resolution. (Missing dependencies are handled per §12d.)
- An **orchestrator name** pulls its full default set.

`configme install` may be run from anywhere (uses `--dir`) or from *inside* an
already-cloned orchestrator/package, in which case it skips cloning that root
and performs the remaining steps.

### `configme [pkgs…]` (config-only)

Regenerates Makefile(s) from template + central machine/compiler + repo
`common.mk`. No clone, no link. Bare `configme` = every package in the current
orchestrator. This is the original `configme yelmo` / `configme FastIsostasy`.

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
5. interactive prompt (TTY only)

The selected machine + compiler are recorded in the orchestrator's
`.configme/config.toml` so every subsequent `configme <pkg>` inside that
orchestrator applies the **same** pair to all components — this is what removes
the per-package repetition that `config.py` forces today.

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
  template packages. Compilation is an explicit user step.
- **fesm-utils** is the structural outlier: no `config/Makefile` template, an
  autotools build of LIS/FFTW/utils (slow, ~10–30 min), and its own
  `machines/*.toml` registry. configme treats it as a **`build.py`-style
  package**:
  - default: clone + link + **print** the exact
    `build.py --variant both -m <machine> -c <compiler>` command;
  - `--build-deps`: actually invoke `build.py`.
  - configme forwards the resolved machine/compiler names and the detected
    netCDF roots; it does **not** centralise fesm-utils' machine registry
    (autotools needs different variables/modules).

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
    set + declared extras (§13). Same shape as a `.configme/manifest.toml`.
- Onboarding a new package or orchestrator is a **data edit** (a natural PR
  target); the CLI logic stays generic and small.

---

## 10. On-disk `.configme/` contract

An orchestrator's `.configme/` holds:

- **`manifest.toml`** — the package list (validated against configme support).
- **`config.toml`** — resolved selections: `machine`, `compiler`, and the
  install choices (download mode, install dir, options) so re-runs are
  consistent. Also holds user/machine-specific extras values (§13).
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

- `pip_package` — pip-install a command if missing (e.g. `runme`).
- `runme_config` — create/patch `.runme_config` (hpc/account).
- `data_link` — link runtime data (e.g. `ice_data`, `isostasy_data`).

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
