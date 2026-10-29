{
  description = "update-manager: update status, CI builds and upstream merges across your NixOS machines, in a Waybar widget";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux"];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = forAll (pkgs: rec {
      update-manager = pkgs.callPackage ./nix/package.nix {};
      default = update-manager;
    });

    nixosModules.default = import ./nix/module.nix self;
    nixosModules.update-manager = self.nixosModules.default;

    overlays.default = final: _prev: {
      update-manager = final.callPackage ./nix/package.nix {};
    };

    checks = forAll (pkgs: let
      update-manager = self.packages.${pkgs.stdenv.hostPlatform.system}.update-manager;
    in {
      # The Python tests run in the package's checkPhase.
      package = update-manager;
      helper = pkgs.runCommand "update-manager-helper-check" {nativeBuildInputs = [pkgs.shellcheck];} ''
        shellcheck --severity=style ${./helper/update-manager-switch}
        touch $out
      '';
      # The NixOS module evaluates: units, polkit rule, nixos-ci progress hook.
      module = let
        system = nixpkgs.lib.nixosSystem {
          inherit (pkgs.stdenv.hostPlatform) system;
          modules = [
            self.nixosModules.default
            {
              services.update-manager = {
                enable = true;
                users = ["alice"];
                settings.ci_host = "machine";
              };
              users.users.alice.isNormalUser = true;
              boot.loader.grub.enable = false;
              fileSystems."/" = {
                device = "none";
                fsType = "tmpfs";
              };
              system.stateVersion = "26.05";
            }
          ];
        };
        c = system.config;
      in
        pkgs.writeText "update-manager-module-check" (builtins.toJSON {
          config = c.environment.etc."update-manager/config.toml".source;
          switch = c.systemd.services."update-manager-switch@".serviceConfig.ExecStart;
          agent = c.systemd.user.services.update-manager-agent.wantedBy;
          panel = c.systemd.user.services.update-manager-panel.serviceConfig.BusName;
          polkit = c.security.polkit.extraConfig;
        });
    });

    devShells = forAll (pkgs: {
      default = pkgs.mkShell {
        inputsFrom = [self.packages.${pkgs.stdenv.hostPlatform.system}.update-manager];
        packages = [pkgs.ruff pkgs.shellcheck pkgs.sway pkgs.grim pkgs.waybar pkgs.jq];
        shellHook = ''export PYTHONPATH=$PWD/src''${PYTHONPATH:+:$PYTHONPATH}'';
      };
    });

    formatter = forAll (pkgs: pkgs.alejandra);
  };
}
