{ pkgs }:
let
  keycodemapdb = pkgs.fetchzip {
    url = "https://gitlab.com/qemu-project/keycodemapdb/-/archive/f5772a62ec52591ff6870b7e8ef32482371f22c6/keycodemapdb-f5772a62ec52591ff6870b7e8ef32482371f22c6.tar.gz";
    sha256 = "193wf392q8hbqkv2irixrp4hxkbswnf23y45s0i76b8qnnd7kdhr";
  };
  softfloat = pkgs.fetchzip {
    url = "https://gitlab.com/qemu-project/berkeley-softfloat-3/-/archive/b64af41c3276f97f0e181920400ee056b9c88037/berkeley-softfloat-3-b64af41c3276f97f0e181920400ee056b9c88037.tar.gz";
    sha256 = "0h2lz7m3mnyccs0sbqg087hgd2csbpd990mqwn1wjlx3x73nkyb1";
  };
  testfloat = pkgs.fetchzip {
    url = "https://gitlab.com/qemu-project/berkeley-testfloat-3/-/archive/e7af9751d9f9fd3b47911f51a5cfd08af256a9ab/berkeley-testfloat-3-e7af9751d9f9fd3b47911f51a5cfd08af256a9ab.tar.gz";
    sha256 = "11a5pv32n6c6g342nwxjkw46s6d4fapw9yvdcrnj9fk6i5wh0x4a";
  };
in pkgs.stdenv.mkDerivation {
  pname = "qemu-fastdyn";
  version = "10.0.50-99b54836";
  src = pkgs.fetchFromGitHub {
    owner = "Arslan8";
    repo = "qemu";
    rev = "99b54836ded8dc3271eba52deee7453efc0a13b9";
    sha256 = "0388n3ih10c9xqgbjcvw6nhgm0yxmxbh7hk282980jkn966bdr9s";
  };
  patches = [ ../patches/qemu-fastdyn-plugin-icount.patch ];
  nativeBuildInputs = with pkgs; [
    pkg-config ninja bison flex
    (python312.withPackages (ps: [ ps.meson ps.pycotap ]))
  ];
  buildInputs = with pkgs; [ glib pixman zlib dtc expat ];
  postPatch = ''
    cp -r ${keycodemapdb} subprojects/keycodemapdb
    cp -r ${softfloat} subprojects/berkeley-softfloat-3
    cp -r ${testfloat} subprojects/berkeley-testfloat-3
    chmod -R u+w subprojects/berkeley-*
    cp -r subprojects/packagefiles/berkeley-softfloat-3/. subprojects/berkeley-softfloat-3/
    cp -r subprojects/packagefiles/berkeley-testfloat-3/. subprojects/berkeley-testfloat-3/
    patchShebangs scripts
  '';
  configurePhase = ''
    runHook preConfigure
    mkdir build
    cd build
    ../configure --prefix="$out" --target-list=arm-softmmu --enable-plugins \
      --disable-docs --disable-werror --disable-download
    runHook postConfigure
  '';
  buildPhase = ''
    runHook preBuild
    ninja -j"$NIX_BUILD_CORES" qemu-system-arm
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin" "$out/include/qemu" "$out/share/qemu"
    cp qemu-system-arm "$out/bin/"
    cp ../include/qemu/qemu-plugin.h "$out/include/qemu/"
    cp ../pc-bios/*bin "$out/share/qemu/"
    runHook postInstall
  '';
}
