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
      ;
    indexStateDir = cfg.index.stateDir;
    indexFreshnessSloHours = cfg.index.freshnessSloHours;
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

  sweepStaleMount =
    point:
    if cfg.platform == "darwin" then
      "umount -f ${escapeShellArg point} >/dev/null 2>&1 || true"
    else
      "fusermount3 -u ${escapeShellArg point} >/dev/null 2>&1 || umount -f ${escapeShellArg point} >/dev/null 2>&1 || true";

  mountScript =
    u:
    pkgs.writeShellScript "gdrive-mount-${u.name}" ''
      set -euo pipefail
      export PATH=${escapeShellArg runtimePath}
      ${secretExports u.org}
      state=${escapeShellArg settings.stateDir}
      conf=${escapeShellArg u.conf}
      point=${escapeShellArg u.point}
      cache=${escapeShellArg u.cache}
      volume=${escapeShellArg cacheVolume}
      err="$state/last-error.${u.org.name}-${u.mount.name}"

      mkdir -p "$state"
      chmod 700 "$state" 2>/dev/null || true
      ${lib.optionalString (!cfg.cache.requireMountpoint) ''mkdir -p "$volume" >/dev/null 2>&1 || true''}

      # Cache-volume guard: bounded wait, then fail loud. The cache never spills
      # onto the boot disk.
      waited=0
      while :; do
        ready=1
        if [ ! -d "$volume" ] || [ ! -w "$volume" ]; then ready=0; fi
        ${lib.optionalString cfg.cache.requireMountpoint ''
          if [ "$ready" = 1 ] && [ "$(stat -c %d "$volume")" = "$(stat -c %d "$volume/..")" ]; then ready=0; fi
        ''}
        if [ "$ready" = 1 ]; then break; fi
        if [ "$waited" -ge ${toString cfg.cache.waitSeconds} ]; then
          printf 'FATAL gdrive-mounts %s: cache volume %s absent after %s seconds\n' \
            ${escapeShellArg u.name} "$volume" ${toString cfg.cache.waitSeconds} >&2
          printf '%s cache volume %s absent after %s seconds\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$volume" ${toString cfg.cache.waitSeconds} > "$err"
          exit 78
        fi
        sleep 5
        waited=$((waited + 5))
      done

      # Clear a stale mount handle left by a killed rclone, then take the point.
      if ! stat "$point" >/dev/null 2>&1; then
        ${sweepStaleMount u.point}
      fi
      mkdir -p "$point" "$cache"
      rm -f "$err"

      # Render only when the conf is missing. rclone owns the file after start:
      # it writes refreshed tokens back into it, and a re-render would undo them.
      if [ ! -s "$conf" ]; then
        ${renderBin} \
          --org ${escapeShellArg u.org.name} \
          --orgs ${escapeShellArg orgsFileArg} \
          --template ${escapeShellArg templateArg} \
          --out "$conf"
      fi

      exec ${cfg.package}/bin/rclone ${escapeShellArgs u.args}
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

  indexScript = pkgs.writeShellScript "gdrive-mounts-index" ''
    set -euo pipefail
    export PATH=${escapeShellArg runtimePath}
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

  # Out-of-store symlinks: the target is a live mountpoint that does not exist
  # until rclone starts, so this is `ln -sfn`, not home.file.
  linkActivation = ''
    $DRY_RUN_CMD mkdir -p ${escapeShellArg settings.mountRoot}
    ${lib.concatMapStrings (
      l: "$DRY_RUN_CMD ln -sfn ${escapeShellArg l.target} ${escapeShellArg l.path}\n"
    ) emission.links}
  '';

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
      ];

      warnings = optional (emission.unwired != [ ]) ''
        programs.gdrive-mounts: enabled org(s) ${lib.concatStringsSep ", " emission.unwired} have no
        `secrets` entry. Their mounts are skipped. Seed the secret files and pass the paths,
        or set enabled=false in orgs.json.
      '';
    }

    (mkIf (cfg.renderOnActivation && emission.wired != [ ]) {
      home.activation.gdriveMountsRender = lib.hm.dag.entryAfter cfg.activationAfter renderActivation;
    })

    (mkIf (emission.links != [ ]) {
      home.activation.gdriveMountsLinks = lib.hm.dag.entryAfter cfg.activationAfter linkActivation;
    })

    (mkIf (cfg.platform == "darwin") { launchd.agents = mountAgents; })
    (mkIf (cfg.platform == "linux") { systemd.user.services = mountServices; })

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
