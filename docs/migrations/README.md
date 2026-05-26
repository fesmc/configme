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

| package | artifact | notes | landed in repo? |
|---------|----------|-------|-----------------|
| yelmo             | [`yelmo/common.mk`](yelmo/common.mk)               | — | yes (committed to yelmo) |
| fesm-utils/utils  | [`fesm-utils/common.mk`](fesm-utils/common.mk)     | netCDF only | yes (committed to fesm-utils) |
| rembo1            | [`rembo1/common.mk`](rembo1/common.mk)             | folds `$(INC_COORD)` into `FFLAGS` | yes (committed to rembo1) |
| FastIsostasy      | [`FastIsostasy/common.mk`](FastIsostasy/common.mk) | shipped as a configme **overlay** (`data/overlays/FastIsostasy/common.mk`), copied into the checkout at configure time — **not** committed to the FastIsostasy repo | no (overlay) |
| coordinates       | [`coordinates/common.mk`](coordinates/common.mk)   | also needs a 1-line template normalization, see [`coordinates/README.md`](coordinates/README.md) | not yet |

Each artifact is verified by generating a Makefile from a working copy and
confirming the `openmp` / `petsc` toggles and link flags resolve as in the
legacy build.

## Overlays

When a package's `common.mk` cannot (yet) be committed to its own repo,
configme ships it as an *overlay* under `src/configme/data/overlays/<pkg>/
common.mk`. At configure time configme copies the overlay into the package's
`config/common.mk` (if absent) so the package configures via the normal modern
path without modifying its upstream. FastIsostasy uses this today.

## Legacy fallback

For a repo not yet migrated, `configme` falls back to using its existing flat
`config/<machine>_<compiler>` file as the whole compiler-configuration block,
with auto-detected netCDF appended to override the flat file's (often env-based)
`INC_NC`/`LIB_NC`. This is a labelled stopgap (the generated Makefile is marked
"LEGACY fallback"); the repo gets no centralisation until properly migrated.
