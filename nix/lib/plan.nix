# plan.nix — pure planning library for programs.gdrive-mounts.
#
# One code path. The home-manager module and the eval fixture both derive mount
# argv, paths and unit names from this file, so a test that passes proves the
# argv the module ships.
#
# Takes `lib` only. It reads no pkgs, no stdenv, no filesystem.
{ lib }:
let
  inherit (lib)
    filter
    concatMap
    optionals
    elem
    head
    hasPrefix
    removePrefix
    ;

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
      ioTimeout = defaults.ioTimeout;
      connectTimeout = defaults.connectTimeout;
      lowLevelRetries = defaults.lowLevelRetries;
      attrTimeout = defaults.attrTimeout;
      pollInterval = defaults.pollInterval;
      logLevel = defaults.logLevel;
      statsInterval = defaults.statsInterval;
      nfsMountOptions = defaults.nfsMountOptions;
      remoteControl = defaults.remoteControl;
      watchdogEnable = defaults.watchdogEnable;
      watchdogIntervalSec = defaults.watchdogIntervalSec;
      watchdogProbeTimeoutSec = defaults.watchdogProbeTimeoutSec;
      watchdogFailureThreshold = defaults.watchdogFailureThreshold;
      watchdogRestartFloorSec = defaults.watchdogRestartFloorSec;
    };

  # rclone subcommand for the platform. darwin defaults to nfsmount (kext-free).
  backendFor =
    { platform, settings }:
    if platform == "darwin" then settings.backendDarwin else settings.backendLinux;

  # An org is read-only unless its OAuth scope is the full `drive`.
  readOnly = org: (org.scope or "drive.readonly") != "drive";

  # One rendered config per org. rclone writes refreshed tokens back into the
  # file it was given, so a single writer per file is a correctness rule.
  confPath = settings: org: "${settings.stateDir}/rclone-${org.name}.conf";

  cacheDir = settings: org: "${settings.cacheRoot}/${org.name}";

  # rclone's remote-control endpoint, one per mount. A unix socket, never a TCP
  # port: `--rc-no-auth` over loopback TCP is reachable by every local process,
  # and rc can drive the VFS. The socket itself is created world-connectable
  # (`srwxr-xr-x`), so the 0700 stateDir is the access control — which is why
  # this path must stay inside stateDir.
  rcSocket =
    settings: org: mount:
    "${settings.stateDir}/rc-${org.name}-${mount.name}.sock";

  # Per-mount watchdog paths. The record is append-only: it is how recurrence
  # becomes measurable, so nothing here is ever rewritten in place.
  watchdogPaths = settings: org: mount: rec {
    slug = "${org.name}-${mount.name}";
    record = "${settings.stateDir}/wedge.${slug}.jsonl";
    capture = "${settings.stateDir}/wedge-capture.${slug}.log";
    floorFile = "${settings.stateDir}/watchdog-floor.${slug}";
    probeDir = "${settings.stateDir}/watchdog";
  };

  # The volume the cache lives on: for /Volumes/<name>/... and /mnt/<name>/...
  # that is the mountpoint two components deep (the cache root may sit in a
  # user-owned subtree of a root-owned volume, e.g. /Volumes/TinylandSSD/tinyland);
  # elsewhere it is the cache root's parent.
  cacheVolume =
    cacheRoot:
    let
      parts = filter (x: x != "") (lib.splitString "/" cacheRoot);
    in
    if
      (hasPrefix "/Volumes/" cacheRoot || hasPrefix "/mnt/" cacheRoot) && builtins.length parts >= 2
    then
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

  linkPath =
    settings: org: link:
    "${settings.mountRoot}/${org.name}-${link.name}";

  linkTarget =
    settings: org: link:
    "${mountPoint settings (rootMount org)}/${link.target}";

  # argv for `rclone`, without the binary. Flags are backend- and scope-gated:
  #   --nfs-cache-handle-limit exists only on nfsmount/serve nfs
  #   --volname is documented macOS/Windows only
  #   --read-only enforces the scope the token was minted with
  #
  # The latency block (--timeout / --contimeout / --low-level-retries /
  # --attr-timeout / --poll-interval) is not tuning for its own sake. rclone's
  # stock patience is 5m IO idle × 10 low-level retries — up to 50 minutes on
  # one operation — while the macOS NFS client marks a mount "not responding"
  # after `vfs.generic.nfs.client.initialdowndelay` seconds, which is 5. Any
  # backend call slower than that kills the mount that rclone still considers
  # perfectly healthy. These defaults pull rclone's stall window back under the
  # client's patience. See docs/evidence/2026-08-19-nfs-wedge-forensics.md.
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
      "--timeout"
      settings.ioTimeout
      "--contimeout"
      settings.connectTimeout
      "--low-level-retries"
      (toString settings.lowLevelRetries)
      "--attr-timeout"
      settings.attrTimeout
      "--poll-interval"
      settings.pollInterval
      "--log-level"
      settings.logLevel
      "--stats"
      settings.statsInterval
    ]
    ++ optionals settings.remoteControl [
      "--rc"
      "--rc-addr"
      "unix://${rcSocket settings org mount}"
      "--rc-no-auth"
    ]
    ++ optionals (backend == "nfsmount") [
      "--nfs-cache-handle-limit"
      (toString settings.nfsCacheHandleLimit)
    ]
    # NFS *client* mount options, nfsmount only. rclone does not interpret
    # these: on Darwin `rclone nfsmount` shells out to mount(8) as
    #   mount -o port=N -o mountport=N -o tcp <each --option> localhost:/ <point>
    # and mount(8), given a host:/path special and no -t, execs /sbin/mount_nfs.
    # So an `--option soft` really does reach the kernel NFS client. rclone
    # hardcodes only port/mountport/tcp and passes ours after its own.
    #
    # This is the H3 mitigation, and it addresses the bistability the latency
    # budget above can only make less likely: `hard,nointr` is a macOS *default*,
    # not something rclone chose, and it is what turns a transient stall into a
    # mount that cannot heal, cannot be interrupted, and cannot even be probed.
    #
    # On `rclone mount` (FUSE, Linux) the same flag means libfuse options, which
    # these are not — hence the backend gate, not a platform gate.
    ++ concatMap (o: [
      "--option"
      o
    ]) (if backend == "nfsmount" then settings.nfsMountOptions else [ ])
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

  # ── watchdog shell ──────────────────────────────────────────────────────────
  #
  # A mount can stop serving while every process involved stays alive: on
  # 2026-08-19 rclone held its NFS listener, its Drive connection and 31 healthy
  # goroutines for five hours while the macOS client had already marked the
  # mount "not responding" and every read blocked forever. `hard,nointr` makes
  # that state bistable — it cannot heal itself, and the callers cannot even be
  # signalled out of it. Nothing failed, so nothing restarted.
  #
  # This is the missing supervisor. It probes from outside, confirms across
  # consecutive cycles, captures before it acts, and restarts the mount unit.
  #
  # Text, not a derivation, for the same reason as cacheGuard: the eval fixture
  # sources this exact text into a harness and *runs* it, so the logic under
  # test is the logic that ships. The four platform verbs are passed in as
  # shell snippets — launchd on Darwin, systemd on Linux, fakes in the harness.
  #
  # Reads `$unit`, `$point`, `$record`, `$capture`, `$floor_file`, `$probe_dir`,
  # `$sock` (may be empty), `$rclone_bin`; needs shellPreamble sourced first.
  # The caller calls gdm_watch_loop.
  watchdogShell =
    {
      intervalSec,
      probeTimeoutSec,
      failureThreshold,
      restartFloorSec,
      mountGraceSec, # how long an unmounted point is "still coming up"
      nfsStatus, # emit the kernel-side NFS signal (darwin + nfsmount)
      mountedCheck, # rc 0 when something is mounted at $point. MUST NOT stat it.
      unitActiveCheck, # rc 0 when the mount unit is loaded/active
      unitPidCommand, # prints the mount unit's pid, or nothing
      restartCommand, # restarts the mount unit
    }:
    ''
      interval=${toString intervalSec}
      probe_timeout=${toString probeTimeoutSec}
      threshold=${toString failureThreshold}
      restart_floor=${toString restartFloorSec}
      mount_grace=${toString mountGraceSec}
      consecutive=0
      cycles=0
      seen_mounted=0
      gdm_probe_flags=""
      gdm_probe_stat_result=""

      # Hourly, whatever the interval. The watchdog's heartbeat is why the mount
      # itself can run with `--stats 0`: one liveness line an hour beats a stats
      # block every minute in a log launchd appends to forever.
      heartbeat_every=$((3600 / interval))
      [ "$heartbeat_every" -ge 1 ] || heartbeat_every=1

      gdm_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
      gdm_epoch() { date +%s; }

      # "[[DD-]HH:]MM:SS" (the only elapsed-time format macOS ps offers — it has
      # no `etimes` keyword) -> seconds. `10#` is load-bearing: `08` is not a
      # valid octal literal, and the arithmetic would abort the script.
      gdm_etime_to_secs() {
        local e d h m s
        e="$1"
        d=0
        h=0
        case "$e" in
          *-*)
            d="''${e%%-*}"
            e="''${e#*-}"
            ;;
        esac
        case "$e" in
          *:*:*)
            h="''${e%%:*}"
            e="''${e#*:}"
            ;;
        esac
        m="''${e%%:*}"
        s="''${e##*:}"
        printf '%s' "$((10#''${d:-0} * 86400 + 10#''${h:-0} * 3600 + 10#''${m:-0} * 60 + 10#''${s:-0}))"
      }

      # Uptime of the wedged process, so a wedge record answers "how long did it
      # survive this time" without correlating logs.
      gdm_pid_uptime() {
        local e
        [ -n "''${1:-}" ] || return 1
        e="$(ps -o etime= -p "$1" 2>/dev/null | tr -d '[:space:]')"
        [ -n "$e" ] || return 1
        gdm_etime_to_secs "$e"
      }

      # The kernel's own view of the NFS client. It reads mount state, so it
      # still answers while the mount does not — that is the whole point of
      # preferring it. `0x0` is healthy; `0x2,not responding` is the wedge.
      gdm_nfs_status() {
        ${
          if nfsStatus then
            ''
              nfsstat -m 2>/dev/null | awk -v p="$point" '
                        $1 == p { seen = 1 }
                        seen && /Status flags:/ {
                          sub(/^[[:space:]]*Status flags:[[:space:]]*/, "")
                          print
                          exit
                        }
                      ' | tr -d '\n'
            ''
          else
            "true"
        }
      }

      # A bounded filesystem call from an expendable child. `timeout` cannot do
      # this job: a `hard,nointr` mount blocks its caller inside the kernel where
      # no signal is delivered, so the probe process may be unkillable. We start
      # it, watch for a completion sentinel, and ABANDON it if it never returns —
      # a wait would wedge the watchdog itself, which is the one thing it may not
      # do. The abandoned process is the price of asking the question; it goes
      # away with the mount we are about to replace.
      #
      # Prints exactly one of: ok | error | timeout.
      gdm_probe_stat() {
        local sentinel rc waited probe_pid
        waited=0
        mkdir -p "$probe_dir"
        rm -f "$probe_dir"/probe.* 2>/dev/null || true
        sentinel="$probe_dir/probe.$$.$(gdm_epoch)"
        # `|| rc=$?` rather than a bare call followed by `$?`: the wrapper runs
        # under `set -e`, and a bare failing `stat` would take the subshell down
        # before it could write the sentinel — turning every fast failure into a
        # full `probe_timeout` wait misreported as `timeout`. It happens to work
        # today only because every caller reaches this through `|| true`, which
        # suspends errexit for the whole call tree. That is far too subtle a
        # thing for the verdict to depend on, so the subshell no longer does.
        (
          rc=0
          stat -- "$point/." >/dev/null 2>&1 || rc=$?
          printf '%s' "$rc" > "$sentinel"
        ) &
        probe_pid=$!
        while [ ! -s "$sentinel" ]; do
          if [ "$waited" -ge "$probe_timeout" ]; then
            kill -9 "$probe_pid" 2>/dev/null || true
            printf 'timeout'
            return 1
          fi
          sleep 1
          waited=$((waited + 1))
        done
        rc="$(cat "$sentinel" 2>/dev/null || printf 1)"
        rm -f "$sentinel" 2>/dev/null || true
        if [ "$rc" = 0 ]; then
          printf 'ok'
          return 0
        fi
        printf 'error'
        return 1
      }

      # One verdict from two independent signals, OR-combined: either the kernel
      # says the server stopped answering, or a real filesystem call did not come
      # back. Neither alone is trusted to act — that is what the threshold is for.
      gdm_probe() {
        gdm_probe_flags="$(gdm_nfs_status || true)"
        gdm_probe_stat_result="$(gdm_probe_stat || true)"
        case "$gdm_probe_flags" in
          "" | 0x0) ;;
          *) return 1 ;;
        esac
        [ "$gdm_probe_stat_result" = ok ] || return 1
        return 0
      }

      # Everything worth having BEFORE the mount is replaced.
      #
      # The rc calls are non-destructive and travel over a unix socket that never
      # touches the wedged path, so they separate "rclone is dead" from "rclone
      # is alive and the NFS layer went quiet" — the 2026-08-19 signature, which
      # took a signal and a hand-read goroutine dump to establish the first time.
      #
      # SIGQUIT then makes the Go runtime print every goroutine to stderr (the
      # unit's .err.log) and exit. It is a capture *and* a kill, which is exactly
      # why it only ever runs on the path that is about to restart the mount.
      gdm_capture() { # pid
        local pid c
        pid="''${1:-}"
        {
          printf '=== %s %s wedge capture (pid %s) ===\n' "$(gdm_now)" "$unit" "''${pid:-none}"
          printf 'probe: flags=%s stat=%s consecutive=%s\n' \
            "''${gdm_probe_flags:-none}" "''${gdm_probe_stat_result:-none}" "$consecutive"
          ${if nfsStatus then "nfsstat -m 2>&1 || true" else "true"}
          if [ -n "''${sock:-}" ] && [ -S "$sock" ]; then
            for c in core/version core/pid core/stats core/memstats vfs/stats; do
              printf -- '--- rc %s ---\n' "$c"
              # Bounded, and here `timeout` is exactly the right tool — the
              # inverse of the stat probe above. That probe cannot be bounded by
              # a signal because a `hard,nointr` caller blocks in the kernel
              # where no signal is delivered; an rc call blocks on a unix-socket
              # read, which is interruptible, so SIGTERM lands. Unbounded, these
              # five calls would let the process under investigation stall its
              # own investigator: `rclone rc` inherits rclone's 5m IO timeout,
              # so a genuinely deadlocked rclone could hold the watchdog for
              # twenty-five minutes — the one thing a watchdog may not do.
              timeout -k 5 "$probe_timeout" "$rclone_bin" rc --unix-socket "$sock" "$c" 2>&1 || true
            done
          else
            printf -- '--- rc unavailable (socket: %s) ---\n' "''${sock:-disabled}"
          fi
        } >> "$capture" 2>&1
        chmod 600 "$capture" 2>/dev/null || true

        if [ -n "$pid" ]; then
          gdm_log "wedge: SIGQUIT $pid — goroutine dump goes to the unit err log"
          kill -QUIT "$pid" 2>/dev/null || true
          sleep 3
        else
          gdm_log "wedge: no pid to signal — skipping the goroutine dump"
        fi
      }

      # Append-only, one JSON object per line, 0600. `restart_count` is derived
      # from the file itself so the record needs no second counter to fall out of
      # step with, and a `held-by-floor` line records a wedge that was seen and
      # deliberately not acted on — recurrence stays measurable either way.
      gdm_record() { # action pid uptime
        local action pid uptime n
        action="$1"
        pid="$2"
        uptime="$3"
        n=0
        if [ -f "$record" ]; then
          n="$(grep -c '"action":"restarted"' "$record" 2>/dev/null || true)"
        fi
        case "''${n:-}" in
          "" | *[!0-9]*) n=0 ;;
        esac
        if [ "$action" = restarted ]; then
          n=$((n + 1))
        fi
        printf '{"ts":"%s","unit":"%s","point":"%s","action":"%s","restart_count":%s,"consecutive_failures":%s,"pid":"%s","uptime_sec":"%s","nfs_status_flags":"%s","probe":"%s"}\n' \
          "$(gdm_now)" "$unit" "$point" "$action" "$n" "$consecutive" \
          "''${pid:-none}" "''${uptime:-unknown}" "''${gdm_probe_flags:-none}" "''${gdm_probe_stat_result:-none}" \
          >> "$record"
        chmod 600 "$record" 2>/dev/null || true
      }

      # The floor lives on disk, not in a variable: a watchdog that crash-loops
      # must not be handed a fresh restart budget every time it comes back.
      gdm_floor_blocks() {
        local last now
        [ -f "$floor_file" ] || return 1
        last="$(cat "$floor_file" 2>/dev/null || true)"
        case "''${last:-}" in
          "" | *[!0-9]*) return 1 ;;
        esac
        now="$(gdm_epoch)"
        [ "$((now - last))" -lt "$restart_floor" ]
      }

      gdm_handle_wedge() {
        local pid uptime
        pid="$(${unitPidCommand})"
        uptime="$(gdm_pid_uptime "''${pid:-}" || printf unknown)"

        if gdm_floor_blocks; then
          gdm_log "wedge: holding — the last restart is inside the ''${restart_floor}s floor"
          gdm_record held-by-floor "$pid" "$uptime"
          return 0
        fi

        gdm_log "wedge: confirmed after $consecutive consecutive failures (flags=[''${gdm_probe_flags:-none}] stat=''${gdm_probe_stat_result:-none} pid=''${pid:-none} uptime=''${uptime}s)"
        gdm_capture "$pid"
        # Stamp and record before restarting: the restart may take this process
        # down with it, and an unrecorded wedge is an unmeasurable one.
        gdm_epoch > "$floor_file"
        gdm_record restarted "$pid" "$uptime"
        gdm_log "wedge: restarting the mount unit"
        ${restartCommand}
        consecutive=0
        # The replacement instance starts cold — it has to clear the sweep and
        # sit through the cache guard's own budget before anything is mounted —
        # so it is owed exactly the grace a cold start is owed. Without this
        # reset the very next cycle reads "was mounted and is not any more" and
        # skips the grace entirely, which turns two legitimate cases into a loop:
        # a mount that is merely slow to come up, and a unit correctly failing
        # loud on an absent cache volume (exit 78). Both then collect a
        # held-by-floor line every interval, into a record this repo never
        # rotates, and a pointless SIGQUIT+kickstart every floor period.
        seen_mounted=0
      }

      gdm_note_failure() {
        cycles=$((cycles + 1))
        consecutive=$((consecutive + 1))
        gdm_log "probe: unhealthy ($consecutive/$threshold) — flags=[''${gdm_probe_flags:-none}] stat=''${gdm_probe_stat_result:-none}"
        if [ "$consecutive" -ge "$threshold" ]; then
          gdm_handle_wedge
        fi
      }

      gdm_watch_once() {
        local pid up

        # An operator who stopped the mount must not have it resurrected.
        if ! ${unitActiveCheck}; then
          gdm_log "probe: the mount unit is not loaded — standing down"
          consecutive=0
          return 0
        fi

        if ! ${mountedCheck}; then
          # Nothing in the kernel's mount table. Inside the grace window that is
          # just a unit coming up — the cache guard alone may wait two minutes.
          #
          # Past it, with the process still alive, this is the quietest failure
          # this system has. Observed on neo 2026-08-19: rclone up 46 minutes,
          # still LISTENing on the NFS port, nothing mounted, not one log line
          # since the mount detached. The goroutine dump explains why nothing
          # ever recovers it — the go-nfs server goroutines are gone while
          # `mountlib.(*MountPoint).Wait` is still blocked waiting to be
          # unmounted, so rclone never exits, so `KeepAlive` never fires. No
          # supervisor can see this, because nothing failed.
          #
          # The grace window only has to cover "has not finished coming up". Once
          # we have *seen* this point mounted, its disappearance is unambiguous
          # and waiting out the grace is pure lost time — which matters: the
          # replacement instance on neo detached about two minutes after start,
          # and a grace-gated watchdog would have sat on that for five.
          if [ "$seen_mounted" = 1 ]; then
            gdm_log "probe: $point was mounted and is not any more"
          else
            pid="$(${unitPidCommand})"
            up="$(gdm_pid_uptime "''${pid:-}" || printf 0)"
            if [ "''${up:-0}" -lt "$mount_grace" ]; then
              gdm_log "probe: nothing is mounted at $point and the unit is ''${up:-0}s old — standing down inside the ''${mount_grace}s grace window"
              consecutive=0
              return 0
            fi
          fi
          gdm_probe_flags=""
          gdm_probe_stat_result="unmounted"
          gdm_note_failure
          return 0
        fi

        seen_mounted=1

        if gdm_probe; then
          if [ "$consecutive" -gt 0 ]; then
            gdm_log "probe: recovered after $consecutive consecutive failure(s)"
          fi
          consecutive=0
          cycles=$((cycles + 1))
          if [ "$((cycles % heartbeat_every))" -eq 0 ]; then
            gdm_log "probe: healthy ($cycles cycles)"
          fi
          return 0
        fi

        gdm_note_failure
        return 0
      }

      gdm_watch_loop() {
        gdm_log "watchdog: start — interval ''${interval}s, probe timeout ''${probe_timeout}s, threshold $threshold, restart floor ''${restart_floor}s"
        while :; do
          gdm_watch_once
          sleep "$interval"
        done
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
          sock = if settings.remoteControl then rcSocket settings org mount else "";
          watchdog = watchdogPaths settings org mount;
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
    rcSocket
    watchdogPaths
    mountPoint
    unitName
    rootMount
    linkPath
    linkTarget
    mountArgs
    renderPlan
    shellPreamble
    cacheGuard
    watchdogShell
    ;
}
