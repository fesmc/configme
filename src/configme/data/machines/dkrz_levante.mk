# Machine configuration: dkrz_levante (intended compiler: ifx).
#
# netCDF is auto-detected by configme (load the netCDF module first so
# nf-config/nc-config are on PATH). To pin it, assign INC_NC / LIB_NC here.

# CPU-specific optimization; overrides the ifx default DFLAGS_NODEBUG.
# Levante compute nodes are AMD EPYC 7763 (Milan/Zen3): AVX2, no AVX-512.
# (Note: the login-node gcc is too old to name Zen3 and mis-resolves
# -march=native to znver1 -- confirm the CPU with lscpu, not gcc.)
DFLAGS_NODEBUG = -Ofast -march=znver3 -traceback
