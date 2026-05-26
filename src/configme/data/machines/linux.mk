# Machine configuration: linux — the generic Linux base (mirrors macbook).
#
# Use this as the default on a Linux box with no machine-specific needs. netCDF
# is auto-detected by configme (nf-config/nc-config) — no NC_FROOT / NC_CROOT in
# .bashrc required; to pin it, assign INC_NC / LIB_NC here as an override.
#
# Unlike macbook, this fragment sets nothing, so common.mk's default applies
# (LFLAGS_EXTRA ?= -Wl,-zmuldefs — the GNU-ld flag macbook disables for Apple ld).
