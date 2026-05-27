# Compiler configuration: Intel Fortran classic (ifort).
#
# A machine fragment (config/machines/<machine>.mk) is loaded *after* this
# file and may override any of these variables (the later assignment wins).
# DFLAGS_NODEBUG here is a safe, portable default; machines with a known CPU
# may override it with -march tuning.

FC = ifort

# Base Fortran flags (module/object dir wiring). Exposed separately so a
# template that reassigns FFLAGS per build target (e.g. climber-x's
# FFLAGS = $(FFLAGS_CLIM)) can compose FFLAGS_BASE into its own variants without
# a recursive self-reference.
FFLAGS_BASE = -no-wrap-margin -module $(objdir) -L$(objdir)
FFLAGS = $(FFLAGS_BASE)
FFLAGS_OPENMP = -qopenmp

# Preprocessor flag (Fortran source preprocessing); -fpp for Intel compilers.
CPPFLAGS_PP = -fpp

DFLAGS_NODEBUG = -O2 -fp-model precise
DFLAGS_DEBUG   = -C -O0 -g -traceback -ftrapuv -fpe0 -check all,nouninit -fp-model precise -debug extended -gen-interfaces -warn interfaces -check arg_temp_created
DFLAGS_PROFILE = -O2 -fp-model precise -pg
