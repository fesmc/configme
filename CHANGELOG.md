# Changelog

Notable changes to `configme`. Entries before 0.14.0 were reconstructed from
the git history, so they summarise rather than record what was written at the
time.

The project follows [semantic versioning](https://semver.org) loosely: minor
for new capability, patch for fixes and refinements. Functionality changes are
expected to come with a version bump.

## 0.14.0

### Added

- `insol` package — orbital solution and insolation library. Standalone: not a
  component of any orchestrator, since yelmox vendors the insolation source
  under `libs/insol` rather than consuming the package.
- `chion` package.

## 0.13.3

### Changed

- `configme status`: package-centric table with a tree layout, fewer commands.

## 0.13.2

### Fixed

- Silenced the `runme config init` "Next steps" noise.

## 0.13.1

### Changed

- `configme git` shows a `name:ref` label per repo in fan-out output.

## 0.13.0

### Changed

- Extras default the hpc name and account from an existing config instead of
  asking again.

## 0.12.0

### Added

- `@machine` sentinel in refs, for per-HPC component branches.

## 0.11.0 – 0.11.3

### Added

- `--color` flag; colorized install/upgrade/git/status output.
- `tracer` and `elsa` packages, nested under yelmo.
- Optional CLI ref to pin the orchestrator or component branch.

### Changed

- Manifest ref pins resolve recursively down nested checkouts.

## 0.10.0

### Added

- `fesm-utils` became a makefile-template package emitting MACHINE/COMPILER.

### Changed

- `configme install` front-loads prompts and runs extras before the slow build.

### Removed

- The `build.py` subsystem, now dead.

## 0.9.0 – 0.9.6

### Added

- Data-package model for auxiliary repos, documented in the README.
- `FastEarth3D` orchestrator and package, later moved to the fesmc org.
- `configme check machine` to detect the CPU `-march`.
- `-march` tuning for `awi_albedo` (znver2) and `dkrz_levante` (znver3).
- `--repos` accepts a path, not just a package name.

### Fixed

- Do not trust an old gcc that under-reports `-march`.

## 0.8.0 – 0.8.2

### Added

- `data_packages`, the `prompt` clone policy, and an upgrade reminder.
- Per-repo `protocol` clone-transport override.
- runme/pip tools treated as version-pinnable dependencies.

### Changed

- Package `optional` flag generalized into a `clone_policy` enum.
- `climber-x-input` migrated to a data package; the `git_repo` extra removed.

## 0.7.0 – 0.7.8

### Added

- `--link` / `links.toml` to reuse an existing on-disk package checkout.
- `nest` links, cloning a dependency inside its consumer's checkout.
- `chinook` machine (UAF).
- `configme update <ref>` installs from a branch, tag or SHA.

### Fixed

- 30s hang in `hostname_machine()` on macOS with no PTR record.
- `root_for` treating any cwd containing `.configme/` as the primary checkout.
- Self-referential symlinks refused in `link_external` and `data_link`.
- Loud failure when a `common.mk` exists but the template does not include it.

## 0.5.0 – 0.6.11

### Added

- `configme status`, a read-only inspector, plus a pending block in the
  install/config summaries.
- `configme git`: fan-out of a git command across managed repos.
- `configme install runme` shortcut.
- `FastHydrology` as a managed package, wired into yelmo and yelmox.
- `[package.artifacts]` schema for build-completeness probing.

### Changed

- Re-install is idempotent: set links and built packages are skipped.
- `common.mk` is no longer inlined; the template's own `include` pulls it in.

### Fixed

- `runme_config` targets `.runme/config.toml` (TOML, not JSON).
- Pull skipped on detached HEAD during `configme upgrade`.

## 0.4.0 – 0.4.12

### Added

- `climber-x` onboarding: root layout, yelmo ref, runme/input extras,
  bgc/vilma.
- `configme update`, self-updating via `pip install -U`.
- netCDF library rpath embedded so binaries find shared libs at runtime.

### Changed

- Manifest deps can pin component refs, overriding orchestrator defaults.
- Pinned component refs enforced on existing checkouts.

## 0.2.0 – 0.2.2

### Added

- Per-component git refs, non-GitHub hosts, optional and clone-only packages.
- `configme upgrade` to pull and reconfigure installed stacks.

### Changed

- Primary checkout detected by Makefile template rather than directory name.
- Install summary reports the real build outcome (built/deferred/failed).

## 0.1.0 – 0.1.4

Initial release and early iteration.

### Added

- `configme list`, `netcdf`, `config`, `install`, `init`.
- Makefile generation from a package's `config/Makefile` template plus shared
  machine/compiler fragments, with netCDF auto-detected.
- `.configme` contract, manifest, and multi-package configuration.
- Typed orchestrator extras: `pip_package`, `runme_config`, `data_link`.
- Onboarding for yelmox, yelmo, FastIsostasy, rembo1, coordinates, climber-x.
- Machine/compiler fragment authoring: `show`, `new`, and an escape valve.
- Machine auto-detection from hostname, FQDN, and OS platform.
