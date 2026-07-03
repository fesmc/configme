# Machine configuration: awi_albedo (intended compiler: ifx).
#
# netCDF is auto-detected by configme (load the netCDF module first so
# nf-config/nc-config are on PATH). To pin it, assign INC_NC / LIB_NC here.

# CPU-specific optimization; overrides the ifx default DFLAGS_NODEBUG.
# Albedo compute nodes (mpp/smp) are uniformly AMD EPYC 7702 (Rome/Zen2),
# AVX2 with no AVX-512 -- so znver2 is a pure tuning win with no ISA risk.
DFLAGS_NODEBUG = -Ofast -march=znver2 -traceback
