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
