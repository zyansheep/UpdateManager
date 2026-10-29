{
  lib,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  gtk4-layer-shell,
  adwaita-icon-theme,
  makeFontsConf,
  # What update-manager runs. nix and ssh come after the session's own (suffix),
  # so the system's nix (matching its daemon) and ssh config win.
  git,
  openssh,
  nix,
  systemd,
  libnotify,
  procps,
  wl-clipboard,
  coreutils,
  util-linux,
  bash,
}:
python3Packages.buildPythonApplication {
  pname = "update-manager";
  version = builtins.head (builtins.match ".*__version__ = '([^']+)'.*" (builtins.readFile ../src/update_manager/__init__.py));
  pyproject = true;
  src = lib.cleanSourceWith {
    src = ./..;
    filter = path: _type: !(lib.hasSuffix ".nix" path || lib.hasInfix "/result" path || lib.hasInfix "/screenshots" path);
  };

  build-system = [python3Packages.setuptools];
  dependencies = with python3Packages; [pygobject3 pycairo];
  nativeBuildInputs = [wrapGAppsHook4 gobject-introspection];
  buildInputs = [gtk4 libadwaita gtk4-layer-shell];

  # One wrapper for every command. Only `panel serve` loads GTK; it re-runs
  # itself with gtk4-layer-shell preloaded (launch.preload), like drainhound.
  dontWrapGApps = true;
  preFixup = ''
    makeWrapperArgs+=(
      "''${gappsWrapperArgs[@]}"
      --prefix XDG_DATA_DIRS : ${adwaita-icon-theme}/share
      --set-default UPDATE_MANAGER_LAYER_SHELL_LIB ${gtk4-layer-shell}/lib/libgtk4-layer-shell.so
      --set UPDATE_MANAGER_SELF $out/bin/update-manager
      --suffix PATH : ${lib.makeBinPath [git openssh nix systemd libnotify procps wl-clipboard coreutils util-linux]}
    )
  '';

  postInstall = ''
    install -Dm755 helper/update-manager-switch $out/libexec/update-manager-switch
    substituteInPlace $out/libexec/update-manager-switch \
      --replace-fail '#!/usr/bin/env bash' '#!${lib.getExe bash}'
    install -Dm644 -t $out/share/doc/update-manager README.md TESTING.md
    cp -r contrib $out/share/doc/update-manager/
  '';

  nativeCheckInputs = [git];
  checkPhase = ''
    runHook preCheck
    export FONTCONFIG_FILE=${makeFontsConf {fontDirectories = [];}} XDG_CACHE_HOME=$TMPDIR/cache HOME=$TMPDIR
    git config --global user.email test@example.com
    git config --global user.name test
    git config --global init.defaultBranch main
    ${python3Packages.python.interpreter} -m unittest discover -s tests -t .
    runHook postCheck
  '';
  pythonImportsCheck = ["update_manager.cli" "update_manager.agent" "update_manager.waybar"];

  meta = {
    description = "Update status, CI builds and upstream merges across your NixOS machines, in a Waybar widget";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "update-manager";
  };
}
