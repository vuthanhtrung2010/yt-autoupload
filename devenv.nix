{
  pkgs,
  lib,
  config,
  inputs,
  ...
}:

let
  libraries = with pkgs; [
    stdenv.cc.cc.lib
    gtk3
    gdk-pixbuf
    glib
    pango
    cairo
    harfbuzz
    fontconfig
    freetype
    dbus
    nss
    nspr
    alsa-lib
    at-spi2-core
    libdrm
    xorg.libX11
    xorg.libXcomposite
    xorg.libXcursor
    xorg.libXdamage
    xorg.libXext
    xorg.libXfixes
    xorg.libXi
    xorg.libXrandr
    xorg.libXrender
    xorg.libXtst
    xorg.libxcb
    xorg.libxshmfence
    libxkbcommon
    libGL
    mesa
    ffmpeg
    udev
  ];
in
{
  packages = [
    pkgs.git
    pkgs.uv
  ] ++ libraries;

  env.LD_LIBRARY_PATH = "${lib.makeLibraryPath libraries}";

  enterShell = ''
    export LD_LIBRARY_PATH="${lib.makeLibraryPath libraries}:$LD_LIBRARY_PATH"
  '';
}
