{ pkgs, self, nixpkgs }:
let
  system = pkgs.stdenv.hostPlatform.system;
  pkg = self.packages.${system}.default;
  plugin = "${pkg}/share/noctalia-switch-os/plugins/switch-os";
  nixos = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      self.nixosModules.default
      {
        boot.isContainer = true;
        system.stateVersion = "26.05";
        programs.noctalia-switch-os = { enable = true; keybinding = "Mod+Alt+P"; };
      }
    ];
  };
  etcBinds = nixos.config.environment.etc."noctalia-switch-os/niri-binds.kdl".source;
in
{
  package = pkg;

  plugin = pkgs.runCommand "noctalia-switch-os-plugin-check" { nativeBuildInputs = [ pkgs.luau pkgs.noctalia pkgs.libxml2 ]; } ''
    export HOME=$TMPDIR
    for f in ${plugin}/*.luau; do luau-compile --null "$f" > /dev/null; done
    noctalia plugins lint ${plugin} | tee lint.txt
    grep -q "0 errors" lint.txt
    grep -q '"${pkg}/bin/noctalia-switch-os"' ${plugin}/client.luau
    ! grep -rq '@[a-z_]*@' ${plugin} ${pkg}/libexec
    xmllint --noout ${pkg}/share/polkit-1/actions/noctalia.switchos.policy
    grep -q '${pkg}/bin/noctalia-switch-os-reboot' ${pkg}/share/polkit-1/actions/noctalia.switchos.policy
    touch $out
  '';

  niri-binds = pkgs.runCommand "noctalia-switch-os-niri-check" { nativeBuildInputs = [ pkgs.niri ]; } ''
    export HOME=$TMPDIR XDG_RUNTIME_DIR=$TMPDIR
    for f in ${pkg}/share/noctalia-switch-os/niri-binds.kdl ${etcBinds}; do
      printf 'include "%s"\n' "$f" > config.kdl
      niri validate -c config.kdl
    done
    grep -q 'Mod+Alt+P' ${etcBinds}
    touch $out
  '';

  module = pkgs.runCommand "noctalia-switch-os-module-check" { } ''
    test -f ${nixos.config.system.path}/share/noctalia-switch-os/plugins/switch-os/plugin.toml
    test -f ${nixos.config.system.path}/share/polkit-1/actions/noctalia.switchos.policy
    test -x ${nixos.config.system.path}/bin/noctalia-switch-os-reboot
    ${if nixos.config.security.polkit.enable then "true" else "false"}
    touch $out
  '';
}
// pkgs.lib.optionalAttrs (system == "x86_64-linux") {
  # A UEFI VM booted through real systemd-boot. The actual root helper selects
  # a one-time entry (a NixOS specialisation) and restarts; the test verifies
  # the VM came up in that entry and that the normal default returns afterwards.
  vm = pkgs.testers.runNixOSTest {
    name = "noctalia-switch-os-vm";
    nodes.machine = { ... }: {
      imports = [ self.nixosModules.default ];
      programs.noctalia-switch-os.enable = true;
      virtualisation.useBootLoader = true;
      virtualisation.useEFIBoot = true;
      boot.loader.systemd-boot.enable = true;
      boot.loader.efi.canTouchEfiVariables = true;
      boot.loader.systemd-boot.extraEntries."windows11.conf" = ''
        title Windows 11
        efi /EFI/Microsoft/Boot/bootmgfw.efi
      '';
      specialisation.alt.configuration.environment.etc."switch-os-target".text = "alt";
      users.users.alice.isNormalUser = true;
    };
    testScript = ''
      import json

      def catalog():
          return json.loads(machine.succeed("noctalia-switch-os-list"))

      machine.start()
      machine.wait_for_unit("multi-user.target")
      machine.succeed("pkaction --action-id noctalia.switchos.reboot --verbose | grep -q auth_admin")
      machine.succeed("pkaction --action-id noctalia.switchos.list --verbose")

      state = catalog()
      print(json.dumps(state, indent=2))
      ids = [e["id"] for e in state["entries"]]
      assert state["supported"], state
      assert state["windows_found"], state
      assert "windows11.conf" in ids, ids
      target = next(i for i in ids if "specialisation-alt" in i)
      default = state["default_entry"]
      assert default and default != target, state

      # Through PolicyKit as root (the same path the UI uses).
      ui = json.loads(machine.succeed("noctalia-switch-os status"))
      assert [e["id"] for e in ui["entries"]] == ids

      # An unprivileged user without an active session cannot even list, and is told why.
      out = machine.fail("su - alice -c 'noctalia-switch-os status'")
      assert "not authorized" in out or "cancelled" in out, out

      # Confirmation gate and invalid / stale identifiers change nothing.
      machine.fail("noctalia-switch-os start --entry " + target)
      machine.fail("noctalia-switch-os-reboot -- --help")
      machine.fail("noctalia-switch-os-reboot does-not-exist.conf")
      assert catalog()["oneshot_entry"] is None

      # Real one-time switch: the helper sets LoaderEntryOneShot and restarts.
      machine.execute("noctalia-switch-os-reboot " + target + " >/dev/console 2>&1 &")
      machine.wait_for_shutdown()
      machine.start()
      machine.wait_for_unit("multi-user.target")
      machine.succeed("grep -q alt /etc/switch-os-target")
      after = catalog()
      assert after["current_entry"] == target, after
      assert after["default_entry"] == default, after
      assert after["oneshot_entry"] is None, after

      # The next ordinary restart returns to the unchanged default.
      machine.shutdown()
      machine.start()
      machine.wait_for_unit("multi-user.target")
      machine.fail("test -e /etc/switch-os-target")
      assert catalog()["current_entry"] == default
    '';
  };
}
