"""`configme install`: clone, configure, and link a stack (see docs/DESIGN.md
sec. 4, 11). This replaces the bespoke per-orchestrator install.py.

Layout: components are cloned as sub-packages directly under the orchestrator
(or under the primary package) — e.g. ``yelmox/yelmo``, ``yelmox/fesm-utils``.
The primary therefore references them directly; only non-primary packages need
inter-component links (e.g. ``yelmox/yelmo/fesm-utils -> ../fesm-utils``), taken
from each package's ``[[package.links]]``.

Selection semantics:
  * an orchestrator name      -> orchestrator + its full default set
  * a single package name     -> that package + the sub-packages it needs (auto)
  * a '+'-joined literal list -> exactly those, no auto-resolution
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from configme import color, context, data, extras as extras_mod, generate, links as links_mod, netcdf


class InstallError(Exception):
    pass


@dataclass
class Node:
    """A thing to install: a package or the orchestrator."""
    name: str
    org: str
    repo: str
    dir: str
    config_style: str
    config_subdir: str
    links: list
    is_orchestrator: bool
    clone: bool = True
    subpackages: list = field(default_factory=list)
    build: "Optional[data.BuildSpec]" = None
    # Git host to clone from (default GitHub) and an optional ref (branch/tag/
    # commit) to check out right after cloning. ``ref`` is set for orchestrator
    # components that pin one (e.g. climber-x's yelmo -> ``climber-x`` branch).
    host: str = "github.com"
    # Per-repo clone transport override ("https"/"ssh"); see data.Package. None
    # means follow the run's `-d` download mode.
    protocol: Optional[str] = None
    ref: Optional[str] = None
    # True when ``ref`` came from an explicit CLI ``name:ref`` spec (e.g.
    # ``configme install yelmox:dev``). Such a ref is authoritative: the
    # orchestrator- and manifest-ref override passes leave it untouched, so the
    # CLI wins over both. Precedence: CLI ref > manifest pin > orchestrator
    # default > repo default branch.
    ref_pinned: bool = False
    # How this checkout's absence is handled at clone time: "required" (a clone
    # failure is fatal), "optional" (a failure is a soft "unavailable" skip — a
    # private repo the user may lack access to), or "prompt" (not cloned unless
    # the user opts in, default no — a large/expensive repo). See data.Package.
    # ``submodules`` runs `git submodule update --init --recursive` after clone.
    clone_policy: str = "required"
    submodules: bool = False
    # For a subpackage node: the parent package name and the path relative to the
    # parent's dest (so its location follows the parent under any orchestrator).
    parent: Optional[str] = None
    subdir: str = ""


@dataclass
class Plan:
    primary: Node
    nodes: List[Node]          # everything to install, deps before dependents
    explicit: bool             # True for a '+'-list (no auto-resolution)
    orchestrator: Optional[data.Orchestrator]


# --------------------------------------------------------------- plan building

def build_clone_url(host: str, org: str, repo: str, download: str) -> str:
    """Build a git clone URL for ``org/repo`` on ``host``. ``download`` selects
    the transport: ``https`` -> ``https://<host>/<org>/<repo>.git``, anything
    else (ssh) -> ``git@<host>:<org>/<repo>.git``. Host-agnostic so GitHub,
    GitLab, etc. all work from the same registry data."""
    if download == "https":
        return f"https://{host}/{org}/{repo}.git"
    return f"git@{host}:{org}/{repo}.git"


def resolve_repo_selectors(entries: List[str],
                           candidates: List[Tuple[str, Path]]) -> set:
    """Resolve ``--repos`` entries to a set of package names.

    Each entry is either a **package name** or a **filesystem path** to where a
    managed checkout already lives. ``candidates`` is the ``[(name, dest), ...]``
    this command can act on (all plan checkouts for ``upgrade``; the present ones
    for ``git``). Resolution is name-first:

      * an entry equal to a candidate name is used directly;
      * otherwise it is treated as a path — ``~``/vars expanded, made absolute
        relative to cwd, symlinks followed (``resolve()``) — and matched against
        the candidates' resolved ``dest`` paths, so pointing at a checkout's real
        location (including a ``--link`` target outside the root) selects it.

    Name wins over a path that merely happens to exist, so a real package name is
    never shadowed by the cwd. Unknown entries raise ``InstallError`` listing the
    available names."""
    names = {name for name, _ in candidates}
    by_path: Dict[Path, str] = {}
    for name, dest in candidates:
        try:
            by_path.setdefault(Path(dest).resolve(), name)
        except OSError:
            pass
    selected: set = set()
    unknown: List[str] = []
    for e in entries:
        if e in names:
            selected.add(e)
            continue
        try:
            p = Path(os.path.expandvars(os.path.expanduser(e))).resolve()
        except OSError:
            p = None
        if p is not None and p in by_path:
            selected.add(by_path[p])
            continue
        unknown.append(e)
    if unknown:
        raise InstallError(
            f"--repos: unknown repo(s) or path(s) {', '.join(unknown)}. "
            f"available: {', '.join(sorted(names))}")
    return selected


def _node_for(name: str, *, prefer_package: bool = False) -> Node:
    orchs = data.orchestrators()
    pkgs = data.packages()
    # A name can be registered as *both* an orchestrator and a component package
    # (e.g. FastEarth3D — standalone orchestrator, yet also a climber-x
    # component). The bare target resolves to the orchestrator; ``prefer_package``
    # picks the package form for the component position (orchestrator expansion,
    # or the non-primary slots of a ``+``-literal).
    if name in orchs and not (prefer_package and name in pkgs):
        o = orchs[name]
        return Node(o.name, o.org, o.repo, o.dir, o.config_style, "", [], True,
                    host=o.host)
    if name in pkgs:
        p = pkgs[name]
        return Node(p.name, p.org, p.repo, p.dir, p.config_style,
                    p.config_subdir, p.links, False,
                    clone=p.clone, subpackages=p.subpackages, build=p.build,
                    host=p.host, protocol=p.protocol,
                    clone_policy=p.clone_policy, submodules=p.submodules)
    known = sorted(set(orchs) | set(pkgs))
    raise InstallError(f"unknown target '{name}'. Supported: {', '.join(known)}")


def _node_for_spec(spec: str, *, prefer_package: bool = False) -> Node:
    """Resolve a CLI target token that may carry a ``name:ref`` pin.

    The bare ``name`` selects the node (so ``yelmox:dev`` still resolves to the
    ``yelmox`` orchestrator); an explicit ref stamps it and marks it CLI-pinned
    so the manifest/orchestrator override passes leave it alone — the CLI ref
    wins (see ``Node.ref_pinned``)."""
    name, ref = data.split_ref(spec)
    node = _node_for(name, prefer_package=prefer_package)
    if ref:
        node.ref = ref
        node.ref_pinned = True
    return node


def _with_subpackages(nodes: List[Node]) -> List[Node]:
    """Expand each node's contained subpackages, inserted right after it. A
    subpackage shares its parent's checkout (not cloned separately), so its dest
    is anchored to the parent via `parent`/`subdir`. Deduped by name."""
    out: List[Node] = []
    seen = set()
    for n in nodes:
        if n.name not in seen:
            out.append(n)
            seen.add(n.name)
        for sub_name in n.subpackages:
            if sub_name in seen:
                continue
            sub = _node_for(sub_name)
            sub.parent = n.name
            sub.subdir = str(Path(sub.dir).relative_to(n.dir))
            out.append(sub)
            seen.add(sub_name)
    return out


def _parent_of(name: str) -> Optional[str]:
    """The package that contains `name` as a subpackage, if any."""
    for pkg in data.packages().values():
        if name in pkg.subpackages:
            return pkg.name
    return None


def _resolve_deps(name: str, pkgs: Dict[str, data.Package],
                  seen: List[str]) -> None:
    """Post-order DFS over package link deps (deps before dependents)."""
    if name in seen:
        return
    for link in pkgs[name].links if name in pkgs else []:
        _resolve_deps(link.dep, pkgs, seen)
    if name not in seen:
        seen.append(name)


def _apply_component_refs(nodes: List[Node],
                          orch: "Optional[data.Orchestrator]") -> None:
    """Stamp each node with the git ref its orchestrator pins for it (if any),
    so the clone phase checks it out. A ref attaches to the component's own
    checkout, never to a subpackage (which rides its parent's checkout)."""
    if orch is None or not orch.component_refs:
        return
    for n in nodes:
        if n.ref_pinned:          # explicit CLI ref wins over the orchestrator
            continue
        ref = orch.component_refs.get(n.name)
        if ref and n.clone:
            n.ref = ref


def _apply_manifest_refs(nodes: List[Node], project) -> None:
    """Override node refs with the checkout manifest's own pins (``name:ref`` in
    ``deps``). The manifest is authoritative over the orchestrator default — a
    package owner edits it to choose the ref to build — so it wins over the
    default already on the node; bare manifest entries pin nothing and leave that
    default in place. An explicit CLI ref (``ref_pinned``) outranks even the
    manifest and is left untouched here.

    Call this once ``project`` (and thus the on-disk manifest) is known: in
    ``config``/``upgrade`` after ``find_project``, and in ``install`` only after
    the primary is cloned (its committed manifest does not exist until then)."""
    if project is None:
        return
    refs = context.manifest_refs(project)
    if not refs:
        return
    for n in nodes:
        if n.ref_pinned:          # explicit CLI ref wins over the manifest
            continue
        ref = refs.get(n.name)
        if ref and n.clone:
            n.ref = ref


def _apply_nested_manifest_refs(plan: "Plan", root: Path, container: Node) -> None:
    """Apply a container checkout's own manifest pins to the deps nested inside it.

    Manifest resolution is recursive: the top primary's manifest governs its
    root-level components (``_apply_manifest_refs``), and each of those in turn
    governs the refs of the deps nested in *its* checkout — so a pin propagates
    all the way down (climber-x's manifest -> yelmo, yelmo's manifest ->
    FastHydrology, ...). This reads ``container``'s on-disk
    ``.configme/manifest.toml`` and stamps the refs it pins onto ``container``'s
    nested children.

    Only nested children (``parent == container.name``, own checkout) are
    touched — a shared root-level dep (e.g. fesm-utils, symlinked not nested)
    stays governed by the top orchestrator, and a subpackage (rides its parent's
    checkout, ``clone = False``) has no ref of its own. A CLI ref
    (``ref_pinned``) still outranks the manifest. Applied *after* the primary
    manifest pass, so a nearer container overrides a farther one on the same dep.

    Call once ``container`` is on disk: after its clone in ``install`` (its nested
    children are ordered after it, so their clones still see the pin), or upfront
    in ``config``/``upgrade`` where every checkout already exists. A container
    with no manifest or no nested children is a no-op."""
    children = [n for n in plan.nodes if n.parent == container.name and n.clone]
    if not children:
        return
    mf = dest_of(container, plan, root) / ".configme" / "manifest.toml"
    refs = context.read_manifest_refs(mf)
    if not refs:
        return
    for n in children:
        if n.ref_pinned:          # explicit CLI ref wins over the manifest
            continue
        ref = refs.get(n.name)
        if ref:
            n.ref = ref


def _apply_manifest_refs_recursive(plan: "Plan", root: Path, project) -> None:
    """Full manifest ref resolution for a fully on-disk checkout: the primary's
    manifest pins its root-level components, then every container's own manifest
    pins the deps nested inside it, down each level (``_apply_nested_manifest_refs``).

    Used by ``config``/``upgrade``, where all checkouts already exist. ``install``
    resolves the primary the same way but applies each container's nested pins
    inline as it clones them (a container is not on disk until then)."""
    _apply_manifest_refs(plan.nodes, project)
    for node in plan.nodes:
        _apply_nested_manifest_refs(plan, root, node)


# The ref sentinel a package/orchestrator uses to say "resolve my ref from the
# component's per-machine `machine_refs` map, using the machine being built for".
# It flows through the normal ref-precedence chain like any other ref, so an
# explicit CLI or manifest pin still overrides it; only when it survives to clone
# time does ``_apply_machine_refs`` turn it into a concrete branch.
MACHINE_REF_SENTINEL = "@machine"


def _apply_machine_refs(nodes: List[Node], machine: Optional[str]) -> None:
    """Resolve the ``@machine`` ref sentinel to a per-machine branch, and warn
    when a machine-dependent component lands somewhere without a known branch.

    A package with per-host precompiled artifacts (e.g. climber-x's vilma) pins
    its ref to ``@machine`` (in the orchestrator's component list); this pass —
    run once the machine is resolved and after every manifest override — looks
    the machine up in the package's ``machine_refs`` map:

    - **machine recognised** -> check out its branch, and say so (the per-HPC
      dependence is non-obvious, so the notice names it);
    - **machine unrecognised** -> fall back to the ``"*"`` wildcard branch if the
      map has one, else the repo's default branch, and *warn* that the artifacts
      are per-HPC and may need a machine-specific branch built by hand;
    - **an explicit pin already won** (the node carries a concrete ref, not the
      sentinel, because the CLI or manifest overrode it) -> leave it, but note
      that a machine-specific branch exists so the override is a deliberate,
      visible choice.

    Idempotent: a node whose ref is neither the sentinel nor machine-mapped is
    untouched, so re-running config/upgrade is a no-op."""
    pkgs = data.packages()
    for n in nodes:
        pkg = pkgs.get(n.name)
        if pkg is None or not pkg.machine_refs:
            continue
        mrefs = pkg.machine_refs
        known = ", ".join(k for k in mrefs if k != "*") or "(none)"
        if n.ref == MACHINE_REF_SENTINEL:
            if machine is not None and machine in mrefs:
                n.ref = mrefs[machine]
                color.cprint(
                    f"  {n.name}: selected '{n.ref}' branch for machine "
                    f"'{machine}' (ships per-HPC precompiled libraries).")
            else:
                n.ref = mrefs.get("*")  # None -> the repo's default branch
                target = f"'{n.ref}' branch" if n.ref else "the repo default branch"
                color.cprint(
                    f"  ~ {n.name}: ships per-HPC precompiled libraries but "
                    f"machine '{machine or 'unknown'}' has no known branch — "
                    f"staying on {target}. You may need a branch built for this "
                    f"machine (known: {known}).")
        elif n.ref and machine in mrefs and mrefs[machine] != n.ref:
            color.cprint(
                f"  {n.name}: using pinned ref '{n.ref}'; a machine-specific "
                f"branch ('{mrefs[machine]}') exists for '{machine}' but your "
                f"explicit pin takes precedence.")


def _apply_nesting(nodes: List[Node]) -> None:
    """Anchor a dependency that its consumer nests inside its own checkout.

    A package can flag a dependency link with ``nest = true`` to mean "clone
    this dependency inside my checkout at ``path``, not at the root" (e.g.
    yelmo's FastHydrology). This stamps the dependency node with that consumer
    as its ``parent`` and the link path as its ``subdir``, so ``dest_of`` anchors
    it under the consumer in every orchestrator.

    The scan is over *all* known packages, not just the planned consumer's node,
    so the dependency still nests when its consumer is on disk but absent from
    this plan — e.g. ``configme install FastHydrology`` inside an existing yelmox
    places it under yelmo (``dest_of`` then resolves yelmo's path via the
    orchestrator). When no consumer and no orchestrator apply (a standalone
    ``configme install FastHydrology``, where it is the primary), it falls back
    to a root clone. An existing parent (a real subpackage) is never overridden."""
    by_name = {n.name: n for n in nodes}
    for pkg in data.packages().values():
        for link in pkg.links:
            if not link.nest:
                continue
            dep = by_name.get(link.dep)
            if dep is not None and dep.parent is None:
                dep.parent = pkg.name
                dep.subdir = link.path


def _finalize_plan(primary: Node, nodes: List[Node], explicit: bool,
                   orch: "Optional[data.Orchestrator]") -> Plan:
    """Assemble the Plan and apply the shared post-passes every install/config
    path needs: nest dependencies inside their consumer (``_apply_nesting``),
    then reorder so a nested checkout follows its container
    (``_order_nested_after_container``)."""
    _apply_nesting(nodes)
    plan = Plan(primary, nodes, explicit, orch)
    _order_nested_after_container(plan)
    return plan


def build_plan(target: str, *, only: bool = False) -> Plan:
    """Resolve a target string into an ordered install/config Plan.

    Grammar (shared by ``configme install`` and ``configme config``):
      * ``yelmox``        orchestrator + its full default set (expanded)
      * ``yelmo``         a package + the sub-packages it needs (auto-resolved)
      * ``yelmox+yelmo``  exactly those, no expansion / auto-resolution
      * ``only=True``     exactly the named target — no orchestrator expansion
                          and no dependency resolution (the ``--only`` flag)

    Any name (the primary or a ``+`` slot) may carry a ``:ref`` pin —
    ``yelmox:dev``, ``climber-x:alex-dev``, ``yelmox:dev+yelmo:foo``. The bare
    name resolves the node; the ref is checked out right after clone (and
    reconciled by ``config``/``upgrade``). A CLI ref is authoritative — it wins
    over both the orchestrator default and the checkout's manifest pin.
    """
    orchs = data.orchestrators()
    pkgs = data.packages()

    if "+" in target:
        specs = [t for t in target.split("+") if t]
        # A slot may carry a `:ref` pin (`yelmox:dev+yelmo:foo`); match on the
        # bare name and let `_node_for_spec` stamp the ref (CLI-pinned, so it
        # outranks the manifest/orchestrator later).
        names = [data.split_ref(s)[0] for s in specs]
        # The primary is the (first) orchestrator in the list; every other slot
        # resolves as a package so a dual-registered name (orchestrator + package,
        # e.g. FastEarth3D) installs as a component, not a nested orchestrator.
        primary_name = next((n for n in names if n in orchs), names[0])
        nodes = [_node_for_spec(s, prefer_package=(nm != primary_name))
                 for s, nm in zip(specs, names)]
        primary = next(n for n in nodes if n.name == primary_name)
        orch = orchs.get(primary.name) if primary.is_orchestrator else None
        expanded = _with_subpackages(nodes)
        _apply_component_refs(expanded, orch)
        return _finalize_plan(primary, expanded, True, orch)

    primary = _node_for_spec(target)

    if only:
        # Exactly the named target: no deps. Subpackages still ride along (they
        # are part of the same checkout, not separate dependencies). Marked
        # explicit so the link phase treats any deps as pending.
        orch = orchs.get(primary.name) if primary.is_orchestrator else None
        return _finalize_plan(primary, _with_subpackages([primary]), True, orch)

    if primary.is_orchestrator:
        orch = orchs[primary.name]
        order = []
        # components (resolve each component's own deps too), then orchestrator
        for comp in orch.default_packages:
            _resolve_deps(comp, pkgs, order)
        # Optional components (e.g. private bgc/vilma): resolved after the
        # required set, deduped against it, and flagged so a clone failure is a
        # soft skip rather than a hard failure.
        opt_order: List[str] = []
        for comp in orch.optional_packages:
            _resolve_deps(comp, pkgs, opt_order)
        opt_only = [n for n in opt_order if n not in order]
        # Components resolve as packages: a dual-registered name (e.g.
        # FastEarth3D, also a standalone orchestrator) clones as a component.
        comp_nodes = [_node_for(n, prefer_package=True) for n in order]
        opt_names = set(orch.optional_packages)
        for n in opt_only:
            node = _node_for(n, prefer_package=True)
            if n in opt_names:
                node.clone_policy = "optional"
            comp_nodes.append(node)
        # Data/auxiliary repos: clone-only, outside the build graph. No
        # `_resolve_deps` (they pull in nothing) and they keep their own
        # clone_policy (e.g. "prompt" for a large data repo). They ride the
        # normal node pipeline so pull/status/--repos cover them for free.
        data_nodes = [_node_for(n, prefer_package=True) for n in orch.data_packages]
        nodes = comp_nodes + data_nodes + [primary]
        expanded = _with_subpackages(nodes)
        _apply_component_refs(expanded, orch)
        return _finalize_plan(primary, expanded, False, orch)

    # single package: its deps (auto) then itself
    order: List[str] = []
    _resolve_deps(primary.name, pkgs, order)
    nodes = [_node_for(n) for n in order]
    return _finalize_plan(primary, _with_subpackages(nodes), False, None)


# --------------------------------------------------------------- runner

def _git_head(dest: Path) -> Optional[str]:
    """Current commit of the checkout at ``dest`` (None if not a git repo)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest,
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None


def _git_branch(dest: Path) -> Optional[str]:
    """Current branch name of the checkout (None if not a git repo or if HEAD is
    detached). Used to tell whether a checkout already sits on a pinned ref."""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=dest, check=True, capture_output=True, text=True)
        name = out.stdout.strip()
        return name if name and name != "HEAD" else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _git_dirty(dest: Path) -> bool:
    """True if the checkout has uncommitted changes to *tracked* files. Untracked
    files are ignored on purpose: configme writes generated Makefiles into each
    checkout, and those should not block an otherwise-clean upgrade."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=dest, check=True, capture_output=True, text=True)
        return bool(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError):
        return False


@dataclass
class Runner:
    download: str = "ssh"           # ssh | https | no
    dry_run: bool = False
    overwrite: bool = False
    log: List[str] = field(default_factory=list)

    def emit(self, line: str) -> None:
        self.log.append(line)

    def clone_url(self, node: Node) -> str:
        # A node may pin its transport with `protocol = "https"/"ssh"` (e.g. a
        # GitLab repo on a host where only HTTPS login works), overriding the
        # run's download mode. `-d no` (use existing checkout) is never
        # overridden — there is nothing to clone.
        transport = self.download
        if node.protocol and transport != "no":
            transport = node.protocol
        return build_clone_url(node.host, node.org, node.repo, transport)

    def clone(self, node: Node, dest: Path) -> str:
        """Clone ``node`` to ``dest`` and, if it pins a ref, check it out.

        When ``dest`` already exists and the node pins a ref, the ref is still
        enforced (see ``ensure_ref``) so the pin is authoritative on every
        install, not just the first clone.

        Returns a status string:
        cloned | exists | switched | dirty | present(no) | dry."""
        if self.download == "no":
            if not (dest.exists() or dest.is_symlink()):
                raise InstallError(
                    f"{node.name}: -d no requires {dest} to exist already")
            self.emit(f"# {node.name}: using existing {dest}")
            return "present(no)"

        url = self.clone_url(node)
        if self.overwrite and (dest.exists() or dest.is_symlink()) and not self.dry_run:
            outdated = dest.parent / "outdated-repos"
            outdated.mkdir(exist_ok=True)
            shutil.move(str(dest), str(outdated / dest.name))

        self.emit(f"git clone {url} {dest}")
        if node.ref:
            self.emit(f"(cd {dest} && git checkout {node.ref})")
        if node.submodules:
            self.emit(f"(cd {dest} && git submodule update --init --recursive)")
        if dest.exists() and not self.overwrite:
            # Existing checkout: enforce a pinned ref (read-only probing in
            # dry-run; ensure_ref skips the actual checkout when dry_run).
            return self.ensure_ref(node, dest) if node.ref else "exists"
        if self.dry_run:
            return "dry"
        subprocess.run(["git", "clone", url, str(dest)], check=True)
        if node.ref:
            subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True)
        if node.submodules:
            subprocess.run(["git", "submodule", "update", "--init", "--recursive"],
                           cwd=dest, check=True)
        return "cloned"

    def ensure_ref(self, node: "Node", dest: Path, *, confirm_fn=None) -> str:
        """Bring an existing checkout onto its resolved ref (e.g. yelmo's
        ``climber-x`` branch). Returns: exists | switched | dirty | declined.

        Already on the ref -> ``exists``. Uncommitted tracked changes -> the
        switch is skipped and reported (never clobbered), returning ``dirty``.

        With no ``confirm_fn`` the switch is automatic (the ``install`` clone
        phase: a freshly cloned/just-present tree has nothing to lose). With one
        — the ``config``/``upgrade`` reconcile paths, where a working copy and a
        possibly hand-edited manifest already exist — a clean branch mismatch is
        offered as ``switch <pkg> from <cur> to <ref>?``; a declined prompt
        leaves the checkout untouched (``declined``).

        The switch itself tries a plain ``git checkout`` first (offline-friendly
        when the ref is already fetched) and, only if that fails, a ``git fetch``
        + retry covers a ref created after the original clone."""
        current = _git_branch(dest)
        if current == node.ref:
            return "exists"
        if _git_dirty(dest):
            self.emit(f"# {node.name}: uncommitted changes at {dest}; "
                      f"cannot switch to {node.ref} (skipped)")
            return "dirty"
        self.emit(f"(cd {dest} && git checkout {node.ref})")
        if self.dry_run:
            return "switched"
        if confirm_fn is not None and not confirm_fn(
                f"switch {node.name} from {current or 'detached HEAD'} "
                f"to {node.ref}?"):
            return "declined"
        try:
            subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True,
                           capture_output=True, text=True)
        except (subprocess.CalledProcessError, OSError):
            self.emit(f"(cd {dest} && git fetch && git checkout {node.ref})")
            try:
                subprocess.run(["git", "fetch"], cwd=dest, check=True)
                subprocess.run(["git", "checkout", node.ref], cwd=dest, check=True)
            except (subprocess.CalledProcessError, OSError) as e:
                raise InstallError(
                    f"{node.name}: could not switch {dest} to ref {node.ref} "
                    f"({e}); switch it by hand or re-clone with --overwrite.")
        return "switched"

    def pull(self, node: "Node", dest: Path) -> Tuple[str, bool]:
        """Update an existing checkout at ``dest`` in place. Returns
        ``(status, updated)`` where status is one of: missing | dirty | detached
        | dry | up-to-date | updated, and ``updated`` is True only when the pull
        advanced HEAD (new commits).

        Uses ``git pull --ff-only`` on whatever branch is checked out: a clean
        fast-forward succeeds, anything else (diverged branch, no upstream) is
        reported for the user to resolve by hand — never merged or clobbered.
        A checkout with uncommitted tracked changes is skipped, not touched.
        A detached HEAD (pinned tag or commit) is skipped: there is no branch
        to fast-forward, and the pin is exactly what the user asked for."""
        name = node.name
        if not dest.is_dir():
            self.emit(f"# {name}: not present at {dest} (skipped)")
            return "missing", False
        if _git_dirty(dest):
            self.emit(f"# {name}: uncommitted changes at {dest} (skipped)")
            return "dirty", False
        if _git_branch(dest) is None:
            self.emit(f"# {name}: detached HEAD at {dest} "
                      f"(pinned ref; no pull)")
            return "detached", False
        self.emit(f"(cd {dest} && git pull --ff-only)")
        if self.dry_run:
            return "dry", False
        before = _git_head(dest)
        try:
            subprocess.run(["git", "pull", "--ff-only"], cwd=dest, check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            raise InstallError(
                f"git pull --ff-only failed in {dest} ({e}); resolve manually "
                f"(diverged branch, missing upstream, or network).")
        return ("updated", True) if _git_head(dest) != before else ("up-to-date", False)

    def link(self, link_path: Path, target: Path) -> str:
        rel = os.path.relpath(target, link_path.parent)
        self.emit(f"ln -s {rel} {link_path}")
        if self.dry_run:
            return "dry"
        if link_path.is_symlink() or link_path.exists():
            return "exists"
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(rel)
        return "linked"

    def link_external(self, node: Node, dest: Path, target: Path) -> str:
        """Symlink ``dest`` to an external, user-managed checkout at ``target``.

        Used in place of ``clone`` when ``--link`` (or ``links.toml``) routes
        ``node`` at an existing on-disk location. The symlink is absolute (the
        user gave an absolute path; making it relative would obscure that).

        Returns: linked | exists | dry. A pre-existing different ``dest`` is a
        hard error unless ``overwrite=True``, in which case it is moved to
        ``outdated-repos/`` exactly like the clone path. A self-referential
        ``target`` (one whose inode is ``dest.parent`` itself, e.g. when an
        APFS case-insensitive path makes ``/x/Foo`` and ``/x/foo`` indistinct)
        is rejected — otherwise we would silently create a no-op loop
        ``dest -> dest.parent``."""
        target = Path(target).expanduser().resolve()
        # samefile() compares inodes, so it catches case-insensitive collisions
        # (`/path/Foo` vs `/path/foo`) that plain string compare on resolve()
        # misses on APFS / HFS+.
        try:
            self_loop = dest.parent.exists() and target.samefile(dest.parent)
        except OSError:
            self_loop = False
        if self_loop:
            raise InstallError(
                f"{node.name}: refusing self-referential link "
                f"{dest} -> {target} (target is the link's own parent "
                f"directory). This usually means a --link/links.toml entry "
                f"points at the install root itself.")
        self.emit(f"# {node.name}: link to existing checkout at {target}")
        self.emit(f"ln -s {target} {dest}")
        if self.dry_run:
            return "dry"
        if dest.is_symlink():
            try:
                if Path(os.readlink(dest)) == target:
                    return "exists"
            except OSError:
                pass
        if dest.exists() or dest.is_symlink():
            if self.overwrite:
                outdated = dest.parent / "outdated-repos"
                outdated.mkdir(exist_ok=True)
                shutil.move(str(dest), str(outdated / dest.name))
            else:
                raise InstallError(
                    f"{node.name}: {dest} already exists; refusing to replace "
                    f"with link to {target}. Move it aside or re-run with "
                    f"--overwrite.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target)
        return "linked"


# --------------------------------------------------------------- main entry

def configure_makefile(*, label, pkg_name, dest, config_subdir, is_orchestrator,
                       machine, compiler, machine_path, compiler_path,
                       dry_run, results) -> None:
    """Generate one Makefile (modern common.mk, overlay, or legacy fallback),
    printing progress and recording the outcome in `results`. `label` is the
    display name (e.g. 'yelmo' or 'fesm-utils/utils'); `pkg_name` keys overlay
    lookup.

    Shared by `configme install` and `configme config` — the single Makefile
    generation code path."""
    cfgroot = dest / config_subdir if config_subdir else dest
    if dry_run:
        color.cprint(f"  configure {label}: (dry) would generate Makefile")
        return
    if not cfgroot.is_dir():
        color.cprint(f"  - {label}: not present; skipping configure")
        results["skipped"].append(label)
        return
    try:
        copied = generate.ensure_common(pkg_name, cfgroot)
        if copied is not None:
            color.cprint(f"  ~ {label}: copied configme-provided common.mk -> {copied}")
        if generate.has_common(cfgroot):
            out = generate.generate_makefile(dest, machine, compiler,
                                             machine_path, compiler_path, config_subdir)
            color.cprint(f"  + configure {label}: {out}")
            results["configured"].append(label)
        elif generate.legacy_flat_config(cfgroot, machine, compiler):
            out = generate.legacy_makefile(dest, machine, compiler, config_subdir)
            color.cprint(f"  + configure {label}: {out} (LEGACY flat config)")
            results["configured"].append(label)
        elif is_orchestrator:
            out = generate.generate_makefile(dest, machine, compiler,
                                             machine_path, compiler_path, config_subdir)
            color.cprint(f"  + configure {label}: {out} (no common.mk)")
            results["configured"].append(label)
        else:
            color.cprint(f"  - {label}: no common.mk and no legacy config "
                  f"'{machine}_{compiler}'; skipping")
            results["skipped"].append(label)
    except (generate.GenerateError, netcdf.NetcdfError) as e:
        color.cprint(f"  ! {label}: {e}")
        results["failed"].append(label)


def _primary_present(root: Path, plan: Plan) -> bool:
    """True if ``root`` holds a real checkout of the primary, judged by the
    presence of its Makefile template — the one file configme must get from the
    checkout (see generate.generate_makefile / legacy_makefile).

    A matching directory name or a bare ``.configme/`` dir is deliberately NOT
    enough: configme writes ``.configme/`` itself, so a stub from an aborted run
    — or an incomplete download with subdirs but no template — would otherwise be
    mistaken for a finished checkout and silently skip the clone."""
    if not root.is_dir():
        return False
    sub = plan.primary.config_subdir
    cfgroot = root / sub if sub else root
    return generate.find_repo_file(cfgroot, "Makefile") is not None


def root_for(plan: Plan, install_dir: Optional[str], cwd: Path) -> Tuple[Path, bool]:
    """Return (root, primary_present). root holds the primary; components are
    sub-dirs of it. Default root is within the orchestrator (``yelmox/``) or the
    named package (``yelmo/``, with its deps as ``yelmo/fesm-utils``).

    ``primary_present`` reflects whether a real checkout already lives at root
    (see _primary_present) — not merely whether the directory exists or is named
    after the primary."""
    if install_dir:
        root = Path(install_dir).expanduser().resolve()
        return root, _primary_present(root, plan)
    # In or pointing at an existing primary checkout? Two signals:
    #  * the directory is named after the primary (pre-manifest case), or
    #  * cwd's manifest names this same primary.
    # A bare ``.configme/`` on its own is NOT enough: cwd may be a *different*
    # configme-managed project (e.g. an orchestrator like yelmox) and we are
    # installing a component into it — that component belongs under
    # ``cwd / plan.primary.dir``, not at cwd itself.
    if cwd.name == plan.primary.dir or context._manifest_primary(cwd) == plan.primary.name:
        return cwd, _primary_present(cwd, plan)
    root = (cwd / plan.primary.dir).resolve()
    return root, _primary_present(root, plan)


def _host_orchestrator_for(target_name: str,
                           cwd: Path) -> "Optional[data.Orchestrator]":
    """Return the orchestrator whose checkout sits at ``cwd``, when that
    orchestrator claims ``target_name`` as one of its (default or optional)
    component packages. Else ``None``.

    Used by ``run_install`` to spot the "ran `configme install <pkg>` from
    inside an orchestrator that already manages <pkg>" case, so the user can
    be offered to install <pkg> *as a component of the orchestrator* (sharing
    its already-cloned deps) instead of side-by-side with its own fresh deps.
    Returns ``None`` when ``cwd`` has no manifest, names a non-orchestrator,
    or the target is the orchestrator itself."""
    try:
        primary = context._manifest_primary(cwd)
    except context.ProjectError:
        return None
    if primary is None:
        return None
    orch = data.orchestrators().get(primary)
    if orch is None:
        return None
    if target_name == orch.name:
        return None
    if target_name in orch.default_packages or target_name in orch.optional_packages:
        return orch
    return None


def dest_of(node: Node, plan: Plan, root: Path) -> Path:
    if node.name == plan.primary.name:
        return root
    # A subpackage (or a nested dependency) lives inside its parent's checkout,
    # so anchor it to the parent's dest (which already accounts for orchestrator
    # component_paths).
    if node.parent is not None:
        parent = next((n for n in plan.nodes if n.name == node.parent), None)
        if parent is not None:
            return dest_of(parent, plan, root) / node.subdir
        # Parent not in this plan (e.g. installing only a nested dependency into
        # an existing orchestrator): resolve the parent's location from the
        # orchestrator so the dependency still lands inside it.
        if plan.orchestrator is not None:
            pkgs = data.packages()
            parent_dir = pkgs[node.parent].dir if node.parent in pkgs else node.parent
            parent_dir = plan.orchestrator.component_paths.get(node.parent, parent_dir)
            return root / parent_dir / node.subdir
    if plan.orchestrator is not None:
        return root / plan.orchestrator.component_paths.get(node.name, node.dir)
    return root / node.dir


def _order_nested_after_container(plan: "Plan") -> None:
    """Reorder ``plan.nodes`` so a component cloned *inside* another component's
    checkout is cloned/configured *after* that container.

    Dependency resolution (``_resolve_deps``) orders deps before dependents, so
    a dependency nested inside its consumer's checkout (e.g. FastHydrology at
    ``yelmo/FastHydrology`` via a ``nest`` link, see ``_apply_nesting``) would
    otherwise come *before* its container. Cloning it first creates a non-empty
    ``yelmo/`` that then blocks yelmo's own clone. configme
    never compiles here (the config step only writes Makefiles), so there is no
    real dep-order constraint between the two — the only hard requirement is
    that a containing checkout exist before anything nested in it.

    This stable topological pass enforces exactly that and leaves every
    unrelated pair in its original (dependency) order. It is a no-op for
    subpackages (already inserted right after their parent by
    ``_with_subpackages``) and for components that nest in nobody (bgc/vilma sit
    in the primary's tree, which is cloned first and excluded here)."""
    root = Path("/")
    rel = {n.name: dest_of(n, plan, root) for n in plan.nodes}

    def is_container(outer: Node, inner: Node) -> bool:
        # The root (primary checkout) is a parent of everything but is cloned
        # first and handled separately, so it must not force an ordering here.
        outer_dest = rel[outer.name]
        return outer_dest != root and outer_dest in rel[inner.name].parents

    remaining = list(plan.nodes)
    ordered: List[Node] = []
    while remaining:
        for i, n in enumerate(remaining):
            if not any(is_container(m, n) for m in remaining if m is not n):
                ordered.append(remaining.pop(i))
                break
        else:  # pragma: no cover - unreachable unless dest paths form a cycle
            ordered.extend(remaining)
            break
    plan.nodes = ordered


def _stamp_note(path: Path, machine: str, compiler: str) -> str:
    """Read the build stamp at ``path`` and return a short ``  (! note)`` suffix
    when the stamp disagrees with the current (machine, compiler), else "".

    Purely informational — a linked external is the user's responsibility, so
    a mismatch is a warning, not a refusal."""
    stamp = links_mod.read_build_stamp(path)
    if stamp is None:
        return ""
    note = links_mod.stamp_mismatch(stamp, machine=machine, compiler=compiler)
    return f"  (! {note})" if note else ""


def _resolve_links(plan: "Plan", root: Path, link_args: List[str],
                   confirm_fn) -> Dict[str, Path]:
    """Resolve the ``--link``/``links.toml`` map for an install/upgrade run.

    Walks the three tiers (CLI > project > global), validates each entry
    points at an existing dir of a known package, then prompts per file-tier
    entry (CLI entries are silent — explicit). Returns the confirmed map.

    Failures are surfaced as ``InstallError`` so the caller bails before the
    clone phase: a half-resolved link map would leave dangling work behind."""
    cli_links = links_mod.parse_link_args(link_args)
    global_links, project_links = links_mod.load_file_links(root)
    merged = links_mod.merge_links(global_links, project_links, cli_links)
    if not merged:
        return {}
    known = [n.name for n in plan.nodes]
    try:
        links_mod.validate_links(merged, known)
    except links_mod.LinkError as e:
        raise InstallError(str(e)) from e
    resolved = links_mod.confirm_file_links(merged, confirm_fn)
    return resolved


def run_install(target: str, *, download: str, install_dir: Optional[str],
                machine: Optional[str], compiler: Optional[str],
                overwrite: bool, build_deps: bool, dry_run: bool,
                only: bool = False,
                link_args: Optional[List[str]] = None,
                select_fn=None, ask_fn=None, confirm_fn=None) -> int:
    cwd = Path.cwd()
    plan = build_plan(target, only=only)           # fail-fast: unknown names
    if not plan.primary.clone:
        parent = _parent_of(plan.primary.name)
        hint = (f"run `configme install {parent}`" if parent
                else "install its parent package")
        raise InstallError(
            f"{plan.primary.name} is a component of another package's checkout "
            f"and cannot be installed on its own; {hint}. (To (re)generate its "
            f"Makefile use `configme config {plan.primary.name}`.)")

    # `configme install <pkg>` from inside an orchestrator that owns <pkg>:
    # the default plan would install <pkg> side-by-side with the orchestrator,
    # cloning its own fresh copies of deps (e.g. a second fesm-utils tree
    # under FastHydrology/). Offer to install <pkg> as a component of the host
    # orchestrator instead, so it shares the deps the orchestrator already
    # provides. Skipped when the user has already been explicit about scope
    # (`--only`, `--dir`, or a `+`-literal target).
    # The target may itself be a standalone orchestrator that the host also
    # manages as a component (e.g. FastEarth3D inside climber-x): the same prompt
    # applies, so the gate does not exclude orchestrator primaries —
    # `_host_orchestrator_for` already returns None when the target *is* the host.
    if (not only and "+" not in target and install_dir is None
            and confirm_fn is not None):
        host = _host_orchestrator_for(plan.primary.name, cwd)
        if host is not None:
            question = (
                f"You are inside the {host.name} install, which manages "
                f"{plan.primary.name} as a component. Install it as a "
                f"{host.name} component (share existing deps)?")
            if confirm_fn(question, True):
                # Preserve an explicit CLI ref (`configme install yelmo:foo`
                # inside yelmox) so the rewrite to a host component keeps it.
                comp = plan.primary.name + (
                    f":{plan.primary.ref}" if plan.primary.ref else "")
                target = f"{host.name}+{comp}"
                plan = build_plan(target)

    root, primary_present = root_for(plan, install_dir, cwd)

    runner = Runner(download=download, dry_run=dry_run, overwrite=overwrite)
    runner.emit("#!/usr/bin/env bash")
    runner.emit("# Generated by `configme install` — reproduces this install.")
    runner.emit("set -euo pipefail")

    header = color.warn("DRY RUN — no changes will be made.") + "\n" if dry_run else ""
    color.cprint(header + color.header(f"configme install {target}"))
    color.cprint(f"  root: {root}")
    color.cprint(f"  packages: {', '.join(n.name for n in plan.nodes)}"
          f"{' (literal, no auto-resolve)' if plan.explicit else ''}")

    # Resolve --link / links.toml *before* any clone: a confirmed entry
    # short-circuits the clone for that package (symlink instead). Fails fast
    # on a bad path or unknown package so we never half-clone the rest. The
    # per-package stamp notes need the machine/compiler resolved below, so the
    # confirmed map is printed after selection.
    link_map = _resolve_links(plan, root, link_args or [], confirm_fn)

    results = {"cloned": [], "configured": [], "built": [], "linked": [],
               "pending": [], "deferred": [], "skipped": [],
               "unavailable": [], "failed": []}
    # Commands the user must run later (a declined opt-in clone, a pending link's
    # missing dependency, a deferred build, then any deferred extras) are
    # collected here and echoed together in the summary's "Deferred" section.
    followups: List[str] = []

    # Linking the primary makes no sense — that's what --dir is for. Reject
    # explicitly rather than silently doing the wrong thing.
    if plan.primary.name in link_map:
        raise InstallError(
            f"--link {plan.primary.name}=...: cannot link the install's primary "
            f"package. Use --dir {link_map[plan.primary.name]} instead.")

    # --- clone the primary first, before resolving machine/compiler. Cloning it
    # up front exposes the checkout's own project-tier `.configme/` (config.toml
    # defaults, machine/compiler fragments, a stored hpc account) to the
    # selection prompt below, and lets the fast orchestrator-level extras (pip
    # runme, runme config, data links) run while the user is still at the
    # keyboard — before the slow component clones and build. Target-name
    # validation already happened in build_plan(); machine/compiler are still
    # validated at selection, just after this cheap, non-destructive clone.
    runner.emit("\n# --- clone primary ---")
    if primary_present and not overwrite:
        runner.emit(f"# {plan.primary.name}: present at {root}")
        color.cprint(f"  {plan.primary.name}: using existing {root}")
    elif download == "no":
        # -d no means "configure what's already on disk" — but there is no real
        # checkout here (no Makefile template found). Fail fast rather than write
        # a misleading .configme stub over an empty/partial directory.
        color.cprint(f"  ! {plan.primary.name}: no checkout found at {root} "
              f"(no Makefile template under config/ or .configme/); "
              f"-d no cannot configure it. Clone it (drop -d no) or point "
              f"--dir at an existing checkout.")
        results["failed"].append(plan.primary.name)
        color.cprint("\nSummary:\n  failed: " + plan.primary.name)
        return 1
    else:
        try:
            st = runner.clone(plan.primary, root)
            ref_note = f" (ref {plan.primary.ref})" if plan.primary.ref else ""
            color.cprint(f"  clone {plan.primary.name}: {st}{ref_note}")
        except (InstallError, subprocess.CalledProcessError) as e:
            color.cprint(f"  ! {plan.primary.name}: {e}")
            results["failed"].append(plan.primary.name)

    # The primary is now on disk, so its committed `.configme/` (manifest,
    # config, fragments) can finally be read. Apply the manifest's component ref
    # pins (they override the orchestrator defaults already on the dep nodes,
    # before those deps are cloned below), then resolve machine/compiler using
    # the project tier the checkout provides. (On a fresh repo with no committed
    # manifest this is a no-op and orchestrator/user tiers govern.)
    project = context.find_project(root) if root.exists() else None
    _apply_manifest_refs(plan.nodes, project)

    # --- selection (machine/compiler, + the runme hpc account) ---
    machine, compiler = context.resolve_selection(machine, compiler, project, select_fn)
    machine_path, machine_tier = context.resolve_fragment("machine", machine, project)
    compiler_path, compiler_tier = context.resolve_fragment("compiler", compiler, project)
    runner.emit(f"# target={target}  machine={machine}  compiler={compiler}")
    color.cprint(f"  machine={machine}  compiler={compiler}  download={download}")
    # Now the machine is known, resolve any ``@machine`` ref sentinel to the
    # component's per-host branch (and warn on an unrecognised machine) before
    # the component clones below check the ref out.
    _apply_machine_refs(plan.nodes, machine)
    if link_map:
        color.cprint("  links:")
        for pkg, path in link_map.items():
            note = _stamp_note(path, machine, compiler)
            color.cprint(f"    {pkg} -> {path}{note}")

    # Capture the runme hpc/account now, alongside machine/compiler, so the
    # runme_config extra (run just below — before the slow component clones and
    # build) reuses them instead of stopping to ask later. hpc defaults to the
    # machine name; a value already in config.toml is reused, not re-asked. Only
    # an orchestrator that seeds runme's config needs it.
    _ask = ask_fn or (lambda label, default=None, *, complete_paths=False: default)
    extras_cfg = context.load_config(project) if project else {}
    if (plan.orchestrator is not None
            and (plan.orchestrator.extras or {}).get("runme_config")):
        hpc, account = extras_mod.prompt_hpc_account(extras_cfg, _ask, machine=machine)
        # Seed both so run_extras' runme_config asks nothing (account may be ""
        # when skipped / non-interactive — a present key still suppresses the ask).
        extras_cfg = {**extras_cfg, "hpc": hpc,
                      "account": account if account is not None else ""}

    # --- extras (orchestrator post-config actions: pip tools, runme config, data
    # links). Run early — right after selection and before the component clones
    # and build — so every prompt they carry (data-link paths; the account was
    # taken above) is answered while the user is still here. Skipped when the
    # primary is not actually on disk (a failed clone leaves nothing to configure
    # into); dry-run still previews the steps.
    if (plan.orchestrator is not None and plan.orchestrator.extras
            and (dry_run or root.is_dir())):
        followups.extend(extras_mod.run_extras(plan.orchestrator, runner, root,
                                               extras_cfg, _ask, confirm_fn,
                                               machine=machine))

    # --- clone components ---
    runner.emit("\n# --- clone components ---")
    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        if not node.clone:
            # A contained component (e.g. fesm-utils/utils): it arrives with its
            # parent's checkout, so there is nothing to clone.
            runner.emit(f"# {node.name}: component of {node.parent} (not cloned)")
            continue
        dest = dest_of(node, plan, root)
        # Linked package: symlink to an existing on-disk checkout instead of
        # cloning. Skip ref pin / submodules — the linked target is the user's.
        if node.name in link_map:
            try:
                st = runner.link_external(node, dest, link_map[node.name])
                color.cprint(f"  link {node.name}: {st} -> {link_map[node.name]}")
                results["linked"].append(node.name)
            except InstallError as e:
                color.cprint(f"  ! {node.name}: {e}")
                results["failed"].append(node.name)
            continue
        # A `prompt` repo (large/expensive, e.g. a data repo) is opt-in: when it
        # is not already on disk, ask before cloning (default no). Declining
        # defers it — the clone is recorded as pending and its command echoed in
        # the summary. (Dry-run previews the clone instead of prompting.)
        already = dest.exists() or dest.is_symlink()
        if (node.clone_policy == "prompt" and not already and not dry_run
                and download != "no" and confirm_fn is not None
                and not confirm_fn(
                    f"Download {node.name}? (large repo, can take a while)",
                    False)):
            cmd = f"git clone {runner.clone_url(node)} {dest}"
            runner.emit(f"# {node.name}: opt-in clone declined; run later: {cmd}")
            color.cprint(f"  - {node.name}: not cloned (opt-in); clone later with:")
            color.cprint(f"      {cmd}")
            followups.append(cmd)
            results["pending"].append(node.name)
            continue
        try:
            st = runner.clone(node, dest)
            ref_note = f" (ref {node.ref})" if node.ref else ""
            color.cprint(f"  clone {node.name}: {st}{ref_note}")
        except (InstallError, subprocess.CalledProcessError) as e:
            if node.clone_policy == "optional":
                # Optional component (often a private repo): no access just means
                # this build variant is unavailable — note it and carry on.
                color.cprint(f"  - {node.name}: optional; not cloned ({e}); skipping")
                results["unavailable"].append(node.name)
            else:
                color.cprint(f"  ! {node.name}: {e}")
                results["failed"].append(node.name)
        # This checkout's own manifest pins the refs of any deps nested inside it
        # (e.g. yelmo -> FastHydrology). Apply them now that it is on disk: its
        # nested children are ordered after it, so they still clone onto the pin.
        _apply_nested_manifest_refs(plan, root, node)

    # --- manifest (make the checkout self-describing, independent of its dir
    # name so e.g. `--dir check` is still recognised by a later bare `configme`)
    deps = [n.name for n in plan.nodes if n.name != plan.primary.name]
    runner.emit("\n# --- manifest ---")
    runner.emit(f"mkdir -p {root}/.configme  # write .configme/manifest.toml")
    if dry_run:
        color.cprint(f"  manifest: (dry) would write {root}/.configme/manifest.toml "
              f"(package={plan.primary.name}, deps={deps})")
    elif root.exists():
        mf, created = context.write_manifest(root, plan.primary.name, deps)
        color.cprint(f"  + wrote {mf}" if created else f"  manifest: using existing {mf}")

    # --- link phase (non-primary packages only)
    runner.emit("\n# --- links ---")
    plan_names = {n.name for n in plan.nodes}
    pkgs_all = data.packages()
    present_dirs = {n.name: dest_of(n, plan, root) for n in plan.nodes}
    for node in plan.nodes:
        if node.name == plan.primary.name:
            continue
        node_root = dest_of(node, plan, root)
        # Don't fabricate links (or a stray package dir) for a package that
        # isn't actually present. In dry-run nothing is on disk, so still show
        # the intended links.
        if not dry_run and not node_root.exists():
            continue
        for link in node.links:
            # A nested dependency (``nest = true``) is *cloned inside* this
            # package's checkout at ``link.path`` (see ``_apply_nesting``), so
            # ``dest_of`` resolves it to exactly ``node_root / link.path`` —
            # the link path itself. Emitting a symlink here would produce a
            # pointless self-referential ``ln -s <path> <path>``. The directory
            # already lives where it belongs, so there is nothing to link.
            if link.nest:
                continue
            dep_dest = present_dirs.get(link.dep)
            if dep_dest is None and link.dep in pkgs_all:
                sub = (plan.orchestrator.component_paths.get(
                       link.dep, pkgs_all[link.dep].dir)
                       if plan.orchestrator else pkgs_all[link.dep].dir)
                dep_dest = root / sub
            link_path = node_root / link.path
            dep_present = dep_dest is not None and dep_dest.exists()
            # A dependency not in this install set and not already on disk is
            # "pending": create the link anyway (so a later `configme install
            # <dep>` just works), but warn. Common in '+'-literal mode.
            if link.dep not in plan_names and not dep_present:
                if dep_dest is not None:
                    runner.link(link_path, dep_dest)
                color.cprint(f"  ! {node.name}: dependency '{link.dep}' not in this "
                      f"install; link {link.path} created but pending")
                results["pending"].append(f"{node.name}->{link.dep}")
                # Surface the fix in the summary's deferred list: installing the
                # missing dependency resolves every pending link onto it, so
                # dedupe the hint per dependency. Skipped on a dry run, matching
                # the deferred-build/extras hints (dry run only previews).
                hint = f"configme install {link.dep}"
                if not dry_run and hint not in followups:
                    followups.append(hint)
                continue
            st = runner.link(link_path, dep_dest)
            color.cprint(f"  link {node.name}/{link.path} -> {link.dep}: {st}")
            if st == "linked":
                results["linked"].append(f"{node.name}/{link.path}")

    # --- configure phase
    runner.emit("\n# --- configure (per package) ---")
    info = None
    try:
        info = netcdf.detect()
    except netcdf.NetcdfError:
        info = None  # configure step will surface this per-package if needed
    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    linked_set = set(link_map)
    for node in plan.nodes:
        # Linked external: configme does not write Makefiles or trigger builds
        # inside a user-managed checkout. Same for subpackages of a linked
        # parent — they share that parent's (symlinked) checkout.
        if node.name in linked_set or (node.parent and node.parent in linked_set):
            runner.emit(f"# {node.name}: linked external (no configure)")
            color.cprint(f"  - {node.name}: linked external; configure left to the "
                  f"linked checkout")
            continue
        dest = dest_of(node, plan, root)
        if node.config_style == "none":
            # Clone-only component (e.g. vilma, bgc): configme places it but does
            # not generate a Makefile for it (it is built by the orchestrator, or
            # ships a prebuilt library).
            runner.emit(f"# {node.name}: clone-only (configme does not configure it)")
            if not dry_run and dest.is_dir():
                color.cprint(f"  - {node.name}: clone-only; no configuration")
            continue
        runner.emit(f"(cd {dest} && configme config {node.name} "
                    f"-m {machine} -c {compiler})")
        configure_makefile(label=node.name, pkg_name=node.name, dest=dest,
                            config_subdir=node.config_subdir,
                            is_orchestrator=node.is_orchestrator, **common_kw)
        # A package configme owns the build of (e.g. fesm-utils) is compiled
        # here, after its Makefile exists, via its [package.build] make target.
        if node.build is not None:
            _build_make_package(runner, node, dest, build_deps,
                                confirm_fn, results, followups,
                                machine=machine, compiler=compiler)

    # (Orchestrator extras ran early, right after selection — before the
    # component clones and build above — so all their prompts are front-loaded.)

    # --- reproducibility log
    install_sh = root / ".install.sh"
    if dry_run:
        print(f"\n--- .install.sh (dry-run preview; would write {install_sh}) ---")
        print("\n".join(runner.log))  # raw generated script — never colorized
    elif root.exists():
        install_sh.write_text("\n".join(runner.log) + "\n")
        try:
            install_sh.chmod(0o755)
        except OSError:
            pass
        color.cprint(f"\n  + wrote {install_sh}")

    # --- summary
    color.cprint("\nThis run:")
    # List the components with a pinned-ref marker so a non-default branch (e.g.
    # yelmo@climber-x) is obvious at a glance rather than buried in the clone log.
    comp_labels = [f"{n.name}@{n.ref}" if n.ref and n.ref != "main" else n.name
                   for n in plan.nodes if n.clone]
    if comp_labels:
        color.cprint(f"  components: {', '.join(comp_labels)}")
    for key in ("configured", "built", "linked", "pending",
                "deferred", "skipped", "unavailable", "failed"):
        if results[key]:
            color.cprint(f"  {key}: {', '.join(results[key])}")
    if followups:
        color.cprint("\nDeferred — run these when ready:")
        for cmd in followups:
            color.cprint(f"  {cmd}")
    # Read-only "still pending" picture of the whole checkout — covers
    # pre-existing state (e.g. an optional repo or data clone skipped on an
    # earlier run, a deferred build), not just what this run deferred. Imported
    # lazily to avoid a status <-> install import cycle. Skipped on a dry run
    # (the previewed .install.sh already shows what would change).
    if not dry_run:
        from configme import status
        block = status.pending_block(status.inspect(plan, root))
        if block:
            color.cprint(block)
    return 1 if results["failed"] else 0


def run_upgrade(target: str, *, install_dir: Optional[str],
                machine: Optional[str], compiler: Optional[str],
                build_deps: bool, dry_run: bool, only: bool = False,
                link_args: Optional[List[str]] = None,
                repos: Optional[List[str]] = None,
                select_fn=None, confirm_fn=None, ask_fn=None) -> int:
    """`configme upgrade`: ``git pull`` existing checkouts in place and
    reconfigure them with the same machine/compiler as the original install
    (read from ``.configme/config.toml``, overridable with ``-m``/``-c``).

    Mirrors ``install``'s target grammar (orchestrator + deps, a package + its
    auto-resolved deps, a '+'-literal list, or ``--only``) but never clones: a
    missing checkout, or one with uncommitted tracked changes, is skipped with a
    warning (run ``configme install`` / resolve it by hand instead). Only a
    package whose pull advanced HEAD is rebuilt, and only when it is a
    make-built package — gated like ``install`` (``--build-deps`` forces the
    build, otherwise an interactive prompt asks).

    Auxiliary/data repos (the orchestrator's ``data_packages``, e.g. climber-x's
    ``input``) ride the same node pipeline: a present one is pulled, and one that
    was never installed is reported under a "Not installed" reminder rather than
    cloned (upgrade never clones). After the components, the orchestrator's typed
    ``extras`` (post-config *actions* — pip tools, runme config, data links) are
    re-run in upgrade mode, each a Y/n prompt defaulting to **no**.

    ``repos`` (``--repos a,b``) narrows the whole run — components and data repos
    alike — to exactly the named checkouts; unknown names fail fast with the
    available list (and the action extras are skipped, since they are not repos).
    ``ask_fn``/``confirm_fn`` are the interactive prompts; passing a confirm that
    always returns True (``configme upgrade -y``) makes the entire upgrade
    non-interactive, including the default-no extra prompts."""
    cwd = Path.cwd()
    plan = build_plan(target, only=only)           # fail-fast: unknown names
    if not plan.primary.clone:
        parent = _parent_of(plan.primary.name)
        hint = (f"run `configme upgrade {parent}`" if parent
                else "upgrade its parent package")
        raise InstallError(
            f"{plan.primary.name} is a component of another package's checkout "
            f"and cannot be upgraded on its own; {hint}. (To (re)generate its "
            f"Makefile use `configme config {plan.primary.name}`.)")
    root, _ = root_for(plan, install_dir, cwd)
    if not root.exists():
        raise InstallError(
            f"{root} does not exist — nothing to upgrade. Run "
            f"`configme install {target}` first.")

    # --repos a,b narrows the run to a named subset of checkouts. Each entry is a
    # package name or a path to where the checkout lives; the candidates are
    # every clone node in the plan — which includes the orchestrator's
    # data_packages (auxiliary/data repos), so no special-casing is needed.
    # `selected=None` means "everything" (no filter).
    selected: Optional[set] = None
    if repos:
        candidates = [(n.name, dest_of(n, plan, root))
                      for n in plan.nodes if n.clone]
        selected = resolve_repo_selectors(repos, candidates)

    # Same selection the install used, reused from config.toml unless -m/-c
    # override it; persisted back so a retarget sticks for later runs.
    project = context.find_project(root)
    machine, compiler = context.resolve_selection(machine, compiler, project, select_fn)
    machine_path, _ = context.resolve_fragment("machine", machine, project)
    compiler_path, _ = context.resolve_fragment("compiler", compiler, project)

    runner = Runner(dry_run=dry_run)
    runner.emit("#!/usr/bin/env bash")
    runner.emit("# Generated by `configme upgrade` — reproduces this upgrade.")
    runner.emit("set -euo pipefail")
    runner.emit(f"# target={target}  machine={machine}  compiler={compiler}")

    header = color.warn("DRY RUN — no changes will be made.") + "\n" if dry_run else ""
    color.cprint(header + color.header(f"configme upgrade {target}"))
    color.cprint(f"  root: {root}")
    color.cprint(f"  machine={machine}  compiler={compiler}")
    color.cprint(f"  packages: {', '.join(n.name for n in plan.nodes)}"
          f"{' (literal, no auto-resolve)' if plan.explicit else ''}")

    # Resolve --link / links.toml (same shape as install). Also treat any
    # already-on-disk symlink at a node's dest as "managed externally" so a
    # prior install with --link survives a later upgrade run without --link.
    link_map = _resolve_links(plan, root, link_args or [], confirm_fn)
    if plan.primary.name in link_map:
        raise InstallError(
            f"--link {plan.primary.name}=...: cannot link the upgrade's "
            f"primary package.")
    linked_names = set(link_map)
    for node in plan.nodes:
        if node.name in linked_names:
            continue
        dest = dest_of(node, plan, root)
        if dest.is_symlink():
            linked_names.add(node.name)
    if link_map:
        color.cprint("  links:")
        for pkg, path in link_map.items():
            color.cprint(f"    {pkg} -> {path}{_stamp_note(path, machine, compiler)}")

    results = {"updated": [], "configured": [], "built": [], "missing": [],
               "deferred": [], "skipped": [], "uninstalled": [], "failed": []}

    # --- ref reconcile phase: switch each existing checkout to its resolved ref
    # *before* pulling and reconfiguring. The manifest pin (a package owner may
    # have edited it) wins over the orchestrator default, and the Makefile
    # template lives inside the checkout — so the ref must be correct first, and
    # `git pull --ff-only` then advances the right branch. A clean mismatch is
    # confirmed (default yes); a dirty or declined checkout is left untouched.
    _apply_manifest_refs_recursive(plan, root, project)
    _apply_machine_refs(plan.nodes, machine)
    runner.emit("\n# --- ref reconcile ---")
    for node in plan.nodes:
        if selected is not None and node.name not in selected:
            continue
        if not node.ref or not node.clone:
            continue
        if node.name in linked_names:
            runner.emit(f"# {node.name}: linked external (no ref switch)")
            continue
        dest = dest_of(node, plan, root)
        if not dest.is_dir():
            continue
        try:
            st = runner.ensure_ref(node, dest, confirm_fn=confirm_fn)
        except InstallError as e:
            color.cprint(f"  ! {node.name}: {e}")
            results["failed"].append(node.name)
            continue
        if st == "switched":
            color.cprint(f"  ref {node.name}: switched to {node.ref}")
        elif st == "dirty":
            color.cprint(f"  ref {node.name}: dirty; not switched to {node.ref} (skipped)")
            if node.name not in results["skipped"]:
                results["skipped"].append(node.name)
        elif st == "declined":
            color.cprint(f"  ref {node.name}: kept current branch "
                  f"(declined switch to {node.ref})")

    # --- pull phase (each checkout in place; a subpackage rides its parent's
    # checkout, so it has no separate pull but inherits the parent's updated flag)
    runner.emit("\n# --- pull ---")
    updated: Dict[str, bool] = {}
    for node in plan.nodes:
        if selected is not None and node.name not in selected:
            continue
        if not node.clone:
            runner.emit(f"# {node.name}: component of {node.parent} (no separate pull)")
            continue
        # Linked external: skip pull (user manages the target). A new --link
        # at upgrade time is honored as a (re)point — emit the symlink.
        if node.name in linked_names:
            updated[node.name] = False
            dest = dest_of(node, plan, root)
            if node.name in link_map:
                try:
                    st = runner.link_external(node, dest, link_map[node.name])
                    color.cprint(f"  link {node.name}: {st} -> {link_map[node.name]}")
                except InstallError as e:
                    color.cprint(f"  ! {node.name}: {e}")
                    results["failed"].append(node.name)
            else:
                runner.emit(f"# {node.name}: linked external (no pull)")
                color.cprint(f"  - {node.name}: linked external; skipping pull")
            continue
        dest = dest_of(node, plan, root)
        try:
            status, did_update = runner.pull(node, dest)
        except InstallError as e:
            color.cprint(f"  ! {node.name}: {e}")
            results["failed"].append(node.name)
            updated[node.name] = False
            continue
        updated[node.name] = did_update
        color.cprint(f"  pull {node.name}: {status}")
        if status == "missing":
            # An opt-in repo (optional private component, or a prompt/data repo)
            # that was never installed is an expected absence, not a problem:
            # upgrade never clones, so remind the user separately rather than
            # flagging it as "missing".
            if node.clone_policy in ("optional", "prompt"):
                results["uninstalled"].append(node.name)
            else:
                results["missing"].append(node.name)
        elif status == "dirty":
            results["skipped"].append(node.name)
        elif status == "updated":
            results["updated"].append(node.name)

    # --- reconfigure (always) + rebuild (only packages the pull advanced)
    runner.emit("\n# --- reconfigure (per package) ---")
    try:
        info = netcdf.detect()
    except netcdf.NetcdfError:
        info = None
    common_kw = dict(machine=machine, compiler=compiler, machine_path=machine_path,
                     compiler_path=compiler_path, dry_run=dry_run, results=results)
    for node in plan.nodes:
        # A subpackage shares its parent's git checkout, so both its "was it
        # updated?" and its `--repos` membership key off that owning checkout.
        owner = node.name if node.clone else (node.parent or node.name)
        if selected is not None and owner not in selected:
            continue
        # Linked external (or a subpackage under one): leave Makefile + build
        # to the linked checkout's owner.
        if node.name in linked_names or (node.parent and node.parent in linked_names):
            runner.emit(f"# {node.name}: linked external (no reconfigure)")
            color.cprint(f"  - {node.name}: linked external; skipping reconfigure")
            continue
        dest = dest_of(node, plan, root)
        was_updated = updated.get(owner, False)

        if node.config_style == "none":
            runner.emit(f"# {node.name}: clone-only (configme does not configure it)")
            continue

        runner.emit(f"(cd {dest} && configme config {node.name} "
                    f"-m {machine} -c {compiler})")
        configure_makefile(label=node.name, pkg_name=node.name, dest=dest,
                            config_subdir=node.config_subdir,
                            is_orchestrator=node.is_orchestrator, **common_kw)
        if node.build is not None:
            if was_updated:
                _build_make_package(runner, node, dest, build_deps,
                                    confirm_fn, results,
                                    prefer_skip_if_built=False,
                                    machine=machine, compiler=compiler)
            elif dest.is_dir():
                color.cprint(f"  - {node.name}: unchanged; skipping rebuild")

    # --- extras (orchestrator post-config *actions*: pip tools, runme config,
    # data links), in upgrade mode: each is an opt-in Y/n (default no; -y
    # forces). Skipped entirely under --repos, which selects repos — and extras
    # are not repos (data repos are plan nodes, handled above). A deferred step
    # surfaces in the summary like install's.
    if (selected is None and plan.orchestrator is not None
            and plan.orchestrator.extras):
        cfg = context.load_config(project) if project else {}
        ask = ask_fn or (lambda label, default=None, *, complete_paths=False: default)
        followups = extras_mod.run_extras(
            plan.orchestrator, runner, root, cfg, ask, confirm_fn,
            machine=machine, upgrade=True)
        results["deferred"].extend(followups)

    # --- reproducibility log
    upgrade_sh = root / ".upgrade.sh"
    if dry_run:
        print(f"\n--- .upgrade.sh (dry-run preview; would write {upgrade_sh}) ---")
        print("\n".join(runner.log))  # raw generated script — never colorized
    else:
        upgrade_sh.write_text("\n".join(runner.log) + "\n")
        try:
            upgrade_sh.chmod(0o755)
        except OSError:
            pass
        color.cprint(f"\n  + wrote {upgrade_sh}")

    # --- summary
    color.cprint("\nThis run:")
    for key in ("updated", "configured", "built", "missing",
                "deferred", "skipped", "failed"):
        if results[key]:
            color.cprint(f"  {key}: {', '.join(results[key])}")
    # Reminder: opt-in repos (optional components, prompt/data repos) that were
    # never installed. upgrade never clones, so surface them with the command to
    # fetch them rather than leaving the user to wonder why they were untouched.
    if results["uninstalled"]:
        color.cprint(f"\nNot installed (upgrade never clones — run "
              f"`configme install {plan.primary.name}` to fetch):")
        for name in results["uninstalled"]:
            color.cprint(f"  - {name}")
    return 1 if results["failed"] else 0


def _artifacts_state(name: str, dest: Path) -> str:
    """Probe a package's declared ``[package.artifacts]`` under ``dest`` to tell
    whether it is already built. Returns one of:

      ``built``    every artifact of every variant is present
      ``partial``  some but not all are present (a half-finished build)
      ``unbuilt``  none present
      ``none``     the package declares no artifacts (can't tell — assume unbuilt)

    Mirrors the per-variant probe in ``status._inspect_builds`` but aggregates
    across variants, so the build step can skip an already-built package on an
    idempotent re-run (see ``_build_make_package``)."""
    pkg = data.packages().get(name)
    if pkg is None or not pkg.artifacts:
        return "none"
    paths = [p for variant_paths in pkg.artifacts.values() for p in variant_paths]
    present = sum(1 for p in paths if (dest / p).exists())
    if present == 0:
        return "unbuilt"
    return "built" if present == len(paths) else "partial"


def _build_make_package(runner, node, dest, build_deps, confirm_fn, results,
                        followups=None, *, prefer_skip_if_built=True,
                        machine: Optional[str] = None,
                        compiler: Optional[str] = None):
    """Compile a package whose build configme owns (`[package.build]`), once per
    variant: ``make openmp=<0|1> <make_target>``. Runs in the inherited shell
    environment (the Makefile already carries the configme-resolved compiler /
    netCDF, so no module loading is needed here). Gated so ``--build-deps``
    forces it, an interactive run asks (default yes), and dry / non-interactive
    runs only print the commands."""
    spec = node.build
    cmds = [(v, f"make openmp={spec.openmp_flag(v)} {spec.make_target}")
            for v in spec.variants]
    runner.emit(f"# {node.name}: build ({', '.join(spec.variants)}):")
    for _, cmd in cmds:
        runner.emit(f"(cd {dest} && {cmd})")

    def _defer(reason: str) -> None:
        color.cprint(f"  - {node.name}: {reason}; build when ready:")
        for _, cmd in cmds:
            color.cprint(f"      (cd {dest} && {cmd})")
        results["deferred"].append(node.name)
        # Echo in the summary's consolidated deferred list (skip on a dry run,
        # where the whole .install.sh is already previewed).
        if followups is not None and not runner.dry_run:
            for _, cmd in cmds:
                followups.append(f"(cd {dest} && {cmd})")

    if runner.dry_run or not dest.is_dir():
        _defer("configured")
        return

    # Skip an already-built package on a re-run; upgrade passes
    # prefer_skip_if_built=False so a post-pull rebuild still happens.
    state = _artifacts_state(node.name, dest)
    already_built = prefer_skip_if_built and state == "built"

    do_build = build_deps
    if not do_build and confirm_fn is not None:
        if already_built:
            color.cprint(f"  {node.name}: already built ({', '.join(spec.variants)}).")
            do_build = confirm_fn(f"Rebuild {node.name}?", False)
        else:
            if state == "partial":
                color.cprint(f"  {node.name}: build incomplete; rebuilding recommended.")
            color.cprint(f"  {node.name} needs a make build ({', '.join(spec.variants)}):")
            do_build = confirm_fn(f"Build {node.name} now?", True)
    if not do_build:
        if already_built:
            color.cprint(f"  - {node.name}: keeping existing build")
            results["built"].append(node.name)
        else:
            _defer("build deferred")
        return

    built_variants: List[str] = []
    for variant, cmd in cmds:
        color.cprint(f"  building {node.name} ({variant}): {cmd}")
        try:
            subprocess.run(["make", f"openmp={spec.openmp_flag(variant)}",
                            spec.make_target], cwd=dest, check=True)
            results["built"].append(f"{node.name} ({variant})")
            built_variants.append(variant)
        except (subprocess.CalledProcessError, OSError) as e:
            color.cprint(f"  ! {node.name} build failed: {e}")
            results["failed"].append(f"{node.name} ({variant})")
    if built_variants and machine and compiler:
        try:
            links_mod.write_build_stamp(dest, tool="make", machine=machine,
                                        compiler=compiler,
                                        variants=built_variants)
        except OSError:
            pass
