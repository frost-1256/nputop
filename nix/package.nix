{ lib
, stdenvNoCC
, makeWrapper
, python3
, ncurses
}:

stdenvNoCC.mkDerivation {
  pname = "npu-top";
  version = "0.1.0";

  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let
        base = baseNameOf path;
      in
        !(base == ".git" || base == ".agents" || base == ".codex" || base == "result");
  };

  nativeBuildInputs = [ makeWrapper ];

  dontConfigure = true;
  dontBuild = true;
  doCheck = true;

  checkPhase = ''
    runHook preCheck
    ${python3}/bin/python -m unittest discover -s tests
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 src/npu_top.py $out/lib/npu-top/npu_top.py
    makeWrapper ${python3}/bin/python $out/bin/npu-top \
      --add-flags $out/lib/npu-top/npu_top.py \
      --prefix TERMINFO_DIRS : ${ncurses.out}/share/terminfo
    ln -s npu-top $out/bin/intel-ai-boost-top
    install -Dm644 README.md $out/share/doc/npu-top/README.md
    install -Dm644 LICENSE $out/share/licenses/npu-top/LICENSE
    runHook postInstall
  '';

  meta = {
    description = "Top-like monitor for Intel AI Boost NPUs using intel_vpu sysfs counters";
    homepage = "https://github.com/spring/npu-top";
    license = lib.licenses.mit;
    mainProgram = "npu-top";
    platforms = [ "x86_64-linux" ];
  };
}
