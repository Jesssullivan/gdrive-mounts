# programs.gdrive-mounts — rclone-mounted Google Workspace drives.
#
# Purity rule (house doctrine, cf. linear-gsuite): this module holds no secrets
# and names no consumer paths. The consumer passes secret file paths in through
# `secrets`; an enabled org with no `secrets` entry emits a warning and no units.
#
# Every runtime knob defaults from orgs.json `defaults` through nix/lib/plan.nix.
# orgs.json is the source of truth; this file restates none of it.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.gdrive-mounts;
  plan = import ../lib/plan.nix { inherit lib; };

  inherit (lib)
    mkEnableOption
    mkOption
    mkIf
    mkMerge
    types
    nameValuePair
    optional
    literalExpression
    literalMD
    escapeShellArg
    escapeShellArgs
    ;

  orgsData = builtins.fromJSON (builtins.readFile cfg.orgsFile);

  # Option defaults. Read once, before any option is set, so a consumer override
  # still wins.
  fromRegistry = plan.settingsFrom {
    home = config.home.homeDirectory;
    defaults = orgsData.defaults;
  };

  # Effective settings: option values, which default to the registry above.
  settings = {
    inherit (cfg)
      stateDir
      mountRoot
      cacheRoot
      cacheMaxSize
      vfsCacheMode
      dirCacheTime
      vfsReadAhead
      nfsCacheHandleLimit
      backendDarwin
      backendLinux
      ioTimeout
      connectTimeout
      lowLevelRetries
      attrTimeout
      pollInterval
      logLevel
      statsInterval
      nfsMountOptions
      ;
    indexStateDir = cfg.index.stateDir;
    indexFreshnessSloHours = cfg.index.freshnessSloHours;
    remoteControl = cfg.remoteControl.enable;
    watchdogEnable = cfg.watchdog.enable;
    watchdogIntervalSec = cfg.watchdog.intervalSec;
    watchdogProbeTimeoutSec = cfg.watchdog.probeTimeoutSec;
    watchdogFailureThreshold = cfg.watchdog.failureThreshold;
    watchdogRestartFloorSec = cfg.watchdog.restartFloorSec;
  };

  secretNames = builtins.attrNames cfg.secrets;

  emission = plan.renderPlan {
    inherit settings secretNames;
    platform = cfg.platform;
    orgs = orgsData.orgs;
    extraFlags = cfg.extraMountFlags;
  };

  # Launchd hands an agent /usr/bin:/bin:/usr/sbin:/sbin and systemd hands it
  # even less, so every generated wrapper carries its own PATH.
  runtimePath =
    lib.makeBinPath (
      [
        cfg.package
        cfg.toolsPackage
        pkgs.jq
        pkgs.sqlite
        pkgs.coreutils
        pkgs.gnused
        pkgs.bash
      ]
      ++ cfg.extraRuntimeInputs
    )
    + ":/usr/bin:/bin:/usr/sbin:/sbin:/run/current-system/sw/bin";

  orgsFileArg = "${cfg.orgsFile}";
  templateArg = "${cfg.toolsPackage}/share/gdrive-mounts/rclone.conf.template";
  renderBin = "${cfg.toolsPackage}/bin/gdrive-mounts-render-config";
  indexBin = "${cfg.toolsPackage}/bin/gdrive-mounts-gdrive-index";

  # Paths only. No secret value is ever read by nix.
  secretExports =
    org:
    let
      sec = cfg.secrets.${org.name};
    in
    ''
      export ${org.secretEnvPrefix}_CLIENT_FILE=${escapeShellArg sec.clientFile}
      export ${org.secretEnvPrefix}_TOKEN_FILE=${escapeShellArg sec.tokenFile}
    '';

  # The volume the cache lives on, not the cache directory itself.
  cacheVolume = plan.cacheVolume settings.cacheRoot;

  backend = plan.backendFor {
    inherit (cfg) platform;
    inherit settings;
  };

  # Is anything mounted at the point? mount(8) on darwin, mountpoint(1) on linux.
  isMounted =
    point:
    if cfg.platform == "darwin" then
      ''mount | grep -qF " on ${point} ("''
    else
      "mountpoint -q ${escapeShellArg point}";

  sweepStaleMount =
    point:
    if cfg.platform == "darwin" then
      "umount -f ${escapeShellArg point} >/dev/null 2>&1 || true"
    else
      "fusermount3 -u ${escapeShellArg point} >/dev/null 2>&1 || umount -f ${escapeShellArg point} >/dev/null 2>&1 || true";

  # Is anything mounted here, WITHOUT touching the filesystem? The watchdog asks
  # this about a mount that may be wedged, where `mountpoint -q` — which stats
  # the path — would block forever in the kernel. mount(8) and /proc/self/mounts
  # both read the kernel's mount table instead.
  isMountedNonBlocking =
    point:
    if cfg.platform == "darwin" then
      ''mount | grep -qF " on ${point} ("''
    else
      "awk -v p=${escapeShellArg point} '$2 == p { found = 1 } END { exit !found }' /proc/self/mounts";

  mountLabel = u: "dev.tinyland.gdrive-mounts.${u.org.name}-${u.mount.name}";

  # The mount unit's pid. Both supervisors report the pid of the process they
  # started, and the wrapper `exec`s rclone, so that pid IS rclone's.
  unitPidCommand =
    u:
    if cfg.platform == "darwin" then
      ''launchctl list ${escapeShellArg (mountLabel u)} 2>/dev/null | sed -n 's/.*"PID" = \([0-9]*\);.*/\1/p' | head -1''
    else
      "systemctl --user show -p MainPID --value ${escapeShellArg "${u.name}.service"} 2>/dev/null | grep -v '^0$' || true";

  unitActiveCheck =
    u:
    if cfg.platform == "darwin" then
      "launchctl list ${escapeShellArg (mountLabel u)} >/dev/null 2>&1"
    else
      "systemctl --user is-active --quiet ${escapeShellArg "${u.name}.service"}";

  restartCommand =
    u:
    if cfg.platform == "darwin" then
      ''launchctl kickstart -k "gui/$(id -u)/${mountLabel u}" >/dev/null 2>&1 || true''
    else
      "systemctl --user restart ${escapeShellArg "${u.name}.service"} >/dev/null 2>&1 || true";

  # Every phase logs one timestamped line before rclone replaces this process.
  # Without it a wrapper that never reaches `exec` leaves an empty stdout log
  # and an operator with nothing to read but a launchd exit status.
  mountScript =
    u:
    pkgs.writeShellScript "gdrive-mount-${u.name}" ''
      set -euo pipefail
      export PATH=${escapeShellArg runtimePath}
      ${secretExports u.org}
      unit=${escapeShellArg u.name}
      state=${escapeShellArg settings.stateDir}
      conf=${escapeShellArg u.conf}
      point=${escapeShellArg u.point}
      cache=${escapeShellArg u.cache}
      volume=${escapeShellArg cacheVolume}
      sock=${escapeShellArg u.sock}
      err="$state/last-error.${u.org.name}-${u.mount.name}"

      ${plan.shellPreamble}
      ${plan.cacheGuard {
        inherit (cfg.cache) requireMountpoint waitSeconds;
      }}

      gdm_log "start: pid $$, backend ${backend}, point $point"

      mkdir -p "$state"
      chmod 700 "$state" 2>/dev/null || true
      ${lib.optionalString (!cfg.cache.requireMountpoint) ''mkdir -p "$volume" >/dev/null 2>&1 || true''}

      # Cache guard: bounded wait, then fail loud with exit 78. The cache never
      # spills onto the boot disk.
      gdm_wait_for_cache

      # Clear anything already mounted at the point (a dead NFS loopback left by
      # a killed rclone keeps answering stat from cache and hangs every real
      # read until it is force-unmounted). We are the only legitimate mounter
      # of this path and we are not running yet, so an existing mount is stale.
      #
      # The sweep must be *asserted*, not attempted. `umount -f` against a
      # `hard,nointr` mount whose callers are blocked in RPC can return EBUSY,
      # and a swallowed failure means the next line mounts on top of the corpse:
      # a live process, a listening port, and a client still addressed to the
      # dead one. Silent, and indistinguishable from the wedge it causes.
      if ${isMounted u.point}; then
        gdm_log "sweep: stale mount at $point — force-unmounting"
        ${sweepStaleMount u.point}
        if ${isMounted u.point}; then
          gdm_log "sweep: FAILED — $point is still mounted (exit 75)"
          printf 'FATAL gdrive-mounts %s: stale mount at %s survived the force-unmount\n' "$unit" "$point" >&2
          printf '%s stale mount at %s survived the force-unmount\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$point" > "$err"
          exit 75
        fi
        gdm_log "sweep: clear"
      else
        gdm_log "sweep: nothing mounted at $point"
      fi
      mkdir -p "$point"
      rm -f "$err"

      # rclone binds the rc socket itself and will not bind over an existing
      # file, so a socket left by a killed process is a start failure.
      if [ -n "$sock" ]; then
        rm -f "$sock"
        gdm_log "rc: serving the remote-control API on $sock (unix socket, inside the 0700 state dir)"
      fi

      # Render only when the conf is missing. rclone owns the file after start:
      # it writes refreshed tokens back into it, and a re-render would undo them.
      if [ ! -s "$conf" ]; then
        gdm_log "render: $conf is missing — rendering it from the template"
        ${renderBin} \
          --org ${escapeShellArg u.org.name} \
          --orgs ${escapeShellArg orgsFileArg} \
          --template ${escapeShellArg templateArg} \
          --out "$conf"
      else
        gdm_log "render: reusing $conf (rclone owns it after first start)"
      fi

      gdm_log exec: rclone ${escapeShellArgs u.args}
      exec ${cfg.package}/bin/rclone ${escapeShellArgs u.args}
    '';

  # The health-probe sidecar. One per mount, supervised alongside it, never in
  # the same process: a watchdog that shares a fate with the thing it watches is
  # not a watchdog.
  watchdogScript =
    u:
    pkgs.writeShellScript "gdrive-mounts-watchdog-${u.name}" ''
      set -euo pipefail
      export PATH=${escapeShellArg runtimePath}
      unit=${escapeShellArg "${u.name}-watchdog"}
      state=${escapeShellArg settings.stateDir}
      point=${escapeShellArg u.point}
      record=${escapeShellArg u.watchdog.record}
      capture=${escapeShellArg u.watchdog.capture}
      floor_file=${escapeShellArg u.watchdog.floorFile}
      probe_dir=${escapeShellArg u.watchdog.probeDir}
      sock=${escapeShellArg u.sock}
      rclone_bin=${cfg.package}/bin/rclone

      ${plan.shellPreamble}
      ${plan.watchdogShell {
        intervalSec = cfg.watchdog.intervalSec;
        probeTimeoutSec = cfg.watchdog.probeTimeoutSec;
        failureThreshold = cfg.watchdog.failureThreshold;
        restartFloorSec = cfg.watchdog.restartFloorSec;
        # Derived, never a knob of its own: it must never be shorter than the
        # cache guard's own budget, or the watchdog would restart a mount that
        # is legitimately still waiting for its volume.
        mountGraceSec = cfg.cache.waitSeconds + 180;
        nfsStatus = cfg.platform == "darwin" && backend == "nfsmount";
        mountedCheck = isMountedNonBlocking u.point;
        unitActiveCheck = unitActiveCheck u;
        unitPidCommand = unitPidCommand u;
        restartCommand = restartCommand u;
      }}

      mkdir -p "$state" "$probe_dir"
      chmod 700 "$state" 2>/dev/null || true
      gdm_watch_loop
    '';

  logDir = "${config.home.homeDirectory}/Library/Logs/tinyland";

  mountAgents = lib.listToAttrs (
    map (
      u:
      nameValuePair u.name {
        enable = true;
        config = {
          Label = "dev.tinyland.gdrive-mounts.${u.org.name}-${u.mount.name}";
          ProgramArguments = [ "${mountScript u}" ];
          RunAtLoad = true;
          KeepAlive = {
            SuccessfulExit = false;
          };
          ThrottleInterval = 30;
          ProcessType = "Background";
          EnvironmentVariables = {
            PATH = runtimePath;
          };
          StandardOutPath = "${logDir}/gdrive-mounts.${u.org.name}-${u.mount.name}.out.log";
          StandardErrorPath = "${logDir}/gdrive-mounts.${u.org.name}-${u.mount.name}.err.log";
        };
      }
    ) emission.units
  );

  watchdogAgents = lib.listToAttrs (
    map (
      u:
      nameValuePair "${u.name}-watchdog" {
        enable = true;
        config = {
          Label = "${mountLabel u}.watchdog";
          ProgramArguments = [ "${watchdogScript u}" ];
          RunAtLoad = true;
          # Unconditional: this is a supervision loop, so a clean exit is still
          # an absence of supervision.
          KeepAlive = true;
          ThrottleInterval = 30;
          ProcessType = "Background";
          EnvironmentVariables = {
            PATH = runtimePath;
          };
          StandardOutPath = "${logDir}/gdrive-mounts.${u.org.name}-${u.mount.name}.watchdog.log";
          StandardErrorPath = "${logDir}/gdrive-mounts.${u.org.name}-${u.mount.name}.watchdog.err.log";
        };
      }
    ) emission.units
  );

  mountServices = lib.listToAttrs (
    map (
      u:
      nameValuePair u.name {
        Unit = {
          Description = "rclone mount ${u.org.remote} at ${u.point}";
          After = [ "network-online.target" ];
          Wants = [ "network-online.target" ];
        };
        Service = {
          ExecStart = "${mountScript u}";
          Environment = [ "PATH=${runtimePath}" ];
          Restart = "on-failure";
          RestartSec = 30;
        };
        Install.WantedBy = [ "default.target" ];
      }
    ) emission.units
  );

  watchdogServices = lib.listToAttrs (
    map (
      u:
      nameValuePair "${u.name}-watchdog" {
        Unit = {
          Description = "gdrive-mounts health watchdog for ${u.point}";
          # After, deliberately not PartOf or BindsTo: the watchdog stands down
          # by itself when the mount unit is inactive, and coupling the two would
          # take the watchdog down in the middle of the restart it just ordered —
          # losing the wedge record that makes recurrence measurable.
          After = [ "${u.name}.service" ];
        };
        Service = {
          ExecStart = "${watchdogScript u}";
          Environment = [ "PATH=${runtimePath}" ];
          Restart = "always";
          RestartSec = 30;
        };
        Install.WantedBy = [ "default.target" ];
      }
    ) emission.units
  );

  indexScript = pkgs.writeShellScript "gdrive-mounts-index" ''
    set -euo pipefail
    export PATH=${escapeShellArg runtimePath}
    unit="gdrive-mounts-index"
    ${plan.shellPreamble}
    gdm_log "start: pid $$, state ${settings.indexStateDir}"
    gdm_log "exec: gdrive-mounts-gdrive-index"
    exec ${indexBin} \
      --orgs ${escapeShellArg orgsFileArg} \
      --conf-dir ${escapeShellArg settings.stateDir} \
      --state-dir ${escapeShellArg settings.indexStateDir}
  '';

  renderActivation = ''
    export PATH=${escapeShellArg runtimePath}
    ${lib.concatMapStrings secretExports emission.wired}
    $DRY_RUN_CMD mkdir -p ${escapeShellArg settings.stateDir}
    $DRY_RUN_CMD chmod 700 ${escapeShellArg settings.stateDir}
    $DRY_RUN_CMD ${renderBin} \
      --orgs ${escapeShellArg orgsFileArg} \
      --template ${escapeShellArg templateArg} \
      --out-dir ${escapeShellArg settings.stateDir} \
      --best-effort
  '';

  # What this module actually resolved, written where doctor can read it. A
  # consumer overrides options per host, and those overrides never reach
  # orgs.json — so orgs.json `defaults` is only doctor's pre-activation guess.
  # Non-secret by construction: `cfg.secrets` is not read here, and must not be.
  effectiveSettings = {
    schema_version = 1;
    inherit (settings)
      stateDir
      mountRoot
      cacheRoot
      cacheMaxSize
      vfsCacheMode
      dirCacheTime
      vfsReadAhead
      nfsCacheHandleLimit
      indexStateDir
      indexFreshnessSloHours
      ioTimeout
      connectTimeout
      lowLevelRetries
      attrTimeout
      pollInterval
      logLevel
      statsInterval
      nfsMountOptions
      remoteControl
      watchdogEnable
      watchdogIntervalSec
      watchdogProbeTimeoutSec
      watchdogFailureThreshold
      watchdogRestartFloorSec
      ;
    inherit (cfg) platform;
    inherit backend;
    orgsFile = "${cfg.orgsFile}";
    units = map (u: {
      org = u.org.name;
      mount = u.mount.name;
      inherit (u)
        point
        conf
        cache
        sock
        ;
      watchdogRecord = u.watchdog.record;
      watchdogLabel =
        if cfg.platform == "darwin" then "${mountLabel u}.watchdog" else "${u.name}-watchdog.service";
    }) emission.units;
    links = map (l: {
      org = l.org.name;
      name = l.link.name;
      inherit (l) path target;
    }) emission.links;
  };

  effectiveSettingsFile = pkgs.writeText "gdrive-mounts-effective-settings.json" (
    builtins.toJSON effectiveSettings
  );

  effectiveSettingsPath = "${settings.stateDir}/effective-settings.json";

  # Atomic 0600 install, same contract as every other file this repo creates:
  # write beside the destination, then rename over it.
  settingsActivation = ''
    export PATH=${escapeShellArg runtimePath}
    $DRY_RUN_CMD mkdir -p ${escapeShellArg settings.stateDir}
    $DRY_RUN_CMD chmod 700 ${escapeShellArg settings.stateDir}
    $DRY_RUN_CMD install -m 600 ${effectiveSettingsFile} ${escapeShellArg "${effectiveSettingsPath}.new"}
    $DRY_RUN_CMD mv -f ${escapeShellArg "${effectiveSettingsPath}.new"} ${escapeShellArg effectiveSettingsPath}
  '';

  # Out-of-store symlinks: the target is a live mountpoint that does not exist
  # until rclone starts, so this is `ln -sfn`, not home.file.
  linkActivation = ''
    $DRY_RUN_CMD mkdir -p ${escapeShellArg settings.mountRoot}
    ${lib.concatMapStrings (
      l: "$DRY_RUN_CMD ln -sfn ${escapeShellArg l.target} ${escapeShellArg l.path}\n"
    ) emission.links}
  '';

  # A soft mount lets a stalled RPC fail with EIO instead of blocking forever.
  # On a read-only mount that is exactly what we want — the failure is
  # recoverable and, crucially, visible to the watchdog. On a read-write mount
  # the same EIO can surface mid-write, so `soft` there is a data-durability
  # decision an operator has to make deliberately, not inherit from a default
  # written for read-only Drive browsing. Fail closed rather than gate silently:
  # a silent per-org downgrade would be the more dangerous behaviour, because
  # nothing would tell anyone that promoting an org had changed write semantics.
  writableUnits = lib.filter (u: !u.readOnly) emission.units;

  softMountViolations =
    lib.optional
      (
        backend == "nfsmount"
        && lib.elem "soft" settings.nfsMountOptions
        && writableUnits != [ ]
        && !cfg.allowSoftReadWrite
      )
      (lib.concatStringsSep ", " (map (u: "${u.org.name}-${u.mount.name}") writableUnits));

  storeViolations = lib.concatMap (
    name:
    let
      sec = cfg.secrets.${name};
    in
    optional (lib.hasPrefix builtins.storeDir sec.clientFile) "${name}.clientFile"
    ++ optional (lib.hasPrefix builtins.storeDir sec.tokenFile) "${name}.tokenFile"
  ) secretNames;

  secretType = types.submodule {
    options = {
      clientFile = mkOption {
        type = types.str;
        example = "config.sops.secrets.\"gdrive-mounts/sulliwood/client\".path";
        description = ''
          Runtime path of the GCP OAuth Desktop client download, verbatim JSON.
          A string, never a path literal: a path literal copies plaintext into
          the world-readable nix store.
        '';
      };
      tokenFile = mkOption {
        type = types.str;
        example = "config.sops.secrets.\"gdrive-mounts/sulliwood/token\".path";
        description = ''
          Runtime path of the `rclone authorize` output, verbatim JSON. A string
          for the same reason as clientFile.
        '';
      };
    };
  };
