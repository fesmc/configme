# Machine configuration: macbook (intended compiler: gfortran).
#
# netCDF is auto-detected by configme (nf-config/nc-config) — no NC_FROOT /
# NC_CROOT in .zshrc required. To pin it instead, assign INC_NC / LIB_NC here
# and configme will use those as an override.

# Disable the default -Wl,-zmuldefs: gfortran forwards it to Apple's ld (ld64),
# which rejects it ("ld: unknown options: -zmuldefs"). It is a GNU-ld/ELF flag.
LFLAGS_EXTRA =
