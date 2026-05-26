# configme

`configme` is a centrally-installed Python package that configures the build of
the yelmox / climber-x model stacks and their component packages from a single
source of machine- and compiler-specific information.

It replaces the per-repository `config.py` and the orchestrator-specific
`install.py` with one tool: detect the netCDF installation automatically, build
the Makefile for each component from its template using shared machine/compiler
fragments, and clone/link a whole stack with one command.

```bash
configme install yelmox    # clone + configure + link a whole stack
configme yelmo             # (re)configure a single package
configme                   # (re)configure every package in the current orchestrator
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

Run `configme --help` for the full command surface and `configme list` to see
the supported orchestrators, packages, machines, and compilers.

This repository is in early development. The full design is specified in
[`docs/DESIGN.md`](docs/DESIGN.md).
