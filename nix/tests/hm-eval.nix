# hm-eval — eval proof for programs.gdrive-mounts.
#
# Two layers:
#   1. eval. lib.evalModules against a stub base module, once per platform.
#      A failed check throws by name, so `nix eval .#checks.<system>.hm-eval.drvPath`
#      is already the gate. No builder needed.
#   2. build. runCommand greps the wrapper scripts the module actually generated,
#      so the argv the tests assert is the argv that ships.
#
# One instance per system evaluates both platform branches, so an x86_64-linux
# runner still proves the Darwin units.
{
  pkgs,
  module,
  orgsFile,
  prodOrgsFile ? ../../orgs.json,
}:
let
  # home-manager injects lib.hm; a bare evalModules does not. Stub the dag
  # helpers the module uses, or home.activation fails on `attribute 'hm' missing`.
  lib = pkgs.lib.extend (
    final: prev: {
      hm = (prev.hm or { }) // {
        dag = {
          entryAnywhere = data: {
            inherit data;
            after = [ ];
            before = [ ];
          };
          entryAfter = after: data: {
            inherit data after;
            before = [ ];
          };
          entryBefore = before: data: {
            inherit data before;
            after = [ ];
          };
        };
      };
    }
  );

  inherit (lib)
    mkOption
    types
    elem
    filter
    head
    hasInfix
    escapeShellArg
    ;

  plan = import ../lib/plan.nix { inherit lib; };

  homeDir = "/home/tester";

  stubBase = {
    options = {
      home.homeDirectory = mkOption { type = types.str; };
      home.packages = mkOption {
        type = types.listOf types.package;
        default = [ ];
      };
      home.activation = mkOption {
        type = types.attrsOf types.unspecified;
        default = { };
      };
      launchd.agents = mkOption {
        type = types.attrsOf types.unspecified;
        default = { };
      };
      systemd.user.services = mkOption {
        type = types.attrsOf types.unspecified;
        default = { };
      };
      systemd.user.timers = mkOption {
        type = types.attrsOf types.unspecified;
        default = { };
      };
      warnings = mkOption {
        type = types.listOf types.str;
        default = [ ];
      };
      assertions = mkOption {
        type = types.listOf types.unspecified;
        default = [ ];
      };
    };
    config.home.homeDirectory = homeDir;
  };

  # Stubs. No real rclone, no real tools package: this check must not pull a Go
  # build into the eval.
  stubRclone = pkgs.writeShellScriptBin "rclone" "exit 0";
  stubTools = pkgs.runCommand "gdrive-mounts-tools-stub" { } ''
    mkdir -p $out/bin $out/share/gdrive-mounts
    for n in render-config gdrive-index; do
      printf '#!/bin/sh\nexit 0\n' > "$out/bin/gdrive-mounts-$n"
      chmod 0755 "$out/bin/gdrive-mounts-$n"
    done
    touch $out/share/gdrive-mounts/rclone.conf.template
  '';

  evalWith =
    {
      platform,
      secrets,
      orgs ? orgsFile,
    }:
    (lib.evalModules {
      specialArgs = { inherit pkgs lib; };
      modules = [
        stubBase
        module
        {
          programs.gdrive-mounts = {
            enable = true;
            inherit platform secrets;
            package = stubRclone;
            toolsPackage = stubTools;
            orgsFile = orgs;
          };
        }
      ];
    }).config;

  # Dummy runtime paths. Never a store path, never a real secret.
  dummySecret = org: {
    clientFile = "/run/secrets/gdrive-mounts/${org}/client.json";
    tokenFile = "/run/secrets/gdrive-mounts/${org}/token.json";
  };

  wiredSecrets = {
    sulliwood = dummySecret "sulliwood";
    rwlab = dummySecret "rwlab";
  };

  darwinCfg = evalWith {
    platform = "darwin";
    secrets = wiredSecrets;
  };
  linuxCfg = evalWith {
    platform = "linux";
    secrets = wiredSecrets;
  };
  unwiredCfg = evalWith {
    platform = "darwin";
    secrets = { };
  };
  storeCfg = evalWith {
    platform = "darwin";
    secrets = {
      sulliwood = {
        clientFile = "${builtins.storeDir}/0000-client.json";
        tokenFile = "/run/secrets/gdrive-mounts/sulliwood/token.json";
      };
    };
  };

  # Same code path the module uses, so deleting a flag in plan.nix fails here.
  testData = builtins.fromJSON (builtins.readFile orgsFile);
  testSettings = plan.settingsFrom {
    home = homeDir;
    defaults = testData.defaults;
  };
  orgNamed = name: head (filter (o: o.name == name) testData.orgs);
  argsOf =
    platform: name:
    let
      org = orgNamed name;
    in
    plan.mountArgs {
      inherit platform org;
      settings = testSettings;
      mount = head org.mounts;
    };

  # Derived cover for the production registry: units == sum of the mounts of
  # every enabled org, plus one index unit. No count is hardcoded.
  prodData = builtins.fromJSON (builtins.readFile prodOrgsFile);
  prodEnabled = filter (o: o.enabled or false) prodData.orgs;
  prodSecrets = builtins.listToAttrs (
    map (o: {
      name = o.name;
      value = dummySecret o.name;
    }) prodEnabled
  );
  prodMountCount = lib.foldl' (acc: o: acc + builtins.length (o.mounts or [ ])) 0 prodEnabled;
  prodCfg = evalWith {
    platform = "darwin";
    secrets = prodSecrets;
    orgs = prodOrgsFile;
  };

  failing = cfg: filter (a: !a.assertion) cfg.assertions;
  agentNames = cfg: builtins.attrNames cfg.launchd.agents;
  serviceNames = cfg: builtins.attrNames cfg.systemd.user.services;

  darwinScript = name: head darwinCfg.launchd.agents.${name}.config.ProgramArguments;
  linuxScript = name: linuxCfg.systemd.user.services.${name}.Service.ExecStart;
  renderEntry = darwinCfg.home.activation.gdriveMountsRender;
  linkEntry = darwinCfg.home.activation.gdriveMountsLinks;

  checks = [
    {
      name = "darwin-emits-one-agent-per-mount-plus-index";
      ok = agentNames darwinCfg == [
        "gdrive-mounts-index"
        "gdrive-mounts-rwlab-root"
        "gdrive-mounts-sulliwood-root"
      ];
    }
    {
      name = "darwin-emits-no-systemd-units";
      ok = serviceNames darwinCfg == [ ] && builtins.attrNames darwinCfg.systemd.user.timers == [ ];
    }
    {
      name = "linux-emits-one-service-per-mount-plus-index";
      ok = serviceNames linuxCfg == [
        "gdrive-mounts-index"
        "gdrive-mounts-rwlab-root"
        "gdrive-mounts-sulliwood-root"
      ];
    }
    {
      name = "linux-emits-no-launchd-agents";
      ok = agentNames linuxCfg == [ ];
    }
    {
      name = "linux-index-timer-fires-on-boot-and-persists";
      ok =
        let
          t = linuxCfg.systemd.user.timers.gdrive-mounts-index.Timer;
        in
        t ? OnBootSec && t.Persistent && t.OnUnitActiveSec == 21600;
    }
    {
      name = "darwin-index-runs-at-load";
      ok = darwinCfg.launchd.agents.gdrive-mounts-index.config.RunAtLoad;
    }
    {
      name = "unwired-orgs-emit-no-units";
      ok = agentNames unwiredCfg == [ ] && serviceNames unwiredCfg == [ ];
    }
    {
      name = "unwired-orgs-warn-by-name";
      ok =
        builtins.length unwiredCfg.warnings == 1
        && hasInfix "sulliwood" (head unwiredCfg.warnings)
        && hasInfix "rwlab" (head unwiredCfg.warnings);
    }
    {
      name = "wired-orgs-do-not-warn";
      ok = darwinCfg.warnings == [ ];
    }
    {
      name = "c1-nfs-handle-limit-on-nfsmount";
      ok = elem "--nfs-cache-handle-limit" (argsOf "darwin" "sulliwood");
    }
    {
      name = "c1-no-nfs-handle-limit-on-mount";
      ok = !(elem "--nfs-cache-handle-limit" (argsOf "linux" "sulliwood"));
    }
    {
      name = "c4-read-only-for-readonly-scope";
      ok =
        elem "--read-only" (argsOf "darwin" "sulliwood") && elem "--read-only" (argsOf "linux" "sulliwood");
    }
    {
      name = "c4-no-read-only-for-drive-scope";
      ok = !(elem "--read-only" (argsOf "darwin" "rwlab"));
    }
    {
      name = "volname-is-darwin-only";
      ok = elem "--volname" (argsOf "darwin" "sulliwood") && !(elem "--volname" (argsOf "linux" "sulliwood"));
    }
    {
      name = "c5-one-config-per-org";
      ok =
        elem "${homeDir}/.local/state/gdrive-mounts/rclone-sulliwood.conf" (argsOf "darwin" "sulliwood")
        && elem "${homeDir}/.local/state/gdrive-mounts/rclone-rwlab.conf" (argsOf "darwin" "rwlab");
    }
    {
      name = "mountpoint-is-org-root";
      ok = elem "${homeDir}/GDrive/sulliwood" (argsOf "darwin" "sulliwood");
    }
    {
      name = "secrets-in-store-fail-assertion";
      ok = builtins.length (failing storeCfg) == 1;
    }
    {
      name = "runtime-secret-paths-pass-assertion";
      ok = failing darwinCfg == [ ];
    }
    {
      name = "links-become-out-of-store-symlinks";
      ok =
        let
          org = orgNamed "sulliwood";
          link = head org.links;
          target = plan.linkTarget testSettings org link;
          path = plan.linkPath testSettings org link;
        in
        target == "${homeDir}/GDrive/sulliwood/GFTB Stuff"
        && path == "${homeDir}/GDrive/sulliwood-gftb-stuff"
        && hasInfix "ln -sfn ${escapeShellArg target} ${escapeShellArg path}" linkEntry.data;
    }
    {
      name = "activation-entries-honour-activationAfter";
      ok = renderEntry.after == [ "writeBoundary" ] && linkEntry.after == [ "writeBoundary" ];
    }
    {
      name = "agents-carry-an-explicit-path";
      ok =
        hasInfix "/bin" darwinCfg.launchd.agents."gdrive-mounts-sulliwood-root".config.EnvironmentVariables.PATH
        && hasInfix "PATH=" (head linuxCfg.systemd.user.services."gdrive-mounts-sulliwood-root".Service.Environment);
    }
    {
      name = "production-orgs-json-unit-count-is-derived";
      ok = builtins.length (agentNames prodCfg) == prodMountCount + 1;
    }
  ];

  failures = map (c: c.name) (filter (c: !c.ok) checks);
