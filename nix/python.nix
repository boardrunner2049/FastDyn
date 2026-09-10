{ pkgs }:
let
  ps = pkgs.python312Packages;
  fmpy = ps.buildPythonPackage {
    pname = "fmpy";
    version = "0.3.32";
    format = "wheel";
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/f5/4c/5e085b9b0c823eb298af2e50edf98c8d8d9af3f04edbb8e972a8dabc97fb/fmpy-0.3.32-py3-none-any.whl";
      hash = "sha256-s3neuUF7hCAoADoojQDBxfE40VgOH/EsxsQqmHGc7PA=";
    };
    nativeBuildInputs = [ pkgs.autoPatchelfHook ];
    buildInputs = [ pkgs.stdenv.cc.cc.lib ];
    preFixup = ''
      sundials="$out/${ps.python.sitePackages}/fmpy/sundials/x86_64-linux"
      ln -s sundials_core.so "$sundials/libsundials_core.so.7"
      ln -s sundials_sunmatrixdense.so "$sundials/libsundials_sunmatrixdense.so.5"
    '';
    pythonRemoveDeps = [ "cmake" ];
    dependencies = with ps; [ attrs jinja2 lark lxml msgpack nbformat numpy ];
    pythonImportsCheck = [ "fmpy" "fmpy.build" "fmpy.fmi3" ];
  };
  mavproxy = ps.buildPythonPackage {
    pname = "mavproxy";
    version = "1.8.74";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "mavproxy";
      version = "1.8.74";
      hash = "sha256-CD2y6xHpYJh7/T7AsAB3WFMf2T93UUHbJ5r1bwqVo0w=";
    };
    build-system = [ ps.setuptools ];
    dependencies = with ps; [ pymavlink pyserial numpy pynmeagps tornado ];
    doCheck = false;
    pythonImportsCheck = [ "MAVProxy" "MAVProxy.modules.mavproxy_cesium" ];
  };
in
pkgs.python312.withPackages (ps: with ps; [
  capstone click cmsis-svd colorama python-dotenv jpype1 lxml networkx numpy
  ollama openai pydot pyelftools pyghidra pytest pyyaml pymavlink future
  scipy six tomli tomli-w setuptools matplotlib mavproxy fmpy
])
