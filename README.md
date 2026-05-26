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

Run `configme --help` for the full command surface and `configme list` to see
the supported orchestrators, packages, machines, and compilers.

This repository is in early development. The full design is specified in
[`docs/DESIGN.md`](docs/DESIGN.md).