in
if failures != [ ] then
  throw "hm-eval failed: ${lib.concatStringsSep ", " failures}"
else
  pkgs.runCommand "gdrive-mounts-hm-eval" { } ''
    set -eu
    dsul=${darwinScript "gdrive-mounts-sulliwood-root"}
    drw=${darwinScript "gdrive-mounts-rwlab-root"}
    lsul=${linuxScript "gdrive-mounts-sulliwood-root"}
    render=${pkgs.writeText "gdrive-mounts-render-activation" renderEntry.data}
    links=${pkgs.writeText "gdrive-mounts-link-activation" linkEntry.data}

    # C4 — scope is enforced by the mount, not only by the token.
    grep -q -- '--read-only' "$dsul"
    grep -q -- '--read-only' "$lsul"
    ! grep -q -- '--read-only' "$drw"

    # C1 — the nfs handle limit exists only on nfsmount.
    grep -q -- '--nfs-cache-handle-limit' "$dsul"
    ! grep -q -- '--nfs-cache-handle-limit' "$lsul"

    # --volname is documented macOS only.
    grep -q -- '--volname' "$dsul"
    ! grep -q -- '--volname' "$lsul"

    # C5 — one config per org, and no org sees another org's secrets.
    grep -q 'rclone-sulliwood.conf' "$dsul"
    ! grep -q 'rclone-rwlab.conf' "$dsul"
    grep -q 'GDM_SULLIWOOD_CLIENT_FILE' "$dsul"
    grep -q 'GDM_SULLIWOOD_TOKEN_FILE' "$dsul"
    ! grep -q 'GDM_RWLAB' "$dsul"

    # C6 — bounded wait then a loud, distinguishable exit.
    grep -qF 'mount | grep -qF " on ' "$dsul"
    grep -q 'umount -f' "$dsul"
    grep -q 'mountpoint -q' "$lsul"
    grep -q 'exit 78' "$dsul"
    grep -q 'last-error.sulliwood-root' "$dsul"
    # C6: the guard waits on the VOLUME mountpoint, not the cache root's parent
    # (the cache root sits in a user-owned subtree of a root-owned volume).
    grep -q "volume=/Volumes/TinylandSSD\b" "$dsul" || grep -q "volume='/Volumes/TinylandSSD'" "$dsul" || grep -q 'volume=/Volumes/TinylandSSD$' "$dsul"

    # Render happens only when the config is missing; rclone owns it afterwards.
    grep -q 'gdrive-mounts-render-config' "$dsul"
    grep -q -- '--org sulliwood' "$dsul"

    # Activation renders every wired org, best-effort, and links out of store.
    grep -q -- '--best-effort' "$render"
    grep -q -- '--out-dir' "$render"
    grep -q 'ln -sfn' "$links"

    touch $out
  ''
