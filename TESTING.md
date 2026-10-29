# Testing update-manager on isomorph

State on 2026-10-29: isomorph has ~/Projects/UpdateManager cloned from functor
at 4b53922e31a4, and its nixos-conf wires the module in. functor runs the agent
from the user profile (see "What is set up on functor"). The steps below take
isomorph to the newest commit and check every part.

## 1. Get the newest commits onto isomorph

```sh
git -C ~/Projects/UpdateManager pull                 # origin is functor:Projects/UpdateManager
git -C ~/Projects/UpdateManager log --oneline 4b53922e31a4..   # what changed (also in the final report)
cd ~/nixos-conf && nix flake update update-manager   # the git+file input moves to the new HEAD
nrb                                                  # or: sudo rebuild
```

From 9f7eaf52f048 on, the module restarts the agent and panel itself on a
switch (`system.userActivationScripts`), when the package changed or the
agent isn't running. switch-to-configuration starts no new user units and
restarts no changed ones. The switch that brings this in still runs the old
module, so restart them by hand once:
`systemctl --user restart update-manager-agent update-manager-panel`.

Without a rebuild (to try a commit quickly), run it from the checkout instead:

```sh
systemctl --user stop update-manager-agent update-manager-panel
nix run ~/Projects/UpdateManager -- agent &                     # the agent, in this terminal
nix run ~/Projects/UpdateManager -- panel serve --open          # the panel, opened
```

## 2. Check that it works

```sh
update-manager status                                 # both machines; functor "seen just now"
journalctl --user -u update-manager-agent -f          # errors from ssh, checks
jq '.peers, .summary' $XDG_RUNTIME_DIR/update-manager/state.json
ssh -o BatchMode=yes functor '~/.nix-profile/bin/update-manager --version'   # what the agent runs remotely
```

- The agent's ssh needs to work non-interactively from a user service (no
  passphrase prompt). If `journalctl` shows `Permission denied (publickey)`,
  use a key without a passphrase, or give the unit an agent:
  `systemctl --user set-environment SSH_AUTH_SOCK=$SSH_AUTH_SOCK`.
- isomorph shows up on functor too:
  `ssh functor '~/.nix-profile/bin/update-manager status'`.
- The widget sits right of the services gear. Click it for the panel;
  right-click opens the Upstream page.

## 3. Try the buttons, safest first

1. The refresh icon in the header: everything is rechecked (the slow checks take a minute).
2. **Upstream → Check now**: functor fetches each fork's upstream and previews
   merges. Z3 Code has `fetch = false` on functor for now (see below).
3. **Pull & rebuild** on isomorph: `git pull --ff-only` in ~/nixos-conf, then
   `sudo rebuild`. It's the same as running `nrb` yourself. The job's progress
   and result show on the card, and a toast appears when it ends.
4. **The switch units**, without changing anything: switch to the system
   already running.

   ```sh
   unit="update-manager-switch@$(systemd-escape "$(basename "$(readlink -f /run/current-system)")").service"
   systemctl start "$unit"          # no password: the polkit rule
   journalctl -u "$unit" -n 30; cat /run/update-manager/*.result.json
   ```

5. **Switch / Boot** in the panel: these appear only when CI's build descends
   from the commit isomorph runs. Today isomorph runs a build from an
   uncommitted tree that CI's origin/main lacks. The card says *Local build ·
   CI has 61b4c721* and offers only Rebuild: switching would drop the local
   changes, the widget included.
6. **functor's buttons from isomorph**:
   - Build now / Build with updated inputs start nixos-ci on functor.
   - Rebuild on functor's card runs `sudo rebuild` there, which deploys
     functor's ~/nixos-conf main: a real switch of functor.
   - Upgrade on functor's profile packages changes functor's nix profile.

## What is set up on functor (user scope, by the functor thread)

- `nix profile install ~/Projects/UpdateManager` (profile element
  `UpdateManager`): isomorph's agent runs `~/.nix-profile/bin/update-manager`
  on functor. Update it with `nix profile upgrade UpdateManager`.
- `~/.config/systemd/user/update-manager-agent.service` (enabled, running):
  functor's agent, from that profile. Once services.update-manager is deployed
  on functor, delete it (`systemctl --user disable --now update-manager-agent`,
  then remove the file) so the module's unit is used.
- `~/.config/update-manager/config.toml`: the fork list, with Z3 Code at
  `fetch = false` so the agent doesn't fetch into ~/Repos/t3code while the Z3
  thread works there. Delete the file to go back to the defaults.

## Deploying on functor (yours to do)

functor's nixos-conf has a local branch `update-manager` (not pushed) with the
functor side:
- the flake input and `profiles/services/update-manager.nix`, imported by functor;
- nixos-ci's `nix build` piped through `update-manager nixlog`, for the CI ETA;
- `system.configurationRevision`, so the widget knows the commit of any system.

```sh
cd ~/nixos-conf && git log --oneline main..update-manager && git diff main update-manager
git merge update-manager        # or cherry-pick; flake.nix/flake.lock may conflict with isomorph's wiring
sudo rebuild                    # then remove the user unit above
```

Before any of this reaches origin/main, the input needs a URL nixos-ci can
fetch (it runs with ProtectHome and can't read /home). Push UpdateManager
somewhere (for example a new github:zyansheep/update-manager) and point
`inputs.update-manager.url` there.
