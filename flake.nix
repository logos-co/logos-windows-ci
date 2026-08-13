{
  # The flake exists for ONE reason: two composite actions in this repo resolve
  # their load-bearing tool from `$GITHUB_ACTION_PATH/../../..`, i.e. from this
  # repo's own checkout --
  #
  #   windows-gates  ->  pkgsWindows.buildPackages.binutils   (the mingw objdump
  #                      every import-closure verdict is only as good as)
  #   windows-smoke  ->  wine64Packages.minimal               (the Linux leg's
  #                      launcher, which decides whether every PE "starts")
  #
  # Both are PINNED on purpose. Taken from the runner's flake registry they
  # would be a floating channel tarball nobody in this org controls, which can
  # differ between two runners in the same workflow -- an unpinned reader is an
  # unpinned verdict. Both actions hard-fail if flake.nix or flake.lock is
  # missing here, so deleting this file is loud rather than silent.
  #
  # WHY NOT TAKE logos-nix AS AN INPUT. It would be the obvious move -- that is
  # where these attribute paths come from -- and it is wrong, because it
  # re-creates exactly the coupling this repo exists to break: this lock would
  # have to be bumped whenever logos-nix moves, and every gates run would
  # evaluate the Qt cross-overlay to fetch an objdump.
  #
  # It is also unnecessary, and that was MEASURED rather than assumed. These two
  # attributes are stock nixpkgs and reproduce byte-identically with no overlay:
  #
  #   pkgsWindows.buildPackages.binutils  ->  the overlay is applied host-side
  #     (logos-nix flake.nix:95 passes it as `crossOverlays`), and crossOverlays
  #     never reach buildPackages. So the mingw objdump is untouched by it.
  #   wine64Packages.minimal              ->  plain native nixpkgs, reached
  #     through logos-nix's `(import nixpkgs {...}) // { pkgsWindows = ...; }`
  #     splat. Nothing logos-nix defines is in its path at all.
  #
  # THE LIMIT OF THAT EQUIVALENCE, because it is narrower than it looks: it
  # holds under `buildPackages` and for stock native attributes. It does NOT
  # generalise to real cross-compiled targets -- overlay-free
  # `pkgsWindows.qt6.qtbase` at this rev does not even evaluate, which is the
  # whole reason logos-nix's overlay exists. If a gate ever needs a genuine
  # `pkgsWindows.<target>` attribute, this flake is no longer sufficient and the
  # answer is to take logos-nix as an input after all.
  #
  # The revs are logos-nix's own, copied at extraction time. They are ALLOWED to
  # drift from it: nothing here is linked against anything logos-nix builds, and
  # the objdump only has to parse `pei-x86-64` -- which windows-gates proves
  # with an `objdump --info` positive control before trusting it.

  description = "Pinned readers for the Logos CI actions: mingw objdump and wine";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e9f00bd893984bc8ce46c895c3bf7cac95331127";
    nixpkgs-windows.url = "github:NixOS/nixpkgs/b5aa0fbd538984f6e3d201be0005b4463d8b09f8";
  };

  outputs = { self, nixpkgs, nixpkgs-windows }:
    let
      # x86_64-linux ONLY, and deliberately not genAttrs over a systems list.
      # Everything that reads this flake is a `nix build` on the ubuntu-latest
      # builder: the gates job, the wine leg, and lint-ci. The native-Windows
      # leg invokes nix at all.
      system = "x86_64-linux";
    in
    {
      legacyPackages.${system} = (import nixpkgs { inherit system; }) // {
        pkgsWindows = import nixpkgs-windows {
          # localSystem is load-bearing TWICE. Without it the flake fails pure
          # eval outright ("attribute 'currentSystem' missing"); evaluated
          # impurely it would silently yield a different reader per runner
          # architecture.
          localSystem = system;
          crossSystem = {
            config = "x86_64-w64-mingw32";
            # ucrt, not the default msvcrt: it changes the store path, so
            # dropping it would silently fetch a different binutils than the one
            # whose behaviour is documented here. Matches logos-nix flake.nix:67.
            libc = "ucrt";
          };
        };
      };
    };
}
