#!/bin/sh
# update-manager for Waybar: the system's (services.update-manager), else one
# installed in the user's profile (`nix profile install ~/Projects/UpdateManager`,
# for trying it before a rebuild). Waybar's PATH has neither ~/.nix-profile nor
# the per-user profile.
for exe in /run/current-system/sw/bin/update-manager "$HOME/.nix-profile/bin/update-manager"; do
  [ -x "$exe" ] && exec "$exe" "$@"
done
[ "$1" = waybar ] && printf '{"text":"\\uf021","class":["unknown"],"tooltip":"update-manager is not installed"}\n'
exit 127
