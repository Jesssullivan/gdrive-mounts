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
    ;
}
