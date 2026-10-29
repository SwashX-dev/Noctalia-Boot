{ lib, stdenvNoCC, python3, makeWrapper, systemd, libnotify, version ? (import ./version.nix) }:
stdenvNoCC.mkDerivation {
  pname = "noctalia-switch-os";
  inherit version;
  src = lib.fileset.toSource {
    root = ./..;
    fileset = lib.fileset.unions [ ../backend ../plugin ../niri ../tests ../README.md ../LICENSE ../CHANGELOG.md ];
  };
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3 ];
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    PYTHONPATH=$PWD/backend PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
    runHook postCheck
  '';
  installPhase = ''
    runHook preInstall
    share=$out/share/noctalia-switch-os
    lib=$out/libexec/noctalia-switch-os
    mkdir -p $out/bin $lib $share/plugins/switch-os $out/share/polkit-1/actions $out/share/doc/noctalia-switch-os
    cp backend/*.py $lib/
    cp -r plugin/. $share/plugins/switch-os/
    cp niri/binds.kdl $share/niri-binds.kdl
    substituteInPlace $share/plugins/switch-os/client.luau \
      --replace-fail '"@backend@"' "\"$out/bin/noctalia-switch-os\""
    substituteInPlace $lib/boot_helper.py \
      --replace-fail '@bootctl@' '${systemd}/bin/bootctl' \
      --replace-fail '@systemctl@' '${systemd}/bin/systemctl'
    substituteInPlace $lib/cli.py \
      --replace-fail '@boot_list@' "$out/bin/noctalia-switch-os-list" \
      --replace-fail '@boot_reboot@' "$out/bin/noctalia-switch-os-reboot" \
      --replace-fail '@notify@' '${libnotify}/bin/notify-send'

    makeWrapper ${python3}/bin/python3 $out/bin/noctalia-switch-os --add-flags "-s $lib/cli.py"
    makeWrapper ${python3}/bin/python3 $out/bin/noctalia-switch-os-setup \
      --add-flags "-s $lib/setup.py" --set-default NOCTALIA_SWITCH_OS_SHARE "$share"
    # Root entry points: isolated interpreter, fixed sub-command, stdlib only.
    makeWrapper ${python3}/bin/python3 $out/bin/noctalia-switch-os-list --add-flags "-I $lib/boot_helper.py list"
    makeWrapper ${python3}/bin/python3 $out/bin/noctalia-switch-os-reboot --add-flags "-I $lib/boot_helper.py reboot"

    cat > $out/share/polkit-1/actions/noctalia.switchos.policy <<POLICY
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN" "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
    <policyconfig>
      <action id="noctalia.switchos.list">
        <description>Read installed operating-system boot entries</description>
        <message>Read the operating-system boot menu</message>
        <defaults><allow_any>no</allow_any><allow_inactive>auth_admin</allow_inactive><allow_active>yes</allow_active></defaults>
        <annotate key="org.freedesktop.policykit.exec.path">$out/bin/noctalia-switch-os-list</annotate>
      </action>
      <action id="noctalia.switchos.reboot">
        <description>Select a one-time operating-system boot and restart</description>
        <message>Authenticate to restart into the operating system selected in Noctalia</message>
        <defaults><allow_any>no</allow_any><allow_inactive>no</allow_inactive><allow_active>auth_admin</allow_active></defaults>
        <annotate key="org.freedesktop.policykit.exec.path">$out/bin/noctalia-switch-os-reboot</annotate>
      </action>
    </policyconfig>
    POLICY
    cp README.md LICENSE CHANGELOG.md $out/share/doc/noctalia-switch-os/
    runHook postInstall
  '';
  meta = {
    description = "Confirmed one-time operating-system switching (systemd-boot) as a Noctalia Shell plugin";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "noctalia-switch-os";
  };
}
