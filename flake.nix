{
  description = "Noctalia Boot: confirmed one-time operating-system switching for systemd-boot";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      version = import ./nix/version.nix;
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        noctalia-switch-os = pkgs.callPackage ./nix/package.nix { inherit version; };
        default = noctalia-switch-os;
      });

      apps = forAllSystems (pkgs: let pkg = self.packages.${pkgs.stdenv.hostPlatform.system}.default; in {
        default = { type = "app"; program = "${pkg}/bin/noctalia-switch-os"; meta.description = "Noctalia Boot front end"; };
        setup = { type = "app"; program = "${pkg}/bin/noctalia-switch-os-setup"; meta.description = "Per-user Noctalia/Niri setup"; };
      });

      overlays.default = final: _prev: {
        noctalia-switch-os = final.callPackage ./nix/package.nix { inherit version; };
      };

      nixosModules.default = import ./nix/module.nix;
      nixosModules.noctalia-switch-os = self.nixosModules.default;

      checks = forAllSystems (pkgs: import ./nix/checks.nix { inherit pkgs self nixpkgs; });

      formatter = forAllSystems (pkgs: pkgs.nixfmt);
    };
}
