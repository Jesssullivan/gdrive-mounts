# programs.gdrive-mounts — rclone-mounted Google Workspace drives.
#
# Purity rule (house doctrine, cf. linear-gsuite): this module holds NO secrets
# and references NO consumer paths. The consumer (lab) passes sops-rendered
# file paths in via `secrets`; missing leaves SKIP the org's agents with a
# warning instead of failing activation (presence-gated).
#
# Substrate: macOS mounts default to `rclone nfsmount` (native NFS loopback,
# kext-free); Linux uses `rclone mount` (FUSE3). Index agents run
# gdrive-mounts-gdrive-index on an interval — Spotlight does not index these
# mounts; the sqlite index is the agent/AX query surface.
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.gdrive-mounts;
  inherit (lib)
    mkEnableOption mkOption mkIf mkMerge types mapAttrs' nameValuePair
    filter concatMap optional;
  orgsData = builtins.fromJSON (builtins.readFile cfg.orgsFile);
  enabledOrgs = filter (o: o.enabled or false) orgsData.orgs;
  wiredOrgs = filter (o: builtins.hasAttr o.name cfg.secrets) enabledOrgs;
  unwiredNames = map (o: o.name) (filter (o: !(builtins.hasAttr o.name cfg.secrets)) enabledOrgs);
  backend = if pkgs.stdenv.isDarwin then cfg.backendDarwin else "mount";

  secretType = types.submodule {
    options = {
      clientIdFile = mkOption { type = types.path; };
      clientSecretFile = mkOption { type = types.path; };
      tokenFile = mkOption { type = types.path; };
    };
  };

  mountScript = org: m:
    let
      sec = cfg.secrets.${org.name};
      mountPoint = "${cfg.mountRoot}/${m.mountSuffix}";
      cacheDir = "${cfg.cacheDir}/${org.name}";
    in
    pkgs.writeShellScript "gdrive-mount-${org.name}-${m.name}" ''
      set -euo pipefail
      export ${org.secretEnvPrefix}_CLIENT_ID_FILE="${sec.clientIdFile}"
      export ${org.secretEnvPrefix}_CLIENT_SECRET_FILE="${sec.clientSecretFile}"
      export ${org.secretEnvPrefix}_TOKEN_FILE="${sec.tokenFile}"
      state="''${XDG_STATE_HOME:-$HOME/.local/state}/gdrive-mounts"
      mkdir -p "$state" "${mountPoint}" "${cacheDir}"
      ${cfg.toolsPackage}/bin/gdrive-mounts-render-config \
        "${cfg.orgsFile}" \
        "${cfg.toolsPackage}/share/gdrive-mounts/rclone.conf.template" \
        "$state/rclone.conf"
        # remotePath may be empty (whole-drive mount)
      remote_path='${m.remotePath}'
      if [ -n "$remote_path" ]; then src="${org.remote}:$remote_path"; else src="${org.remote}:"; fi
      exec ${cfg.package}/bin/rclone ${backend} "$src" "${mountPoint}" \
        --config "$state/rclone.conf" \
        --vfs-cache-mode ${cfg.vfsCacheMode} \
        --cache-dir "${cacheDir}" \
        --vfs-cache-max-size ${cfg.cacheMaxSize} \
        --dir-cache-time ${cfg.dirCacheTime} \
        --nfs-cache-handle-limit ${toString cfg.nfsCacheHandleLimit} \
        --volname "gdrive-${org.name}-${m.name}" \
        ${lib.concatStringsSep " " cfg.extraMountFlags}
    '';

  mountAgents = lib.listToAttrs (concatMap (org:
    map (m: nameValuePair "gdrive-mounts-${org.name}-${m.name}" {
      enable = true;
      config = {
        Label = "dev.tinyland.gdrive-mounts.${org.name}-${m.name}";
        ProgramArguments = [ "${mountScript org m}" ];
        RunAtLoad = true;
        KeepAlive = { SuccessfulExit = false; };
        ThrottleInterval = 30;
        ProcessType = "Background";
        StandardOutPath = "${config.home.homeDirectory}/Library/Logs/tinyland/gdrive-mounts.${org.name}-${m.name}.out.log";
        StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/tinyland/gdrive-mounts.${org.name}-${m.name}.err.log";
      };
    }) org.mounts) wiredOrgs);

  mountServices = lib.listToAttrs (concatMap (org:
    map (m: nameValuePair "gdrive-mounts-${org.name}-${m.name}" {
      Unit = {
        Description = "rclone mount ${org.remote}:${m.remotePath}";
        After = [ "network-online.target" ];
      };
      Service = {
        ExecStart = "${mountScript org m}";
        Restart = "on-failure";
        RestartSec = 30;
      };
      Install.WantedBy = [ "default.target" ];
    }) org.mounts) wiredOrgs);
