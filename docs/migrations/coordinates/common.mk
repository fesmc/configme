# Shared build configuration for coordinates (dependency wiring).
#
# coordinates depends only on netCDF. References FFLAGS / FFLAGS_OPENMP
# (compiler) and LIB_NC (machine or auto-detected netCDF).

# coordinates-specific compile flags: -fbackslash and the netCDF include.
FFLAGS += -fbackslash $(INC_NC)

ifeq ($(openmp), 1)
    FFLAGS += $(FFLAGS_OPENMP)
endif

LFLAGS_EXTRA ?=
LFLAGS = $(LIB_NC) $(LFLAGS_EXTRA)
