{ pkgs, devShell }:
let
  # dockerTools captures the actual development shell, including compiler and
  # pkg-config setup hooks. No second package list or Dockerfile to maintain.
  shellImage = pkgs.dockerTools.streamNixShellImage {
    drv = devShell;
    name = "fastdyn-dev";
    tag = "local";
    run = ''exec "$@"'';
  };
  image = shellImage.override (old: {
    config = old.config // {
      Entrypoint = old.config.Cmd;
      Cmd = [ "bash" ];
      WorkingDir = "/workspace";
    };
    # Support --user UID:GID with a bind-mounted checkout on any Linux host.
    fakeRootCommands = old.fakeRootCommands + ''
      chmod 1777 ./build
    '';
  });
in pkgs.runCommand "fastdyn-dev.tar.gz" {
  nativeBuildInputs = [ pkgs.pigz ];
} ''
  ${image} | pigz -n -1 > "$out"
''
