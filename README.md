# configme

`configme` is a centrally-installed Python package that configures the build of
the yelmox / climber-x model stacks and their component packages from a single
source of machine- and compiler-specific information.

It replaces the per-repository `config.py` and the orchestrator-specific
`install.py` with one tool: detect the netCDF installation automatically, build
the Makefile for each component from its template using shared machine/compiler
fragments, and clone/link a whole stack with one command.

```bash
configme install yelmox    # clone + configure + link (+ build) a whole stack
configme upgrade yelmox    # git pull the stack + reconfigure (+ rebuild if changed)
configme config yelmox     # (re)generate Makefiles for an already-present stack
configme status yelmox     # report what is present and what is still pending (read-only)
configme                   # (re)configure the current directory (orchestrator or package)
configme show macbook      # print a machine (or compiler) fragment to copy/edit
configme new machine mybox # scaffold a new machine fragment from linux, to edit
configme netcdf            # detect & print NC_FROOT / NC_CROOT
```

## Install

`configme` is installed once, globally, and provides the `configme` command on
your `PATH`:

```bash
pip install git+https://github.com/fesmc/configme
```

To upgrade later, add `--upgrade` (or `--force-reinstall`) to the same command.
The only runtime dependency is `tomli` on Python < 3.11 (`tomllib` is used on
3.11+); it is installed automatically. `configme` invokes `nf-config` /
`nc-config`, `git`, and `make` as external commands when needed.

If the `configme` command is not found after installing, the Python user bin
directory is probably not on your `PATH`. Add it to your `~/.bashrc` /
`~/.zshrc`:

```bash
export PATH="${PATH}:${HOME}/.local/bin"
```

## Getting started

The most common workflow is a one-shot `install`: clone a whole stack,
configure every package for your machine/compiler, link the orchestrator, and
build what needs building — all in one command.

```bash
configme install yelmox                              # clone + configure + link (+ build) the whole stack
configme install yelmox -m dkrz_levante -c ifx       # pick the machine + compiler explicitly
configme install yelmox -d https                     # clone over HTTPS (no GitHub SSH key needed)
configme install yelmox --dir ~/models/yelmox        # put the checkout here instead of ./yelmox
configme install yelmox --build-deps                 # rebuild dependency packages without prompting
configme install yelmo                               # just yelmo + the sub-packages it needs
configme install yelmo --only                        # only yelmo, nothing pulled in
configme install yelmox:dev                          # orchestrator on its `dev` branch (its manifest then drives components)
configme install climber-x:alex-dev                  # same for climber-x
configme install yelmox+yelmo:mybranch               # pin one component; CLI ref overrides the manifest
configme install yelmox --link fesm-utils=/abs/path  # symlink an existing checkout instead of cloning
configme install yelmox --dry-run                    # preview what would be cloned/configured
```

If you omit `-m`/`-c`, configme detects the machine from the hostname where it
can and otherwise prompts you. Options combine freely, e.g.:

```bash
configme install yelmox -m dkrz_levante -c ifx -d https
```

By default `configme install` clones over SSH (`git@github.com:...`), which
needs a GitHub SSH key. On a machine where you haven't set one up (e.g. a fresh
HPC account), `-d https` clones over HTTPS instead. Use `--overwrite` to
re-clone over an existing checkout, or `-d no` to skip cloning entirely and
configure whatever is already on disk.

Run `configme --help` for the full command surface and `configme list` to see
the supported orchestrators, packages, machines, and compilers.

## Working in an existing directory

If you already have an orchestrator checkout on disk (cloned by hand, or one you
are developing in), run configme from inside it to configure the stack without
cloning anything:

```bash
cd yelmox                          # an existing orchestrator checkout
configme init                      # optional — only needed if the dir name is different than the package name
configme -m macbook -c gfortran    # configure every package in the stack
```

`configme init` scaffolds `.configme/` (a committable `manifest.toml` plus a
`config.toml` template). It is optional: a checkout whose directory name matches
its package name (e.g. `yelmox`) is recognised without it, and the bare command
above creates `.configme/config.toml` for you on first run. Use `init` when the
directory name is different than the package name (the manifest makes it
recognisable regardless of directory name) or when you want the manifest tracked
in git.

### Configure part of a stack

`configme config` shares its target grammar with `configme install`, so the
same names work for both:

