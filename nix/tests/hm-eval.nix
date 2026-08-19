# hm-eval — eval proof for programs.gdrive-mounts.
#
# Three layers:
#   1. eval. lib.evalModules against a stub base module, once per platform.
#      A failed check throws by name, so `nix eval .#checks.<system>.hm-eval.drvPath`
#      is already the gate. No builder needed.
#   2. build. runCommand greps the wrapper scripts the module actually generated,
#      so the argv the tests assert is the argv that ships.
#   3. behaviour. The same guard text plan.nix hands the module is sourced into a
#      harness and *run* against synthetic directory trees, so the cache guard is
#      proved by execution, not only by grep. Layer 3 caught the `stat -f` trap
#      that layers 1 and 2 could not: GNU stat accepts `-f` and answers a
#      different question.
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
  settingsEntry = darwinCfg.home.activation.gdriveMountsSettings;

  # The guard text, straight from the builder the module uses. Layer 1 asserts
  # its shape; layer 3 runs it.
  guardFor =
    requireMountpoint:
    plan.cacheGuard {
      inherit requireMountpoint;
      waitSeconds = 0; # tests assert the fail-loud path, not the wait
      sleepSeconds = 1;
    };

  # usage: harness <cache> <volume> <errfile>
  guardHarness =
    requireMountpoint:
    pkgs.writeShellScript "gdrive-mounts-guard-harness" ''
      set -euo pipefail
      unit=harness
      cache="$1"
      volume="$2"
      err="$3"
      ${plan.shellPreamble}
      ${guardFor requireMountpoint}
      gdm_wait_for_cache
      printf 'GUARD-READY\n'
    '';

  # usage: probe dev|mountpoint|deepest <path> [floor]
  probeScript = pkgs.writeShellScript "gdrive-mounts-guard-probe" ''
    set -euo pipefail
    unit=probe
    ${plan.shellPreamble}
    case "$1" in
      dev) gdm_dev_of "$2" || true ;;
      mountpoint)
        if gdm_is_mountpoint "$2"; then printf 'yes'; else printf 'no'; fi
        ;;
      deepest) gdm_deepest_existing "$2" "$3" ;;
      *)
        printf 'unknown probe: %s\n' "$1" >&2
        exit 2
        ;;
    esac
    printf '\n'
  '';

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
      ok =
        renderEntry.after == [ "writeBoundary" ]
        && linkEntry.after == [ "writeBoundary" ]
        && settingsEntry.after == [ "writeBoundary" ];
    }
    {
      name = "activation-writes-effective-settings";
      ok =
        darwinCfg.home.activation ? gdriveMountsSettings
        && hasInfix "${homeDir}/.local/state/gdrive-mounts/effective-settings.json" settingsEntry.data;
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
    # The stat dialect is detected, not assumed: `stat -c` is an illegal option
    # to BSD stat, GNU's `-f` is `--file-system` (it succeeds and prints the
    # wrong thing), and the platform cannot decide it either — the wrapper's
    # PATH puts nix coreutils ahead of /usr/bin, so `stat` is GNU on Darwin too.
    {
      name = "stat-probe-detects-the-implementation";
      ok =
        let
          p = plan.shellPreamble;
        in
        hasInfix "stat --version >/dev/null 2>&1" p && hasInfix "stat -c %d" p && hasInfix "stat -f %d" p;
    }
    # The neo 2026-08-19 regression: an external volume root is root-owned, so
    # writability belongs to the cache subtree, not to the mountpoint itself.
    {
      name = "guard-tests-the-cache-ancestor-not-the-volume-root";
      ok =
        let
          g = guardFor true;
        in
        hasInfix "gdm_deepest_existing \"$cache\" \"$volume\"" g && !(hasInfix ''! -w "$volume"'' g);
    }
    {
      name = "guard-still-requires-a-real-mountpoint";
      ok =
        hasInfix "require_mountpoint=1" (guardFor true)
        && hasInfix "require_mountpoint=0" (guardFor false)
        && hasInfix "refusing to spill the cache onto the boot disk" (guardFor true);
    }
    {
      name = "guard-failure-names-the-path-its-mode-and-exits-78";
      ok =
        let
          g = guardFor true;
        in
        hasInfix "gdm_perm_of" g && hasInfix "is not writable by" g && hasInfix "exit 78" g;
    }
    {
      name = "mountpoint-probe-fails-closed-on-an-unreadable-stat";
      ok = hasInfix ''[ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ]'' plan.shellPreamble;
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
    settings=${pkgs.writeText "gdrive-mounts-settings-activation" settingsEntry.data}

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
    # C6: …and it tests the cache subtree for writability, never the root-owned
    # volume root, which is what respawn-looped neo on 2026-08-19.
    grep -q 'gdm_deepest_existing "$cache" "$volume"' "$dsul"
    ! grep -qF '! -w "$volume"' "$dsul"
    # …and the cache root under /Volumes/ still demands a real mountpoint.
    grep -qF 'require_mountpoint=1' "$dsul"

    # The stat dialect is detected in the wrapper, on both platforms.
    grep -qF 'stat --version >/dev/null 2>&1' "$dsul"
    grep -qF 'stat -c %d' "$dsul"
    grep -qF 'stat -f %d' "$dsul"
    grep -qF 'stat --version >/dev/null 2>&1' "$lsul"

    # Every phase logs one timestamped line before rclone replaces the process,
    # so a wrapper stuck before `exec` is diagnosable from the log alone.
    grep -qF 'date -u +%Y-%m-%dT%H:%M:%SZ' "$dsul"
    grep -q 'gdm_log "start:' "$dsul"
    grep -q 'gdm_log "guard:' "$dsul"
    grep -q 'gdm_log "sweep:' "$dsul"
    grep -q 'gdm_log "render:' "$dsul"
    grep -q 'gdm_log exec: rclone' "$dsul"
    grep -q 'gdm_log "start:' "$lsul"
    grep -q 'gdm_log exec: rclone' "$lsul"

    # Render happens only when the config is missing; rclone owns it afterwards.
    grep -q 'gdrive-mounts-render-config' "$dsul"
    grep -q -- '--org sulliwood' "$dsul"

    # Activation renders every wired org, best-effort, and links out of store.
    grep -q -- '--best-effort' "$render"
    grep -q -- '--out-dir' "$render"
    grep -q 'ln -sfn' "$links"

    # Activation records the settings it resolved, 0600, where doctor reads it.
    grep -q 'effective-settings.json' "$settings"
    grep -q -- '-m 600' "$settings"

    # That record is non-secret by construction: paths and knobs, no secrets.
    doc="$(grep -o '/nix/store/[^ ]*-gdrive-mounts-effective-settings.json' "$settings" | head -1)"
    grep -q '"cacheRoot"' "$doc"
    grep -q '"backend":"nfsmount"' "$doc"
    ! grep -q '/run/secrets' "$doc"

    # ── layer 3: run the guard against real directory trees ───────────────────
    harness=${guardHarness false}
    harnessmp=${guardHarness true}
    probe=${probeScript}
    t="$PWD/guard-t"
    mkdir -p "$t"

    if [ "$(id -u)" = 0 ]; then
      echo "note: builder is uid 0, mode bits do not deny — skipping the negative-writability scenario"
    fi

    # The probes must work on this builder. A wrong stat flag prints nothing,
    # which is exactly how the mountpoint test degraded silently.
    dev="$("$probe" dev "$t")"
    case "$dev" in
      "" | *[!0-9]*)
        echo "gdm_dev_of returned a non-numeric device: '$dev'" >&2
        exit 1
        ;;
    esac
    [ -z "$("$probe" dev "$t/absent")" ]
    [ "$("$probe" mountpoint "$t")" = no ]
    [ "$("$probe" mountpoint "$t/absent")" = no ]   # fails closed
    if [ -d /proc/self ]; then [ "$("$probe" mountpoint /proc)" = yes ]; fi

    mkdir -p "$t/deep/a/b"
    [ "$("$probe" deepest "$t/deep/a/b/c/d" "$t/deep")" = "$t/deep/a/b" ]
    [ "$("$probe" deepest "$t/deep" "$t/deep")" = "$t/deep" ]

    # 1. The neo regression. Volume root unwritable, cache subtree writable:
    #    the guard must pass and create the cache dir.
    mkdir -p "$t/vol1/tinyland/gdrive-cache"
    chmod 0555 "$t/vol1"
    "$harness" "$t/vol1/tinyland/gdrive-cache/sulliwood" "$t/vol1" "$t/err1" > "$t/out1"
    grep -q GUARD-READY "$t/out1"
    grep -q 'guard: ready' "$t/out1"
    [ -d "$t/vol1/tinyland/gdrive-cache/sulliwood" ]
    [ ! -e "$t/err1" ]

    # 2. A genuinely unwritable cache ancestor still fails loud, naming it.
    if [ "$(id -u)" != 0 ]; then
      mkdir -p "$t/vol2/tinyland"
      chmod 0555 "$t/vol2/tinyland"
      rc=0
      "$harness" "$t/vol2/tinyland/gdrive-cache/sulliwood" "$t/vol2" "$t/err2" \
        > "$t/out2" 2> "$t/e2" || rc=$?
      [ "$rc" = 78 ]
      grep -q 'is not writable by' "$t/e2"
      grep -qF "$t/vol2/tinyland" "$t/e2"
      grep -qF "gdrive-cache/sulliwood" "$t/e2"
      [ -s "$t/err2" ]
    fi

    # 3. An absent volume names the volume, not a generic "cache absent".
    rc=0
    "$harness" "$t/vol3/cache" "$t/vol3" "$t/err3" > "$t/out3" 2> "$t/e3" || rc=$?
    [ "$rc" = 78 ]
    grep -qF "cache volume $t/vol3 does not exist" "$t/e3"

    # 4. Create-if-missing: the whole cache path may be absent.
    mkdir -p "$t/vol4"
    "$harness" "$t/vol4/a/b/c" "$t/vol4" "$t/err4" > "$t/out4"
    grep -q GUARD-READY "$t/out4"
    [ -d "$t/vol4/a/b/c" ]

    # 5. Never spill onto the boot disk: a plain directory is not a volume.
    mkdir -p "$t/vol5/tinyland"
    rc=0
    "$harnessmp" "$t/vol5/tinyland/cache" "$t/vol5" "$t/err5" > "$t/out5" 2> "$t/e5" || rc=$?
    [ "$rc" = 78 ]
    grep -q 'is not a mountpoint' "$t/e5"
    [ ! -d "$t/vol5/tinyland/cache" ]

    chmod -R u+w "$t" 2>/dev/null || true
    touch $out
  ''
