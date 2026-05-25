# Machine configuration: awi_albedo (intended compiler: ifx).
#
# netCDF is auto-detected by configme (load the netCDF module first so
# nf-config/nc-config are on PATH). To pin it, assign INC_NC / LIB_NC here.

# CPU-specific optimization; overrides the ifx default DFLAGS_NODEBUG.
DFLAGS_NODEBUG = -Ofast -march=core-avx2 -mtune=core-avx2 -traceback
