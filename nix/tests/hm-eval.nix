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

  noWatchdogCfg =
    (lib.evalModules {
      specialArgs = { inherit pkgs lib; };
      modules = [
        stubBase
        module
        {
          programs.gdrive-mounts = {
            enable = true;
            platform = "darwin";
            secrets = wiredSecrets;
            package = stubRclone;
            toolsPackage = stubTools;
            orgsFile = orgsFile;
            watchdog.enable = false;
            remoteControl.enable = false;
          };
        }
      ];
    }).config;

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

  # The watchdog text as the module builds it, for shape assertions.
  watchdogTextFor =
    nfsStatus:
    plan.watchdogShell {
      intervalSec = 60;
      probeTimeoutSec = 10;
      failureThreshold = 2;
      restartFloorSec = 300;
      mountGraceSec = 300;
      inherit nfsStatus;
      mountedCheck = "true";
      unitActiveCheck = "true";
      unitPidCommand = "true";
      restartCommand = "true";
    };

  # The watchdog's own text, run for real. The four platform verbs are the seam:
  # launchd and systemd in the shipped units, files in the harness. Everything
  # above them — the threshold, the stand-downs, the capture ordering, the
  # restart floor, the wedge record — is the shipped code, executed.
  #
  # usage: watchdog-harness <point> <state> <cycles>
  watchdogHarness =
    {
      failureThreshold ? 2,
      restartFloorSec ? 300,
      probeTimeoutSec ? 2,
      mountGraceSec ? 300,
    }:
    pkgs.writeShellScript "gdrive-mounts-watchdog-harness" ''
      set -euo pipefail
      unit=harness
      point="$1"
      state="$2"
      cycles_wanted="$3"
      record="$state/wedge.jsonl"
      capture="$state/capture.log"
      floor_file="$state/floor"
      probe_dir="$state/probe"
      sock=""
      rclone_bin=/nonexistent/rclone
      mkdir -p "$state" "$probe_dir"
      ${plan.shellPreamble}
      ${plan.watchdogShell {
        intervalSec = 1;
        inherit
          probeTimeoutSec
          failureThreshold
          restartFloorSec
          mountGraceSec
          ;
        nfsStatus = false;
        mountedCheck = ''[ -f "$state/mounted" ]'';
        unitActiveCheck = ''[ -f "$state/active" ]'';
        unitPidCommand = ''cat "$state/pid" 2>/dev/null || true'';
        restartCommand = ''printf 'restart\n' >> "$state/restarts"'';
      }}
      i=0
      while [ "$i" -lt "$cycles_wanted" ]; do
        gdm_watch_once
        i=$((i + 1))
        # Let a scenario detach the mount mid-run: `seen_mounted` is in-memory,
        # so proving it takes a sighting and a disappearance in one invocation.
        if [ -f "$state/detach-after" ] && [ "$i" = "$(cat "$state/detach-after")" ]; then
          rm -f "$state/mounted"
        fi
      done
    '';

  # Direct assertions on the elapsed-time parser. macOS ps offers no `etimes`,
  # so a wedge record's uptime depends entirely on parsing this format right —
  # including the leading zeros that are not valid octal.
  etimeProbe = pkgs.writeShellScript "gdrive-mounts-etime-probe" ''
    set -euo pipefail
    unit=probe
    point=/nonexistent
    record=/dev/null
    capture=/dev/null
    floor_file=/dev/null
    probe_dir=/tmp
    sock=""
    rclone_bin=/nonexistent/rclone
    ${plan.shellPreamble}
    ${plan.watchdogShell {
      intervalSec = 60;
      probeTimeoutSec = 1;
      failureThreshold = 2;
      restartFloorSec = 300;
      mountGraceSec = 300;
      nfsStatus = false;
      mountedCheck = "false";
      unitActiveCheck = "false";
      unitPidCommand = "true";
      restartCommand = "true";
    }}
    gdm_etime_to_secs "$1"
    printf '\n'
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
      name = "darwin-emits-one-agent-and-one-watchdog-per-mount-plus-index";
      ok =
        agentNames darwinCfg == [
          "gdrive-mounts-index"
          "gdrive-mounts-rwlab-root"
          "gdrive-mounts-rwlab-root-watchdog"
          "gdrive-mounts-sulliwood-root"
          "gdrive-mounts-sulliwood-root-watchdog"
        ];
    }
    {
      name = "watchdog-can-be-turned-off-without-touching-the-mounts";
      ok =
        agentNames noWatchdogCfg == [
          "gdrive-mounts-index"
          "gdrive-mounts-rwlab-root"
          "gdrive-mounts-sulliwood-root"
        ];
    }
    {
      # A supervision loop that exits cleanly has still stopped supervising, so
      # KeepAlive must be unconditional — not the mounts' SuccessfulExit=false.
      name = "watchdog-agent-is-kept-alive-unconditionally";
      ok =
        darwinCfg.launchd.agents."gdrive-mounts-sulliwood-root-watchdog".config.KeepAlive == true
        &&
          darwinCfg.launchd.agents."gdrive-mounts-sulliwood-root".config.KeepAlive == {
            SuccessfulExit = false;
          };
    }
    {
      name = "watchdog-service-restarts-always-and-is-not-bound-to-the-mount";
      ok =
        let
          s = linuxCfg.systemd.user.services."gdrive-mounts-sulliwood-root-watchdog";
        in
        s.Service.Restart == "always"
        && s.Unit.After == [ "gdrive-mounts-sulliwood-root.service" ]
        && !(s.Unit ? PartOf)
        && !(s.Unit ? BindsTo);
    }
    {
      name = "darwin-emits-no-systemd-units";
      ok = serviceNames darwinCfg == [ ] && builtins.attrNames darwinCfg.systemd.user.timers == [ ];
    }
    {
      name = "linux-emits-one-service-and-one-watchdog-per-mount-plus-index";
      ok =
        serviceNames linuxCfg == [
          "gdrive-mounts-index"
          "gdrive-mounts-rwlab-root"
          "gdrive-mounts-rwlab-root-watchdog"
          "gdrive-mounts-sulliwood-root"
          "gdrive-mounts-sulliwood-root-watchdog"
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
      ok =
        elem "--volname" (argsOf "darwin" "sulliwood") && !(elem "--volname" (argsOf "linux" "sulliwood"));
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
        hasInfix "/bin"
          darwinCfg.launchd.agents."gdrive-mounts-sulliwood-root".config.EnvironmentVariables.PATH
        && hasInfix "PATH=" (
          head linuxCfg.systemd.user.services."gdrive-mounts-sulliwood-root".Service.Environment
        );
    }
    {
      # One mount agent + one watchdog agent per mount, plus the single index.
      name = "production-orgs-json-unit-count-is-derived";
      ok = builtins.length (agentNames prodCfg) == (2 * prodMountCount) + 1;
    }
    # ── the 2026-08-19 wedge: latency budget, instrumentation, supervision ─────
    {
      # rclone's stock patience (5m IO idle x 10 retries) outlasts the macOS NFS
      # client's by three orders of magnitude. These flags are the fix, so their
      # absence is a regression, not a preference.
      name = "latency-budget-flags-ship-on-both-platforms";
      ok =
        let
          need = [
            "--timeout"
            "--contimeout"
            "--low-level-retries"
            "--attr-timeout"
            "--poll-interval"
          ];
          has = args: lib.all (f: elem f args) need;
        in
        has (argsOf "darwin" "sulliwood") && has (argsOf "linux" "sulliwood");
    }
    {
      # rclone logs low-level retries and pacer backoff at INFO. At the NOTICE
      # default the wedge presented as "no error ever logged".
      name = "log-level-is-above-the-default-notice";
      ok = elem "INFO" (argsOf "darwin" "sulliwood");
    }
    {
      # launchd appends to a log forever and never rotates it.
      name = "periodic-stats-block-is-disabled";
      ok =
        let
          a = argsOf "darwin" "sulliwood";
        in
        elem "--stats" a && elem "0" a;
    }
    {
      # A unix socket inside the 0700 state dir, never a TCP port: --rc-no-auth
      # on loopback is reachable by every local process and rc can drive the VFS.
      name = "remote-control-is-a-unix-socket-in-the-state-dir";
      ok =
        let
          a = argsOf "darwin" "sulliwood";
          sock = plan.rcSocket testSettings (orgNamed "sulliwood") (head (orgNamed "sulliwood").mounts);
        in
        elem "--rc" a
        && elem "unix://${sock}" a
        && lib.hasPrefix "${homeDir}/.local/state/gdrive-mounts/" sock
        && !(lib.any (lib.hasPrefix "127.0.0.1") a)
        && !(lib.any (lib.hasPrefix "localhost") a);
    }
    {
      name = "remote-control-can-be-turned-off";
      ok =
        let
          org = orgNamed "sulliwood";
        in
        !(elem "--rc" (
          plan.mountArgs {
            platform = "darwin";
            settings = testSettings // {
              remoteControl = false;
            };
            inherit org;
            mount = head org.mounts;
          }
        ));
    }
    {
      # Forensics refuted the handle-limit and the read-ahead as causes. A
      # mitigation pass that "tidies" them anyway is changing untested things.
      name = "refuted-knobs-are-left-alone";
      ok =
        let
          a = argsOf "darwin" "sulliwood";
        in
        elem "250000" a && elem "128M" a && elem "100G" a;
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
    {
      # `timeout` cannot bound a call into a hard,nointr mount: the caller blocks
      # in the kernel where no signal lands. The probe must abandon, never wait.
      name = "the-probe-abandons-a-hung-call-instead-of-waiting-on-it";
      ok =
        let
          w = watchdogTextFor true;
        in
        # A completion sentinel, not `wait`: `wait` on a child blocked in an
        # uninterruptible NFS call never returns, and a zombie still answers
        # `kill -0`, so neither can bound the probe.
        hasInfix ''while [ ! -s "$sentinel" ]'' w
        && hasInfix "printf 'timeout'" w
        && !(hasInfix ''wait "$probe_pid"'' w)
        && !(hasInfix "kill -0" w);
    }
    {
      # nfsstat reads the kernel's mount table, so it still answers while the
      # mount does not. On Linux there is no such signal and the probe is the
      # bounded stat alone.
      name = "the-kernel-nfs-signal-is-darwin-nfsmount-only";
      ok = hasInfix "nfsstat -m" (watchdogTextFor true) && !(hasInfix "nfsstat" (watchdogTextFor false));
    }
    {
      # neo, 2026-08-19: rclone up 46 minutes, still LISTENing on the NFS port,
      # nothing mounted, no log line since the mount detached. The dump showed
      # the go-nfs server goroutines gone while mountlib's Wait was still
      # blocked — so rclone never exits and no supervisor ever notices. An
      # unmounted point past the grace window has to count as a wedge.
      name = "an-unmounted-point-past-the-grace-window-is-a-wedge";
      ok =
        let
          w = watchdogTextFor true;
        in
        hasInfix ''gdm_probe_stat_result="unmounted"'' w
        && hasInfix ''[ "''${up:-0}" -lt "$mount_grace" ]'' w;
    }
    {
      name = "the-restart-floor-is-read-from-disk-not-from-a-variable";
      ok =
        let
          w = watchdogTextFor true;
        in
        hasInfix ''[ -f "$floor_file" ] || return 1'' w && hasInfix ''gdm_epoch > "$floor_file"'' w;
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
    dwatch=${darwinScript "gdrive-mounts-sulliwood-root-watchdog"}
    lwatch=${linuxScript "gdrive-mounts-sulliwood-root-watchdog"}
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

    # C6b — the sweep is asserted, not attempted. A swallowed `umount -f`
    # failure means the next line mounts on top of the corpse.
    grep -q 'exit 75' "$dsul"
    grep -q 'survived the force-unmount' "$dsul"
    grep -q 'survived the force-unmount' "$lsul"

    # The latency budget and the instrumentation ship in the argv, both platforms.
    for f in "$dsul" "$lsul"; do
      grep -q -- '--timeout' "$f"
      grep -q -- '--contimeout' "$f"
      grep -q -- '--low-level-retries' "$f"
      grep -q -- '--attr-timeout' "$f"
      grep -q -- '--poll-interval' "$f"
      grep -q -- '--log-level INFO' "$f"
      grep -q -- '--stats 0' "$f"
    done

    # rc is a unix socket in the state dir, and the wrapper clears a stale one
    # before rclone tries to bind over it.
    grep -q -- '--rc-addr unix:///' "$dsul"
    grep -q -- '--rc-no-auth' "$dsul"
    ! grep -q -- '--rc-addr 127.0.0.1' "$dsul"
    ! grep -q -- '--rc-addr localhost' "$dsul"
    grep -qF 'rm -f "$sock"' "$dsul"

    # ── the watchdog wrappers ─────────────────────────────────────────────────
    # Its probes must never stat a path that may be wedged: mount(8) and
    # /proc/self/mounts read the kernel table instead.
    grep -qF 'mount | grep -qF " on ' "$dwatch"
    grep -qF '/proc/self/mounts' "$lwatch"
    ! grep -q 'mountpoint -q' "$lwatch"

    # It supervises the real unit, by name, with the real supervisor.
    grep -q 'launchctl kickstart -k' "$dwatch"
    grep -qF 'dev.tinyland.gdrive-mounts.sulliwood-root' "$dwatch"
    grep -q 'systemctl --user restart' "$lwatch"
    grep -qF 'gdrive-mounts-sulliwood-root.service' "$lwatch"

    # It stands down instead of resurrecting a unit the operator stopped.
    grep -q 'launchctl list' "$dwatch"
    grep -q 'systemctl --user is-active' "$lwatch"
    grep -q 'standing down' "$dwatch"

    # It captures before it acts, and SIGQUIT is on the capture path only.
    grep -q -- 'kill -QUIT' "$dwatch"
    grep -q 'core/stats' "$dwatch"
    grep -q 'vfs/stats' "$dwatch"
    grep -qF 'wedge.sulliwood-root.jsonl' "$dwatch"

    # The kernel NFS signal is Darwin+nfsmount only.
    grep -q 'nfsstat -m' "$dwatch"
    ! grep -q 'nfsstat' "$lwatch"

    # The grace window before an unmounted point counts as a wedge is derived
    # from the cache guard's own budget (120s default + 180s), never a knob of
    # its own — it can therefore never be shorter than a legitimate wait for the
    # cache volume, which would make the watchdog fight the guard.
    grep -qF 'mount_grace=300' "$dwatch"
    grep -qF 'mount_grace=300' "$lwatch"

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

    # ── layer 3b: run the watchdog against synthetic conditions ───────────────
    #
    # Everything above the four platform verbs is the shipped code, executed:
    # the stand-downs, the consecutive-failure threshold, the capture, the
    # restart floor, and the wedge record.
    wd=${watchdogHarness { }}
    wdfloor=${
      watchdogHarness {
        restartFloorSec = 0;
      }
    }
    wdnograce=${
      watchdogHarness {
        restartFloorSec = 0;
        mountGraceSec = 0;
      }
    }
    etime=${etimeProbe}
    w="$PWD/wd"
    mkdir -p "$w"

    # The elapsed-time parser, including the leading zeros that are not octal.
    [ "$("$etime" 01:30)" = 90 ]
    [ "$("$etime" 02:03:04)" = 7384 ]
    [ "$("$etime" 1-02:03:04)" = 93784 ]
    [ "$("$etime" 08:08)" = 488 ]
    [ "$("$etime" 09:09:09)" = 32949 ]

    # 1. Healthy: a mounted, active unit whose mountpoint answers. Three cycles,
    #    no restart, no wedge record, nothing to read.
    s1="$w/s1"
    mkdir -p "$s1/point"
    touch "$s1/mounted" "$s1/active"
    "$wd" "$s1/point" "$s1" 3 > "$s1/out"
    [ ! -e "$s1/restarts" ]
    [ ! -e "$s1/wedge.jsonl" ]
    [ ! -e "$s1/floor" ]

    # 2. Wedge: the mount table says mounted, the unit is active, the
    #    mountpoint does not answer. ONE failure must not act — the "not
    #    responding" flag is observed to set and clear inside a single minute —
    #    and the second must.
    s2="$w/s2"
    mkdir -p "$s2"
    touch "$s2/mounted" "$s2/active"
    "$wd" "$s2/absent" "$s2" 1 > "$s2/out1"
    grep -q 'probe: unhealthy (1/2)' "$s2/out1"
    [ ! -e "$s2/restarts" ]
    [ ! -e "$s2/wedge.jsonl" ]

    "$wd" "$s2/absent" "$s2" 2 > "$s2/out2"
    grep -q 'wedge: confirmed after 2 consecutive failures' "$s2/out2"
    [ "$(wc -l < "$s2/restarts" | tr -d ' ')" = 1 ]
    grep -q '"action":"restarted"' "$s2/wedge.jsonl"
    grep -q '"restart_count":1' "$s2/wedge.jsonl"
    grep -q '"probe":"error"' "$s2/wedge.jsonl"
    [ -s "$s2/floor" ]
    # …and it captured before it acted, even with no rc socket to read.
    grep -q 'wedge capture' "$s2/capture.log"
    grep -q 'rc unavailable' "$s2/capture.log"
    # The record is 0600: it names paths and pids.
    [ "$(${pkgs.coreutils}/bin/stat -c %a "$s2/wedge.jsonl")" = 600 ]

    # 3. The restart floor holds a second wedge, and still records it — a floor
    #    suppresses the restart, never the evidence.
    "$wd" "$s2/absent" "$s2" 2 > "$s2/out3"
    grep -q 'wedge: holding' "$s2/out3"
    [ "$(wc -l < "$s2/restarts" | tr -d ' ')" = 1 ]
    grep -q '"action":"held-by-floor"' "$s2/wedge.jsonl"
    # The held line does not inflate the restart count.
    [ "$(grep -c '"action":"restarted"' "$s2/wedge.jsonl")" = 1 ]

    # 4. With the floor at zero the same conditions restart again, so scenario 3
    #    proves the floor and not merely a second no-op.
    s4="$w/s4"
    mkdir -p "$s4"
    touch "$s4/mounted" "$s4/active"
    "$wdfloor" "$s4/absent" "$s4" 2 > "$s4/o1"
    "$wdfloor" "$s4/absent" "$s4" 2 > "$s4/o2"
    [ "$(wc -l < "$s4/restarts" | tr -d ' ')" = 2 ]
    [ "$(grep -c '"action":"restarted"' "$s4/wedge.jsonl")" = 2 ]
    grep -q '"restart_count":2' "$s4/wedge.jsonl"

    # 5. An operator who stopped the mount must not have it resurrected.
    s5="$w/s5"
    mkdir -p "$s5"
    touch "$s5/mounted"     # mounted, but the unit is not loaded
    "$wdfloor" "$s5/absent" "$s5" 4 > "$s5/out"
    grep -q 'the mount unit is not loaded — standing down' "$s5/out"
    [ ! -e "$s5/restarts" ]

    # 6. Nothing mounted yet is not a wedge: the wrapper may still be inside the
    #    cache guard, which waits up to two minutes by design.
    s6="$w/s6"
    mkdir -p "$s6"
    touch "$s6/active"
    "$wdfloor" "$s6/absent" "$s6" 4 > "$s6/out"
    grep -q 'standing down inside the' "$s6/out"
    [ ! -e "$s6/restarts" ]

    # 6b. …but past the grace window it is the quietest wedge there is. neo,
    #     2026-08-19: rclone alive 46 minutes, still LISTENing on the NFS port,
    #     nothing mounted, not one log line since it detached. Nothing failed,
    #     so nothing restarted it. With the grace window at zero the same
    #     conditions must be acted on, which is what separates this from a
    #     mount that has simply not come up yet.
    s6b="$w/s6b"
    mkdir -p "$s6b"
    touch "$s6b/active"
    "$wdnograce" "$s6b/absent" "$s6b" 2 > "$s6b/out"
    grep -q 'wedge: confirmed' "$s6b/out"
    [ "$(wc -l < "$s6b/restarts" | tr -d ' ')" = 1 ]
    grep -q '"probe":"unmounted"' "$s6b/wedge.jsonl"

    # 6c. …and once the point has been SEEN mounted, its disappearance needs no
    #     grace at all. The replacement instance on neo detached about two
    #     minutes after start; a grace-gated watchdog would have sat on that for
    #     five. Same default grace as scenario 6, which stood down — the only
    #     difference here is that the first cycle saw it mounted.
    s6c="$w/s6c"
    mkdir -p "$s6c/point"
    touch "$s6c/active" "$s6c/mounted"
    printf 1 > "$s6c/detach-after"     # healthy cycle 1, then the mount vanishes
    "$wdfloor" "$s6c/point" "$s6c" 3 > "$s6c/out"
    grep -q 'was mounted and is not any more' "$s6c/out"
    ! grep -q 'standing down inside the' "$s6c/out"
    [ "$(wc -l < "$s6c/restarts" | tr -d ' ')" = 1 ]
    grep -q '"probe":"unmounted"' "$s6c/wedge.jsonl"

    # 7. A hung filesystem call must not hang the watchdog. `timeout` cannot
    #    help here — a hard,nointr caller blocks in the kernel where no signal
    #    is delivered — so the probe abandons it. Prove that by making `stat`
    #    itself never return, and requiring the cycle to finish anyway.
    s7="$w/s7"
    mkdir -p "$s7/bin"
    touch "$s7/mounted" "$s7/active"
    # Built with printf, not a heredoc: nix strips the common indentation of an
    # indented string but not the rest, and a shebang cannot carry leading space.
    # The preamble probes the stat dialect at source time, so only the real call
    # is allowed to hang.
    {
      printf '#!/bin/sh\n'
      printf 'case "''${1:-}" in --version) exit 0 ;; esac\n'
      printf 'sleep 300\n'
    } > "$s7/bin/stat"
    chmod 0755 "$s7/bin/stat"
    started="$(date +%s)"
    PATH="$s7/bin:$PATH" "$wdfloor" "$s7/point" "$s7" 1 > "$s7/out"
    elapsed=$(( $(date +%s) - started ))
    grep -q 'stat=timeout' "$s7/out"
    # probeTimeoutSec is 2 in the harness; anything near 300 means it waited.
    [ "$elapsed" -lt 60 ]

    chmod -R u+w "$t" "$w" 2>/dev/null || true
    touch $out
  ''
