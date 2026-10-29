# NixOS module: update-manager's user services (the agent and the resident
# panel), the root units that switch to a CI-built system, and the polkit rule
# that lets the listed users start them.
#
#   services.update-manager = {
#     enable = true;
#     users = [ "alice" ];          # may start update-manager-switch@ / -boot@ (polkit)
#     settings.ci_host = "desktop"; # /etc/update-manager/config.toml
#   };
self: {
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.update-manager;
  toml = pkgs.formats.toml {};
  exe = "${cfg.package}/bin/update-manager";
  # The setuid sudo first (for `rebuild`), then the user's and the system's
  # packages (git, ssh, nix, t3 from the user's profile).
  unitPath = "/run/wrappers/bin:%h/.nix-profile/bin:/etc/profiles/per-user/%u/bin:/run/current-system/sw/bin";
  switchUnit = mode: {
    description = "update-manager: make the NixOS system %I the ${
      if mode == "switch"
      then "running"
      else "default boot"
    } system";
    # A switch restarts changed units; this one must never be one of them.
    restartIfChanged = false;
    stopIfChanged = false;
    after = ["network-online.target"];
    wants = ["network-online.target"];
    path = [config.nix.package pkgs.systemd pkgs.coreutils pkgs.hostname cfg.package];
    environment = {
      UPDATE_MANAGER_HOST = config.networking.hostName;
      UPDATE_MANAGER_SUBSTITUTERS = lib.concatStringsSep " " cfg.substituters;
      UPDATE_MANAGER_SYSTEM_STATE = "/var/lib/update-manager";
    };
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${cfg.package}/libexec/update-manager-switch ${mode} %I";
      StateDirectory = "update-manager";
      StateDirectoryMode = "0755";
      RuntimeDirectory = "update-manager";
      RuntimeDirectoryMode = "0755";
      RuntimeDirectoryPreserve = "yes";
      SyslogIdentifier = "update-manager-${mode}";
      TimeoutStartSec = "2h";
    };
  };
in {
  options.services.update-manager = {
    enable = lib.mkEnableOption "update-manager: update status, CI builds and upstream merges across machines";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "update-manager.packages.\${system}.default";
      description = "The update-manager package.";
    };
    users = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      example = ["alice"];
      description = ''
        Users who may start update-manager-switch@<system>.service and
        update-manager-boot@<system>.service without a password (a polkit
        rule): switching this machine to a NixOS system built for it. The
        units only take /nix/store/<hash>-nixos-system-<this host>-* and fetch
        it with signature checks, but whoever can put such a path in the store
        (any trusted Nix user) can then make it the running system: give this
        only to users who are root-equivalent already.
      '';
    };
    substituters = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      example = ["http://100.64.0.4:5000?priority=50"];
      description = "Extra substituters the switch units fetch from (their keys must be trusted already).";
    };
    agent.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run the agent for every user session (and lingering users): it gathers the state the widget shows.";
    };
    panel.preload = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Keep the panel loaded (hidden) in graphical sessions so it opens at once.";
    };
    settings = lib.mkOption {
      inherit (toml) type;
      default = {};
      description = ''
        /etc/update-manager/config.toml. A user's ~/.config/update-manager/config.toml
        overrides it. See src/update_manager/config.py for every key and its default.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [cfg.package];
    environment.etc."update-manager/config.toml".source = toml.generate "update-manager.toml" cfg.settings;

    systemd.services."update-manager-switch@" = switchUnit "switch";
    systemd.services."update-manager-boot@" = switchUnit "boot";

    systemd.user.services.update-manager-agent = lib.mkIf cfg.agent.enable {
      description = "update-manager agent: update status of this machine and its peers";
      wantedBy = ["default.target"];
      after = ["network-online.target"];
      environment.PATH = lib.mkForce unitPath;
      serviceConfig = {
        ExecStart = "${exe} agent";
        Restart = "on-failure";
        RestartSec = 10;
        # Background work: closure diffs and fetches yield to everything else.
        Nice = 10;
        IOSchedulingClass = "idle";
      };
    };

    systemd.user.services.update-manager-panel = {
      description = "update-manager panel (resident, hidden until toggled)";
      wantedBy = lib.mkIf cfg.panel.preload ["graphical-session.target"];
      partOf = ["graphical-session.target"];
      after = ["graphical-session.target"];
      environment.PATH = lib.mkForce unitPath;
      serviceConfig = {
        Type = "dbus";
        BusName = "org.zyansheep.UpdateManager";
        ExecStart = "${exe} panel serve";
        Restart = "on-failure";
      };
    };

    # switch-to-configuration starts no new user units and restarts no changed ones: on the first
    # switch the widget would stay stale until the next login, and after an upgrade the old agent
    # and panel would keep running. Each user manager runs this on every switch
    # (nixos-activation.service); it acts only when the package changed or the agent isn't running.
    system.userActivationScripts.update-manager = let
      systemctl = "${pkgs.systemd}/bin/systemctl";
    in ''
      stamp="''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/update-manager/package"
      if [ "$(cat "$stamp" 2>/dev/null)" != "${cfg.package}" ] ${lib.optionalString cfg.agent.enable
        "|| ! ${systemctl} --user --quiet is-active update-manager-agent.service"}; then
        ${lib.optionalString cfg.agent.enable "${systemctl} --user restart --no-block update-manager-agent.service || true"}
        if ${systemctl} --user --quiet is-active graphical-session.target; then
          ${systemctl} --user restart --no-block update-manager-panel.service || true
        fi
        mkdir -p "$(dirname "$stamp")" && echo "${cfg.package}" >"$stamp"
      fi
    '';

    security.polkit.extraConfig = lib.mkIf (cfg.users != []) ''
      // update-manager: the listed users may start (and stop) the switch units.
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" &&
            ${builtins.toJSON cfg.users}.indexOf(subject.user) >= 0) {
          var unit = action.lookup("unit") || "";
          var verb = action.lookup("verb");
          if (/^update-manager-(switch|boot)@[^\/]+\.service$/.test(unit) &&
              (verb == "start" || verb == "stop")) {
            return polkit.Result.YES;
          }
        }
      });
    '';
  };
}
