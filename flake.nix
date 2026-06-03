{
  description = "npu-top: top-like monitor for Intel AI Boost NPUs on NixOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [ "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f system (import nixpkgs { inherit system; }));
      packageFor = pkgs: pkgs.callPackage ./nix/package.nix { };
    in
    {
      packages = forAllSystems (_system: pkgs: {
        default = packageFor pkgs;
        npu-top = packageFor pkgs;
      });

      apps = forAllSystems (system: _pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/npu-top";
          meta.description = "Run npu-top";
        };
        npu-top = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/npu-top";
          meta.description = "Run npu-top";
        };
      });

      devShells = forAllSystems (_system: pkgs: {
        default = pkgs.mkShell {
          packages = [
            pkgs.python3
            pkgs.nixpkgs-fmt
          ];
        };
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.programs.npu-top;
        in
        {
          options.programs.npu-top = {
            enable = lib.mkEnableOption "npu-top Intel AI Boost monitor";
            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              defaultText = lib.literalExpression "inputs.npu-top.packages.\${pkgs.system}.default";
              description = "npu-top package to install.";
            };
            enableFirmware = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Enable redistributable firmware needed by Intel NPUs.";
            };
          };

          config = lib.mkIf cfg.enable {
            environment.systemPackages = [ cfg.package ];
            boot.kernelModules = [ "intel_vpu" ];
            hardware.enableRedistributableFirmware = lib.mkIf cfg.enableFirmware (lib.mkDefault true);
          };
        };
    };
}
