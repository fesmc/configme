# Migration artifacts

Each supported legacy package must be migrated from its monolithic flat config
files (`config/<machine>_<compiler>`) to the two-axis layout configme expects:
the package keeps its `config/Makefile` template plus a **repo-owned
`common.mk`** (dependency wiring only), while the compiler/machine content is
centralised in configme's shipped registry (see `docs/DESIGN.md` §7).

This directory holds the `common.mk` produced for each package during
onboarding, as a record and a ready-to-land artifact. The file is intended to
be committed into the package's own repo (under `config/common.mk`); it is
staged here until that lands, so configme can be developed and reviewed without
modifying the component repos prematurely.

| package | artifact | landed in repo? |
|---------|----------|-----------------|
| yelmo   | [`yelmo/common.mk`](yelmo/common.mk) | not yet |

Verified by generating a Makefile from a working copy and confirming the
`openmp` / `petsc` toggles and link flags resolve as in the legacy build.
