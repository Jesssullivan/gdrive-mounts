{
  description = "gdrive-mounts — IaC for mounting multiple Google Workspace orgs' Drives via rclone (neo/macOS + sting/Linux), deployed through lab home-manager.";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { self, nixpkgs, flake-utils }:
    let
      hmModule = import ./nix/modules/home-manager.nix;
    in
    (flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        version = "0.1.0-dev+${self.shortRev or self.dirtyShortRev or "unknown"}";
      in
      {
        packages.default = pkgs.stdenvNoCC.mkDerivation {
          pname = "gdrive-mounts-tools";
          inherit version;
          src = ./.;
          dontConfigure = true;
          dontBuild = true;
          installPhase = ''
            runHook preInstall
            mkdir -p $out/bin $out/libexec/gdrive-mounts $out/share/gdrive-mounts

            # Every operator script becomes gdrive-mounts-<name>.
            for s in scripts/*.sh; do
              install -m0755 "$s" "$out/bin/gdrive-mounts-$(basename "$s" .sh)"
            done

            # Shared shell library. Scripts look here when the in-repo copy is absent.
            install -m0644 scripts/lib/common.sh $out/libexec/gdrive-mounts/common.sh

            install -m0644 orgs.json $out/share/gdrive-mounts/orgs.json
            install -m0644 config/rclone.conf.template $out/share/gdrive-mounts/rclone.conf.template
            install -m0644 config/orgs.schema.json $out/share/gdrive-mounts/orgs.schema.json
            runHook postInstall
          '';
        };

        checks = {
          # Structural validation of orgs.json + dummy-secret render + rclone parse.
          validate = pkgs.runCommand "gdrive-mounts-validate"
            { nativeBuildInputs = [ pkgs.jq pkgs.rclone pkgs.check-jsonschema ]; }
            ''
              cp -r ${self} work && chmod -R +w work && cd work
              bash scripts/validate-config.sh --quick
              touch $out
            '';

          # gitleaks dir-mode over the tracked tree (offline; defaults embedded).
          gitleaks = pkgs.runCommand "gdrive-mounts-gitleaks"
            { nativeBuildInputs = [ pkgs.gitleaks ]; }
            ''
              cd ${self}
              gitleaks dir . --config .gitleaks.toml --redact --exit-code 1
              touch $out
            '';

          # Home-manager module eval proof. Both platform branches run on every
          # system, so an x86_64-linux runner still covers the Darwin units.
          hm-eval = import ./nix/tests/hm-eval.nix {
            inherit pkgs;
            module = hmModule;
            orgsFile = ./nix/tests/fixtures/orgs-test.json;
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [
            # coreutils/gnused first: bazelisk ships a `sha256sum` helper that
            # would otherwise shadow GNU sha256sum on PATH.
            pkgs.coreutils
            pkgs.gnused
            pkgs.just
            pkgs.bazelisk
            pkgs.rclone
            pkgs.gitleaks
            pkgs.sops
            pkgs.age
            pkgs.jq
            pkgs.sqlite
            pkgs.actionlint
            pkgs.check-jsonschema
            pkgs.shellcheck
          ];
        };

        formatter = pkgs.nixfmt-rfc-style;
      }
    ))
    // {
      homeManagerModules.default = hmModule;
      homeModules.default = hmModule;
    };
}
