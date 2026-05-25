# coordinates migration note

Besides adding `config/common.mk`, the coordinates `config/Makefile` template
needs a one-line normalization so it uses the shared compiler fragment's
`FFLAGS` instead of its own `FFLAGS_DEFAULT` / `FFLAGS_OPENMP` selection.

Replace this block in `config/Makefile`:

```make
# Determine whether to use openmp flags
FFLAGS = $(FFLAGS_DEFAULT)
ifeq ($(openmp), 1)
	FFLAGS = $(FFLAGS_OPENMP)
endif
```

with:

```make
# OpenMP flags appended by config/common.mk (FFLAGS comes from the compiler
# fragment; configme assembles compiler -> machine -> netCDF -> common).
```

i.e. delete the `FFLAGS_DEFAULT`/`FFLAGS_OPENMP` selection — `common.mk` now
sets the coordinates-specific compile flags (`-fbackslash`, `$(INC_NC)`) and the
`openmp` toggle. The compiler fragment provides the base `FFLAGS` and
`FFLAGS_OPENMP = -fopenmp`.