```bash
configme config yelmo -m macbook -c gfortran    # yelmo + the sub-packages it needs
configme config yelmox+yelmo                     # exactly those two, no expansion
configme config yelmo --only                     # just yelmo, nothing pulled in
cd yelmo && configme -m macbook -c gfortran      # bare = `configme config` on this dir
```

`configme config` only (re)generates Makefiles — it never clones, links, or
builds. Add `--dry-run` to preview which Makefiles would change.

### Reuse an existing checkout (`--link`)

If a package is already cloned somewhere else on disk (e.g. a shared
`fesm-utils` build on an HPC filesystem, or a development checkout you maintain
by hand), point configme at it instead of cloning a duplicate:

```bash
configme install yelmox --link fesm-utils=/work/shared/fesm-utils
configme install yelmox --link fesm-utils=/abs/a --link coordinates=/abs/b
```

configme symlinks the package's slot inside the install root (`yelmox/fesm-utils
-> /work/shared/fesm-utils`) and skips its clone, configure, and build steps —
the linked checkout is treated as user-managed. Inter-package links (e.g.
`yelmox/yelmo/fesm-utils -> ../fesm-utils`) still work because they chain through
the symlink. The primary package itself cannot be linked — use `--dir` for that.

A non-existent link target is a hard error: configme will not fall back to
cloning, since silent fallback would defeat the point of the flag.

Frequently-reused mappings can live in TOML instead:

```toml
# ~/.configme/links.toml             — applies to every install
# <root>/.configme/links.toml        — overrides global per-package
[links]
"fesm-utils" = "/work/shared/fesm-utils"
"coordinates" = "/work/shared/coordinates"
```

Precedence: `--link` (CLI) overrides `<root>/.configme/links.toml` (project)
overrides `~/.configme/links.toml` (global). CLI links are applied silently;
file-supplied links are confirmed one at a time at install time (the file may
carry stale entries — better to ask than silently link a wrong path).

`configme upgrade` honors links too: a linked package's pull and rebuild are
skipped because the external checkout is the user's to manage. Pass `--link`
again at upgrade time to (re)point a link at a different target.

After each successful build, configme writes a small `.configme-build.toml`
into the package's checkout recording the machine and compiler it was built
for. When a later install links that same checkout into a project with a
different `(machine, compiler)`, the link line gets a `(! …)` warning so a
toolchain mismatch is visible before it confuses the consumer's build.

### Upgrade an installed stack

`configme upgrade` pulls new versions of already-installed checkouts and
reconfigures them with the same machine/compiler you installed with (read from
`.configme/config.toml`; override with `-m`/`-c`). It shares the target grammar
with `install`/`config`:

```bash
configme upgrade            # the current checkout's primary + its deps
configme upgrade yelmox     # the whole yelmox stack
configme upgrade yelmo      # yelmo + the sub-packages it needs
configme upgrade yelmo --only   # just yelmo, nothing pulled in
```

Each checkout is updated in place with `git pull --ff-only` on whatever branch
is checked out. A checkout that is missing, has uncommitted changes, or whose
branch has diverged from upstream is **skipped with a warning and never
touched** — resolve those by hand (anything beyond a clean fast-forward is left
to you). Every package is then reconfigured; a package whose pull brought in new
commits is also rebuilt when it is a build-it package (e.g. `fesm-utils`), gated
exactly like `install` — `--build-deps` rebuilds without asking, otherwise you
are prompted. Add `--dry-run` to preview the pulls and reconfigures.

The orchestrator's **data repos** are refreshed alongside the components. These
are auxiliary, clone-only checkouts — for CLIMBER-X, the large `input/` data repo
on GitLab — declared in the orchestrator's `data_packages` list. Upgrade pulls
one that is already on disk, but **never clones** a missing one: upgrade only
ever refreshes what you already have. If a data repo (or an optional private
component) was never installed, the run ends with a **reminder** naming it and
the `configme install` command to fetch it — so the big `input` repo is surfaced
without forcing a multi-GB download.

The `pip_package`/`runme_config` post-config extras (e.g. the `runme` tool) are
offered a re-run, each a `y/N` prompt (default **no**). To run the whole upgrade
unattended, pass `-y` — it answers yes to *every* prompt (ref switches, rebuilds,
data-repo pulls), so `configme upgrade -y` needs no input.

To touch only specific checkouts, use `--repos` with a comma-separated list —
components and/or data repos. Each entry is either a **package name** or a
**path** to where the checkout already lives (handy when you don't recall the
package name): a path is matched against the managed checkouts, `--link` targets
and symlinks included, and resolved back to the package name.

```bash
configme upgrade -y                            # pull/refresh everything, no prompts
configme upgrade --repos climber-x-input       # by package name
configme upgrade --repos ./input               # by path — resolves to climber-x-input
configme upgrade --repos yelmo,./input         # mix names and paths
```

The same `--repos` (name or path) applies to `configme git`.

### Check what is still pending

`configme status` is a **read-only** report of what is actually on disk versus
what the plan expects — it never clones, configures, links, or builds. It shares
the target grammar with `install`/`config` and answers the questions you have
after a partial install: are all the repositories there (including optional and
data ones that may have been skipped)? Are the inter-component links in place?
Is `fesm-utils` built for every variant?

```bash
configme status            # the current checkout's primary + its deps
configme status yelmox     # the whole yelmox stack
configme status -v         # show every check, including the ones already ok
```

It groups checks into **repos**, **links**, **builds**, and **extras**, hides
everything that is already in order, and ends with the exact commands to run by
hand for whatever is left:

```text
  Builds:
    [partial] fesm-utils (omp)  (missing fftw-omp/lib/libfftw3.a)  -> configme install fesm-utils --build-deps