in
{
  options.programs.gdrive-mounts = {
    enable = mkEnableOption "rclone-mounted Google Workspace drives";

    platform = mkOption {
      type = types.enum [
        "darwin"
        "linux"
      ];
      default = if pkgs.stdenv.hostPlatform.isDarwin then "darwin" else "linux";
      defaultText = literalMD "`darwin` on a Darwin host, `linux` otherwise";
      internal = true;
      visible = false;
      description = ''
        Unit flavour to emit. This is the only host-platform read in the module;
        the eval fixture flips it so one runner exercises both branches.
      '';
    };

    package = mkOption {
      type = types.package;
      default = pkgs.rclone;
      defaultText = literalExpression "pkgs.rclone";
      description = "rclone package. nixpkgs is the delivery vehicle; brew is not.";
    };

    toolsPackage = mkOption {
      type = types.package;
      example = literalExpression "inputs.gdrive-mounts.packages.\${pkgs.system}.default";
      description = "gdrive-mounts tools package. The consumer wrapper passes this flake's packages.default.";
    };

    orgsFile = mkOption {
      type = types.path;
      default = ../../orgs.json;
      defaultText = literalMD "this flake's `orgs.json`";
      description = ''
        Non-secret org registry (schema: config/orgs.schema.json). Every unit
        passes this exact path to the CLIs, so the copy inside toolsPackage is
        never the effective registry.
      '';
    };

    secrets = mkOption {
      type = types.attrsOf secretType;
      default = { };
      description = ''
        Per-org secret file paths, keyed by org name. Two whole-document JSON
        files per org. An enabled org with no entry emits a warning and no units.
      '';
    };

    stateDir = mkOption {
      type = types.str;
      default = fromRegistry.stateDir;
      defaultText = literalMD "`orgs.json` `defaults.stateDir`";
      description = "Runtime state root. Holds rclone-<org>.conf (0600) and the error breadcrumbs.";
    };

    mountRoot = mkOption {
      type = types.str;
      default = fromRegistry.mountRoot;
      defaultText = literalMD "`orgs.json` `defaults.mountRoot`";
      description = "Parent of every mountpoint and of every link.";
    };

    cacheRoot = mkOption {
      type = types.str;
      default = fromRegistry.cacheRoot;
      defaultText = literalMD "`orgs.json` `defaults.cacheRoot`";
      description = "VFS cache root. Each org gets <cacheRoot>/<org>.";
    };

    cacheMaxSize = mkOption {
      type = types.str;
      default = fromRegistry.cacheMaxSize;
      defaultText = literalMD "`orgs.json` `defaults.cacheMaxSize`";
      description = "rclone --vfs-cache-max-size.";
    };

    vfsCacheMode = mkOption {
      type = types.enum [
        "off"
        "minimal"
        "writes"
        "full"
      ];
      default = fromRegistry.vfsCacheMode;
      defaultText = literalMD "`orgs.json` `defaults.vfsCacheMode`";
      description = "rclone --vfs-cache-mode.";
    };

    dirCacheTime = mkOption {
      type = types.str;
      default = fromRegistry.dirCacheTime;
      defaultText = literalMD "`orgs.json` `defaults.dirCacheTime`";
      description = "rclone --dir-cache-time.";
    };

    vfsReadAhead = mkOption {
      type = types.str;
      default = fromRegistry.vfsReadAhead;
      defaultText = literalMD "`orgs.json` `defaults.vfsReadAhead`";
      description = "rclone --vfs-read-ahead.";
    };

    nfsCacheHandleLimit = mkOption {
      type = types.int;
      default = fromRegistry.nfsCacheHandleLimit;
      defaultText = literalMD "`orgs.json` `defaults.nfsCacheHandleLimit`";
      description = "rclone --nfs-cache-handle-limit. Emitted only when the backend is nfsmount.";
    };

    backendDarwin = mkOption {
      type = types.enum [
        "nfsmount"
        "mount"
      ];
      default = fromRegistry.backendDarwin;
      defaultText = literalMD "`orgs.json` `defaults.mountBackendDarwin`";
      description = "rclone subcommand on Darwin.";
    };

    backendLinux = mkOption {
      type = types.enum [ "mount" ];
      default = fromRegistry.backendLinux;
      defaultText = literalMD "`orgs.json` `defaults.mountBackendLinux`";
      description = "rclone subcommand on Linux.";
    };

    ioTimeout = mkOption {
      type = types.str;
      default = fromRegistry.ioTimeout;
      defaultText = literalMD "`orgs.json` `defaults.ioTimeout`";
      description = ''
        rclone `--timeout` (IO idle). Deliberately far below rclone's own 5m
        default: macOS marks an NFS mount "not responding" after
        `vfs.generic.nfs.client.initialdowndelay` seconds, which is 5, and
        `hard,nointr` then makes that state permanent.
      '';
    };

    connectTimeout = mkOption {
      type = types.str;
      default = fromRegistry.connectTimeout;
      defaultText = literalMD "`orgs.json` `defaults.connectTimeout`";
      description = "rclone --contimeout. Same budget as ioTimeout.";
    };

    lowLevelRetries = mkOption {
      type = types.int;
      default = fromRegistry.lowLevelRetries;
      defaultText = literalMD "`orgs.json` `defaults.lowLevelRetries`";
      description = "rclone --low-level-retries. Multiplies ioTimeout into the worst-case stall.";
    };

    attrTimeout = mkOption {
      type = types.str;
      default = fromRegistry.attrTimeout;
      defaultText = literalMD "`orgs.json` `defaults.attrTimeout`";
      description = "rclone --attr-timeout.";
    };

    pollInterval = mkOption {
      type = types.str;
      default = fromRegistry.pollInterval;
      defaultText = literalMD "`orgs.json` `defaults.pollInterval`";
      description = "rclone --poll-interval for backend change notification.";
    };

    logLevel = mkOption {
      type = types.enum [
        "DEBUG"
        "INFO"
        "NOTICE"
        "ERROR"
      ];
      default = fromRegistry.logLevel;
      defaultText = literalMD "`orgs.json` `defaults.logLevel`";
      description = ''
        rclone --log-level. rclone logs low-level retries and pacer backoff at
        INFO, so at the NOTICE default a stalling mount genuinely logs nothing.
      '';
    };

    statsInterval = mkOption {
      type = types.str;
      default = fromRegistry.statsInterval;
      defaultText = literalMD "`orgs.json` `defaults.statsInterval`";
      description = ''
        rclone --stats. `"0"` disables the periodic stats block. launchd never
        rotates a log, and the watchdog heartbeat already carries liveness.
      '';
    };

    nfsMountOptions = mkOption {
      type = types.listOf types.str;
      default = fromRegistry.nfsMountOptions;
      defaultText = literalMD "`orgs.json` `defaults.nfsMountOptions`";
      example = literalExpression ''[ "intr" "timeo=100" "retrans=5" "dumbtimer" ]'';
      description = ''
        NFS client mount options, emitted one per `--option`. Emitted only when
        the backend is `nfsmount`; on `rclone mount` the same flag means libfuse
        options, which these are not.

        These are not interpreted by rclone. On Darwin `rclone nfsmount` runs
        `mount -o port=N -o mountport=N -o tcp <our options> localhost:/ <point>`,
        and `mount(8)` execs `/sbin/mount_nfs` for a `host:/path` special — so
        they land in the kernel NFS client. rclone hardcodes only
        `port`/`mountport`/`tcp`; everything else, including `hard` and
        `nointr`, is a macOS default it never chose.

        The default trades rclone's stubbornness for a mount that can recover:
        `soft`+`intr` make a stalled call fail with `EIO` and make its caller
        killable, instead of blocking forever in the kernel where no signal is
        delivered — the state that made the 2026-08-19 wedge bistable and left
        the watchdog unable even to probe it. `timeo` is in **tenths of a
        second** on macOS, so `timeo=100` is 10s, and `dumbtimer` keeps the
        dynamic retransmit estimator from deriving a microsecond timeout from
        the loopback round-trip.

        `soft` can surface a stalled write as an I/O error, so it is refused on
        a read-write org (see the assertion). Confirm any change actually landed
        with `nfsstat -m`; never assume it did.

        This list is fleet-wide. A unit whose org resolves read-only also gets
        `rdonly` appended, which is per-org and not settable here: without it
        rclone's `--read-only` refusal reaches the caller as `NFS3ERR_ACCES`,
        `MNT_RDONLY` is never set, and macOS reports a per-file permission
        error rather than a read-only volume.
      '';
    };

    allowSoftReadWrite = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Permit `soft` in `nfsMountOptions` while a read-write mount exists.

        Off by default, and the resulting assertion is the point: `soft` turns a
        stalled RPC into `EIO`, which is the right trade for read-only Drive
        browsing and a data-durability decision on a mount that accepts writes.
        Promoting an org to `scope = "drive"` should not silently change what
        happens to an in-flight write.
      '';
    };

    remoteControl.enable = mkOption {
      type = types.bool;
      default = fromRegistry.remoteControl;
      defaultText = literalMD "`orgs.json` `defaults.remoteControl`";
      description = ''
        Serve rclone's rc API for each mount on a unix socket inside `stateDir`.
        Never a TCP port: `--rc-no-auth` on loopback is reachable by every local
        process, and rc can drive the VFS. The socket is created world-
        connectable (`srwxr-xr-x`), so the 0700 `stateDir` is the access control.

        The watchdog uses it to read `core/stats`, `core/pid` and `vfs/stats`
        while a mount is wedged — over a channel that never touches the wedged
        path, which is what separates "rclone is dead" from "rclone is alive and
        the NFS layer went quiet".
      '';
    };

    watchdog = {
      enable = mkOption {
        type = types.bool;
        default = fromRegistry.watchdogEnable;
        defaultText = literalMD "`orgs.json` `defaults.watchdogEnable`";
        description = ''
          Run a health-probe sidecar unit per mount. It probes from outside,
          confirms across consecutive cycles, captures, and restarts the mount
          unit. Without it, a mount that stops serving while every process
          involved stays alive is never restarted — nothing has failed.
        '';
      };
      intervalSec = mkOption {
        type = types.int;
        default = fromRegistry.watchdogIntervalSec;
        defaultText = literalMD "`orgs.json` `defaults.watchdogIntervalSec`";
        description = "Seconds between probes.";
      };
      probeTimeoutSec = mkOption {
        type = types.int;
        default = fromRegistry.watchdogProbeTimeoutSec;
        defaultText = literalMD "`orgs.json` `defaults.watchdogProbeTimeoutSec`";
        description = "How long a bounded filesystem probe may take before it counts as a failure.";
      };
      failureThreshold = mkOption {
        type = types.int;
        default = fromRegistry.watchdogFailureThreshold;
        defaultText = literalMD "`orgs.json` `defaults.watchdogFailureThreshold`";
        description = ''
          Consecutive failed probes before the watchdog acts. Never 1: the
          macOS "not responding" flag is observed to set and clear on its own
          within a single minute.
        '';
      };
      restartFloorSec = mkOption {
        type = types.int;
        default = fromRegistry.watchdogRestartFloorSec;
        defaultText = literalMD "`orgs.json` `defaults.watchdogRestartFloorSec`";
        description = ''
          Minimum seconds between watchdog restarts of one mount. Held wedges
          are still written to the wedge record, so the floor suppresses the
          restart, never the evidence.
        '';
      };
    };

    extraMountFlags = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = literalExpression ''[ "--log-level" "INFO" ]'';
      description = "Extra argv appended to every rclone mount.";
    };

    extraRuntimeInputs = mkOption {
      type = types.listOf types.package;
      default = [ ];
      example = literalExpression "[ pkgs.fuse3 ]";
      description = "Extra packages placed on the PATH of every generated wrapper.";
    };

    cache = {
      waitSeconds = mkOption {
        type = types.int;
        default = 120;
        description = "How long a mount waits for the cache volume before it fails loud with exit 78.";
      };
      requireMountpoint = mkOption {
        type = types.bool;
        default = lib.hasPrefix "/Volumes/" cfg.cacheRoot || lib.hasPrefix "/mnt/" cfg.cacheRoot;
        defaultText = literalMD "true when `cacheRoot` sits under `/Volumes/` or `/mnt/`";
        description = ''
          Require the cache volume to be a real mountpoint, not an empty
          directory on the boot disk.
        '';
      };
    };

    renderOnActivation = mkOption {
      type = types.bool;
      default = true;
      description = "Render the per-org configs during activation, best-effort. A missing secret warns; it does not fail the switch.";
    };

    activationAfter = mkOption {
      type = types.listOf types.str;
      default = [ "writeBoundary" ];
      example = literalExpression ''[ "writeBoundary" "sops-nix" ]'';
      description = "Activation entries this module's render and link steps run after. sops-nix's home-manager module names its entry `sops-nix` (`setupSecrets` is the NixOS name; an unknown name is silently ignored by the DAG).";
    };

    index = {
      enable = mkEnableOption "scheduled rclone lsjson metadata index" // {
        default = true;
      };
      intervalSec = mkOption {
        type = types.int;
        default = 21600;
        description = "Seconds between index refreshes.";
      };
      stateDir = mkOption {
        type = types.str;
        default = fromRegistry.indexStateDir;
        defaultText = literalMD "`orgs.json` `defaults.indexStateDir`";
        description = "Where the index writes its json and sqlite artifacts.";
      };
      freshnessSloHours = mkOption {
        type = types.int;
        default = fromRegistry.indexFreshnessSloHours;
        defaultText = literalMD "`orgs.json` `defaults.indexFreshnessSloHours`";
        description = "Age at which doctor calls the index stale.";
      };
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      home.packages = [
        cfg.package
        cfg.toolsPackage
      ];

      assertions = [
        {
          assertion = storeViolations == [ ];
          message = ''
            programs.gdrive-mounts: secret path(s) ${lib.concatStringsSep ", " storeViolations}
            point into ${builtins.storeDir}. Pass a runtime path such as
            config.sops.secrets."gdrive-mounts/<org>/client".path, never a path literal.
          '';
        }
        {
          assertion = softMountViolations == [ ];
          message = ''
            programs.gdrive-mounts: nfsMountOptions contains `soft`, but mount(s)
            ${lib.concatStringsSep ", " softMountViolations} are read-write.

            A soft mount fails a stalled RPC with EIO instead of blocking, which is
            what we want for read-only Drive browsing — but that EIO can also land
            mid-write. Decide deliberately:

              # keep hard semantics everywhere (safe, but a stall stays permanent)
              programs.gdrive-mounts.nfsMountOptions =
                [ "intr" "timeo=100" "retrans=5" "dumbtimer" ];

            or, having considered the write path, accept the trade explicitly:

              programs.gdrive-mounts.allowSoftReadWrite = true;
          '';
        }
      ];

      warnings = optional (emission.unwired != [ ]) ''
        programs.gdrive-mounts: enabled org(s) ${lib.concatStringsSep ", " emission.unwired} have no
        `secrets` entry. Their mounts are skipped. Seed the secret files and pass the paths,
        or set enabled=false in orgs.json.
      '';
    }

    {
      home.activation.gdriveMountsSettings = lib.hm.dag.entryAfter cfg.activationAfter settingsActivation;
    }

    (mkIf (cfg.renderOnActivation && emission.wired != [ ]) {
      home.activation.gdriveMountsRender = lib.hm.dag.entryAfter cfg.activationAfter renderActivation;
    })

    (mkIf (emission.links != [ ]) {
      home.activation.gdriveMountsLinks = lib.hm.dag.entryAfter cfg.activationAfter linkActivation;
    })

    (mkIf (cfg.platform == "darwin") { launchd.agents = mountAgents; })
    (mkIf (cfg.platform == "linux") { systemd.user.services = mountServices; })

    (mkIf (cfg.watchdog.enable && emission.units != [ ]) (mkMerge [
      (mkIf (cfg.platform == "darwin") { launchd.agents = watchdogAgents; })
      (mkIf (cfg.platform == "linux") { systemd.user.services = watchdogServices; })
    ]))

    (mkIf (cfg.index.enable && emission.wired != [ ]) (mkMerge [
      (mkIf (cfg.platform == "darwin") {
        launchd.agents.gdrive-mounts-index = {
          enable = true;
          config = {
            Label = "dev.tinyland.gdrive-mounts.index";
            ProgramArguments = [ "${indexScript}" ];
            StartInterval = cfg.index.intervalSec;
            RunAtLoad = true;
            ProcessType = "Background";
            EnvironmentVariables = {
              PATH = runtimePath;
            };
            StandardOutPath = "${logDir}/gdrive-mounts.index.out.log";
            StandardErrorPath = "${logDir}/gdrive-mounts.index.err.log";
          };
        };
      })
      (mkIf (cfg.platform == "linux") {
        systemd.user.services.gdrive-mounts-index = {
          Unit.Description = "gdrive-mounts metadata index refresh";
          Service = {
            Type = "oneshot";
            ExecStart = "${indexScript}";
            Environment = [ "PATH=${runtimePath}" ];
          };
        };
        systemd.user.timers.gdrive-mounts-index = {
          Unit.Description = "gdrive-mounts metadata index timer";
          Timer = {
            OnBootSec = 300;
            OnUnitActiveSec = cfg.index.intervalSec;
            Persistent = true;
          };
          Install.WantedBy = [ "timers.target" ];
        };
      })
    ]))
  ]);
}
