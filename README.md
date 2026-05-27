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

### Development install

To work on configme itself, clone it and install in editable mode so changes
take effect without reinstalling:

```bash
git clone git@github.com:fesmc/configme.git
cd configme
pip install -e .
```

## Getting started

```bash
cd yelmox                  # an orchestrator checkout
configme init              # scaffold .configme/ (manifest + config)
configme -m macbook -c gfortran   # configure every package in the manifest
```

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

### Cloning without a GitHub SSH key

`configme install` clones over SSH (`git@github.com:...`) by default, which
needs a GitHub SSH key. On a machine where you haven't set one up (e.g. a fresh
HPC account), use `-d clone-https` to clone over HTTPS instead:

```bash
configme install yelmox -d clone-https
```

Run `configme --help` for the full command surface and `configme list` to see
the supported orchestrators, packages, machines, and compilers.

This repository is in early development. The full design is specified in
[`docs/DESIGN.md`](docs/DESIGN.md).