Run these when ready:
  configme install fesm-utils --build-deps
```

The exit status is non-zero only for a genuine problem (a required repo missing,
a symlink broken, a half-finished build); intentionally deferred items (a
skipped optional repo, a data clone you postponed, an unbuilt variant) are shown
as `pending` but do not fail. The same "still pending" block is appended to the
`install` and `config` summaries, so a normal run also reminds you of anything
outstanding.

### A machine configme doesn't know yet

On a new machine or cluster, give it any name you like. `configme new machine`
scaffolds a starter fragment (seeded from `linux`) into both the project's
`.configme/machines/` and your `~/.configme/machines/`, ready to edit:

```bash
configme new machine mybox            # creates mybox.mk in project + user tiers
configme show macbook                 # print an existing one as a reference to copy
configme config yelmox -m mybox -c gfortran
```

You don't even have to run `new` first: if you pass an unknown machine to
`config`/`install` at the interactive prompt, configme offers to create it from
`linux` on the spot. Once your fragment works well, please consider
contributing it back to configme so others can reuse it.

The `.mk` fragment above configures the libraries configme builds itself. One
dependency — `fesm-utils` — builds its bundled `fftw`/`lis` with its own
`build.py`, which reads a **separate** machine file in its own schema (compiler
vars, `module load`s, and per-component overrides configme deliberately doesn't
model). So `configme new machine <name>` (and the unknown-machine prompt above)
writes *two* stubs per tier: the `<name>.mk` fragment **and** a `<name>.toml` in
fesm-utils' build.py schema, seeded from a bundled template with the machine name
filled in. Edit the `.toml`'s compilers/modules for your cluster (an HPC machine
like chinook+ifx needs an `[compilers.ifx]` table and the right `[modules]`).

When configme drives `build.py`, it copies your `<name>.toml` into the
`fesm-utils` checkout's `machines/` so `build.py -m <name>` resolves it — you
never edit the checkout directly. If a same-named file already exists there and
differs, configme asks before overwriting (default no), so a checked-in machine
file is never clobbered.

### Standalone tools

A few standalone fesmc tools that aren't model packages can still be installed
through `configme install` as a convenience — currently just
[`runme`](https://github.com/fesmc/runme):

```bash
configme install runme            # pip install -U git+https://github.com/fesmc/runme
configme install runme --dry-run  # print the pip command without running it
```

This is exactly the same install the `pip_package` orchestrator extra does
automatically as part of `configme install yelmox` / `climber-x`; the standalone
form is handy when you want to install or update `runme` on its own. Other
`install` flags (`-m`/`-c`/`--dir`/`-d`/`--build-deps`) don't apply and are
ignored. `configme list` shows the available tools under **Tools:**.

## For configme developers

To work on configme itself, clone it and install in editable mode so your
changes take effect immediately, without reinstalling:

```bash
git clone git@github.com:fesmc/configme.git
cd configme
pip install -e .
```

With an editable (`pip install -e .`) install, the globally-available `configme`
command always reflects your working tree, so you can iterate on the code and
rerun commands directly — no reinstall step between edits.

This repository is in early development. The full design is specified in
[`docs/DESIGN.md`](docs/DESIGN.md).
