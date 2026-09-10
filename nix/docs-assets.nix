{ pkgs }:
let
  pins = builtins.fromTOML (builtins.readFile ../docs/assets.toml);
  rumocaWeb = pkgs.fetchurl { inherit (pins.rumoca) url hash; };
  monaco = pkgs.fetchurl { inherit (pins.monaco) url hash; };
  assets = pkgs.runCommand "fastdyn-docs-assets" { } ''
    mkdir -p "$out/rumoca" "$out/monaco" "$out/licenses"
    tar -xOf ${rumocaWeb} package/modelica_language.js > "$out/rumoca/modelica_language.js"
    tar -xOf ${rumocaWeb} package/LICENSE > "$out/licenses/rumoca-LICENSE"
    tar -xf ${monaco} --strip-components=2 -C "$out/monaco" package/min/vs
    tar -xOf ${monaco} package/LICENSE > "$out/licenses/monaco-LICENSE"
    tar -xOf ${monaco} package/ThirdPartyNotices.txt > "$out/licenses/monaco-ThirdPartyNotices.txt"
  '';
in pkgs.writeShellScriptBin "mdbook-fastdyn-assets" ''
  exec ${pkgs.python312}/bin/python ${../src/fastdyn/docs_assets.py} --prebuilt ${assets} "$@"
''
