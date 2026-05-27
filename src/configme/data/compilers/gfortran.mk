# Compiler configuration: GNU Fortran (gfortran).
#
# A machine fragment (config/machines/<machine>.mk) is loaded *after* this
# file and may override any of these variables (the later assignment wins) --
# e.g. DFLAGS_NODEBUG for CPU-specific optimization.

FC = gfortran

# Base Fortran flags (module/object dir wiring). Exposed separately so a
# template that reassigns FFLAGS per build target (e.g. climber-x's
# FFLAGS = $(FFLAGS_CLIM)) can compose FFLAGS_BASE into its own variants without
# a recursive self-reference.
FFLAGS_BASE = -ffree-line-length-none -I$(objdir) -J$(objdir)
FFLAGS = $(FFLAGS_BASE)
FFLAGS_OPENMP = -fopenmp

# Preprocessor flag (Fortran source preprocessing); -cpp for GNU Fortran.
CPPFLAGS_PP = -cpp

DFLAGS_NODEBUG = -O2
DFLAGS_DEBUG   = -w -g -ggdb -ffpe-trap=invalid,zero,overflow,underflow -fbacktrace -fcheck=all
DFLAGS_PROFILE = -O2 -pg
