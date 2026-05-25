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

This repository is in early development. The full design is specified in
[`docs/DESIGN.md`](docs/DESIGN.md).