in
{
  options.programs.gdrive-mounts = {
    enable = mkEnableOption "rclone-mounted Google Workspace drives";

    package = mkOption {
      type = types.package;
      default = pkgs.rclone;
      description = "rclone package (nixpkgs; brew is not the delivery vehicle).";
    };

    toolsPackage = mkOption {
      type = types.package;
      description = "gdrive-mounts tools package (this flake's packages.default, passed by the consumer wrapper).";
    };

    orgsFile = mkOption {
      type = types.path;
      default = ../../orgs.json;
      description = "Non-secret org registry (schema: config/orgs.schema.json).";
    };

    secrets = mkOption {
      type = types.attrsOf secretType;
      default = { };
      description = "Per-org sops-rendered secret file paths, keyed by org name. Consumer passes config.sops.secrets.<name>.path.";
    };

    mountRoot = mkOption {
      type = types.str;
      default = "${config.home.homeDirectory}/GDrive";
    };

    cacheDir = mkOption {
      type = types.str;
      default = "${config.home.homeDirectory}/.cache/gdrive-mounts";
    };

    cacheMaxSize = mkOption { type = types.str; default = "100G"; };
    vfsCacheMode = mkOption { type = types.enum [ "off" "minimal" "writes" "full" ]; default = "full"; };
    dirCacheTime = mkOption { type = types.str; default = "720h"; };
    nfsCacheHandleLimit = mkOption { type = types.int; default = 250000; };
    backendDarwin = mkOption { type = types.enum [ "nfsmount" "mount" ]; default = "nfsmount"; };
    extraMountFlags = mkOption { type = types.listOf types.str; default = [ ]; };

    index = {
      enable = mkEnableOption "scheduled rclone lsjson metadata index" // { default = true; };
      intervalSec = mkOption { type = types.int; default = 21600; };
    };
  };

  config = mkIf cfg.enable (mkMerge [
    {
      home.packages = [ cfg.package cfg.toolsPackage ];
      warnings = optional (unwiredNames != [ ]) ''
        programs.gdrive-mounts: enabled org(s) ${lib.concatStringsSep ", " unwiredNames} have no
        `secrets` entry — their mount agents are SKIPPED (presence-gated). Seed the sops leaves
        in lab and pass the paths, or set enabled=false in orgs.json.
      '';
    }

    (mkIf pkgs.stdenv.isDarwin { launchd.agents = mountAgents; })
    (mkIf pkgs.stdenv.isLinux { systemd.user.services = mountServices; })

    (mkIf cfg.index.enable (mkMerge [
      (mkIf pkgs.stdenv.isDarwin {
        launchd.agents.gdrive-mounts-index = {
          enable = true;
          config = {
            Label = "dev.tinyland.gdrive-mounts.index";
            ProgramArguments = [
              "${cfg.toolsPackage}/bin/gdrive-mounts-gdrive-index"
              "${cfg.orgsFile}"
            ];
            StartInterval = cfg.index.intervalSec;
            RunAtLoad = false;
            ProcessType = "Background";
            StandardOutPath = "${config.home.homeDirectory}/Library/Logs/tinyland/gdrive-mounts.index.out.log";
            StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/tinyland/gdrive-mounts.index.err.log";
          };
        };
      })
      (mkIf pkgs.stdenv.isLinux {
        systemd.user.services.gdrive-mounts-index = {
          Unit.Description = "gdrive-mounts metadata index refresh";
          Service = {
            Type = "oneshot";
            ExecStart = "${cfg.toolsPackage}/bin/gdrive-mounts-gdrive-index ${cfg.orgsFile}";
          };
        };
        systemd.user.timers.gdrive-mounts-index = {
          Unit.Description = "gdrive-mounts metadata index timer";
          Timer.OnUnitActiveSec = cfg.index.intervalSec;
          Install.WantedBy = [ "timers.target" ];
        };
      })
    ]))
  ]);
}
