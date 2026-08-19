# plan.nix — pure planning library for programs.gdrive-mounts.
#
# One code path. The home-manager module and the eval fixture both derive mount
# argv, paths and unit names from this file, so a test that passes proves the
# argv the module ships.
#
# Takes `lib` only. It reads no pkgs, no stdenv, no filesystem.
{ lib }:
let
  inherit (lib) filter concatMap optionals elem head hasPrefix removePrefix;

  # The only nix-side `~` expansion in this repo.
  expandHome =
    home: p:
    if p == "~" then
      home
    else if hasPrefix "~/" p then
      home + removePrefix "~" p
    else
      p;

  # Resolve orgs.json `defaults` into the settings the module and the plan use.
  # orgs.json is the single source of truth for every knob below.
  settingsFrom =
    { home, defaults }:
    let
      e = expandHome home;
    in
    {
      stateDir = e defaults.stateDir;
      mountRoot = e defaults.mountRoot;
      cacheRoot = e defaults.cacheRoot;
      cacheMaxSize = defaults.cacheMaxSize;
      vfsCacheMode = defaults.vfsCacheMode;
      dirCacheTime = defaults.dirCacheTime;
      vfsReadAhead = defaults.vfsReadAhead;
      nfsCacheHandleLimit = defaults.nfsCacheHandleLimit;
      indexStateDir = e defaults.indexStateDir;
      indexFreshnessSloHours = defaults.indexFreshnessSloHours;
      backendDarwin = defaults.mountBackendDarwin;
      backendLinux = defaults.mountBackendLinux;
    };

  # rclone subcommand for the platform. darwin defaults to nfsmount (kext-free).
  backendFor = { platform, settings }: if platform == "darwin" then settings.backendDarwin else settings.backendLinux;

  # An org is read-only unless its OAuth scope is the full `drive`.
  readOnly = org: (org.scope or "drive.readonly") != "drive";

  # One rendered config per org. rclone writes refreshed tokens back into the
  # file it was given, so a single writer per file is a correctness rule.
  confPath = settings: org: "${settings.stateDir}/rclone-${org.name}.conf";

  cacheDir = settings: org: "${settings.cacheRoot}/${org.name}";

  # The volume the cache lives on: for /Volumes/<name>/... and /mnt/<name>/...
  # that is the mountpoint two components deep (the cache root may sit in a
  # user-owned subtree of a root-owned volume, e.g. /Volumes/TinylandSSD/tinyland);
  # elsewhere it is the cache root's parent.
  cacheVolume =
    cacheRoot:
    let
      parts = filter (x: x != "") (lib.splitString "/" cacheRoot);
    in
    if (hasPrefix "/Volumes/" cacheRoot || hasPrefix "/mnt/" cacheRoot) && builtins.length parts >= 2 then
      "/" + (builtins.elemAt parts 0) + "/" + (builtins.elemAt parts 1)
    else
      builtins.dirOf cacheRoot;

  mountPoint = settings: mount: "${settings.mountRoot}/${mount.mountSuffix}";

  unitName = org: mount: "gdrive-mounts-${org.name}-${mount.name}";

  # Links hang off the org root mount.
  rootMount =
    org:
    let
      mounts = org.mounts or [ ];
      roots = filter (m: m.name == "root") mounts;
    in
    if roots != [ ] then
      head roots
    else if mounts != [ ] then
      head mounts
    else
      throw "gdrive-mounts: org ${org.name} declares links but no mount to hang them on";

  linkPath = settings: org: link: "${settings.mountRoot}/${org.name}-${link.name}";

  linkTarget = settings: org: link: "${mountPoint settings (rootMount org)}/${link.target}";

  # argv for `rclone`, without the binary. Flags are backend- and scope-gated:
  #   --nfs-cache-handle-limit exists only on nfsmount/serve nfs
  #   --volname is documented macOS/Windows only
  #   --read-only enforces the scope the token was minted with
  mountArgs =
    {
      platform,
      settings,
      org,
      mount,
      extraFlags ? [ ],
    }:
    let
      backend = backendFor { inherit platform settings; };
      path = mount.remotePath or "";
      src = if path == "" then "${org.remote}:" else "${org.remote}:${path}";
    in
    [
      backend
      src
      (mountPoint settings mount)
      "--config"
      (confPath settings org)
      "--vfs-cache-mode"
      settings.vfsCacheMode
      "--cache-dir"
      (cacheDir settings org)
      "--vfs-cache-max-size"
      settings.cacheMaxSize
      "--dir-cache-time"
      settings.dirCacheTime
      "--vfs-read-ahead"
      settings.vfsReadAhead
    ]
    ++ optionals (backend == "nfsmount") [
      "--nfs-cache-handle-limit"
      (toString settings.nfsCacheHandleLimit)
    ]
    ++ optionals (platform == "darwin") [
      "--volname"
      "gdrive-${org.name}-${mount.name}"
    ]
    ++ optionals (readOnly org) [ "--read-only" ]
    ++ extraFlags;

  # ── generated-wrapper shell ─────────────────────────────────────────────────
  #
  # Text, not derivations: plan.nix stays pure, and the eval fixture can source
  # the exact same text into a harness it executes. A guard that passes there is
  # the guard that ships.

  # Portable probes. `stat` is the trap: GNU takes `-c FORMAT`, BSD takes
  # `-f FORMAT`, and the flags are not merely incompatible — GNU's `-f` is
  # `--file-system`, so `stat -f %d` against GNU stat succeeds and prints a
  # statfs report instead of a device number. The platform cannot decide this
  # either, because the wrapper's PATH puts nix coreutils ahead of /usr/bin, so
  # `stat` is GNU even on Darwin. Detect the implementation, exactly as
  # `scripts/doctor.sh` already does.
  #
  # Defines: gdm_log, gdm_dev_of, gdm_perm_of, gdm_is_mountpoint,
  # gdm_deepest_existing. Reads `$unit` for the log prefix.
  shellPreamble = ''
    # One timestamped line per phase, on stdout, so a wrapper that is stuck
    # somewhere before rclone can be diagnosed from the launchd log alone.
    gdm_log() {
      printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$unit" "$*"
    }

    if stat --version >/dev/null 2>&1; then
      gdm_dev_of() { stat -c %d -- "$1" 2>/dev/null; }
      # Mode and ownership, so a permission failure names what to fix.
      gdm_perm_of() { stat -c '%A %U:%G' -- "$1" 2>/dev/null || printf 'mode unreadable'; }
    else
      gdm_dev_of() { stat -f %d -- "$1" 2>/dev/null; }
      gdm_perm_of() { stat -f '%Sp %Su:%Sg' -- "$1" 2>/dev/null || printf 'mode unreadable'; }
    fi

    # Is a real filesystem mounted here? Fails closed: an unreadable probe is
    # "not a mountpoint", never "yes" — the cache must not land on the boot
    # disk because a stat call went wrong.
    gdm_is_mountpoint() {
      local p a b
      p="$1"
      [ -d "$p" ] || return 1
      a="$(gdm_dev_of "$p")"
      b="$(gdm_dev_of "$p/..")"
      [ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]
    }

    # Deepest existing directory at or above $1, never above the floor $2.
    # This is the directory `mkdir -p "$1"` will actually write into, so it is
    # the one whose writability decides whether the cache can be created.
    gdm_deepest_existing() {
      local p floor
      p="$1"
      floor="$2"
      while [ ! -d "$p" ]; do
        case "$p" in
          "$floor" | / | .) break ;;
        esac
        p="$(dirname -- "$p")"
      done
      printf '%s' "$p"
    }
  '';

  # The cache-volume guard: a bounded wait, then one loud, distinguishable exit.
  #
  # Reads `$unit`, `$cache`, `$volume`, `$err`; needs shellPreamble sourced
  # first. Defines gdm_cache_ready and gdm_wait_for_cache; the caller calls
  # gdm_wait_for_cache.
  #
  # Writability is tested on the deepest existing ancestor of the cache
  # directory, not on the volume root. An external volume's root is commonly
  # root-owned (`/Volumes/<name>` mounts drwxr-xr-x root:wheel) while the
  # user-owned cache subtree beneath it is perfectly writable, and testing the
  # root turned that into an exit-78 respawn loop on neo, 2026-08-19.
  cacheGuard =
    {
      requireMountpoint,
      waitSeconds,
      sleepSeconds ? 5,
    }:
    ''
      require_mountpoint=${if requireMountpoint then "1" else "0"}

      # Prints why the cache is not usable yet; empty output and rc 0 mean ready.
      gdm_cache_ready() {
        local anchor
        if [ ! -d "$volume" ]; then
          printf 'cache volume %s does not exist' "$volume"
          return 1
        fi
        if [ "$require_mountpoint" = 1 ] && ! gdm_is_mountpoint "$volume"; then
          printf 'cache volume %s is not a mountpoint (%s) — refusing to spill the cache onto the boot disk' \
            "$volume" "$(gdm_perm_of "$volume")"
          return 1
        fi
        anchor="$(gdm_deepest_existing "$cache" "$volume")"
        if [ ! -w "$anchor" ]; then
          printf 'cache dir %s cannot be created: %s is not writable by %s (%s)' \
            "$cache" "$anchor" "$(id -un)" "$(gdm_perm_of "$anchor")"
          return 1
        fi
        return 0
      }

      gdm_fail_cache() { # reason
        printf 'FATAL gdrive-mounts %s: %s\n' "$unit" "$1" >&2
        printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" > "$err"
        gdm_log "guard: FATAL — $1 (exit 78)"
        exit 78
      }

      gdm_wait_for_cache() {
        local waited=0 why
        while :; do
          why="$(gdm_cache_ready)" && break
          if [ "$waited" -ge ${toString waitSeconds} ]; then
            gdm_fail_cache "$why (waited ''${waited}s)"
          fi
          gdm_log "guard: not ready — $why; retrying in ${toString sleepSeconds}s"
          sleep ${toString sleepSeconds}
          waited=$((waited + ${toString sleepSeconds}))
        done
        mkdir -p "$cache" || gdm_fail_cache "cache dir $cache could not be created under $volume"
        gdm_log "guard: ready — volume $volume, cache $cache"
      }
    '';

  # The whole emission plan for one platform. `secretNames` are the org names
  # the consumer wired secrets for; an unwired org emits nothing but a warning.
  renderPlan =
    {
      platform,
      settings,
      orgs,
      secretNames,
      extraFlags ? [ ],
    }:
    let
      enabled = filter (o: o.enabled or false) orgs;
      wired = filter (o: elem o.name secretNames) enabled;
      unwired = map (o: o.name) (filter (o: !(elem o.name secretNames)) enabled);
      units = concatMap (
        org:
        map (mount: {
          inherit org mount;
          name = unitName org mount;
          point = mountPoint settings mount;
          conf = confPath settings org;
          cache = cacheDir settings org;
          readOnly = readOnly org;
          args = mountArgs {
            inherit
              platform
              settings
              org
              mount
              extraFlags
              ;
          };
        }) (org.mounts or [ ])
      ) wired;
      links = concatMap (
        org:
        map (link: {
          inherit org link;
          path = linkPath settings org link;
          target = linkTarget settings org link;
        }) (org.links or [ ])
      ) wired;
    in
    {
      inherit
        enabled
        wired
        unwired
        units
        links
        ;
    };
in
{
  inherit
    cacheVolume
    expandHome
    settingsFrom
    backendFor
    readOnly
    confPath
    cacheDir
    mountPoint
    unitName
    rootMount
    linkPath
    linkTarget
    mountArgs
    renderPlan
    shellPreamble
    cacheGuard
    ;
}
