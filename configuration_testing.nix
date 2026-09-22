# Tak_OS · configuration.nix — System entry point — feature flags and module wiring
# github.com/tak0dan/Tak_OS · GNU GPLv3
{ config, pkgs, lib, ... }:

# =============================================================================
#                           🧠 CONFIG OVERVIEW
# =============================================================================
# Modular NixOS configuration driven by feature flags and GPU profiles.
#
# ┌─ HOW TO USE ───────────────────────────────────────────────────────────┐
# │  1. Set your options in the `features` block below.                    │
# │  2. Run: nixos-rebuild switch                                          │
# │  3. If something breaks, check which feature flag controls it and      │
# │     open the module file listed in that flag's comment.                │
# └────────────────────────────────────────────────────────────────────────┘
#
# ┌─ MODULE LOADING RULES ─────────────────────────────────────────────────────────────────┐
# │  Always loaded:                                                                        │
# │    hardware, boot, display/login, core system, shell                                   │
# │    fonts-base, hardware-graphics, keyring, nix-settings                                │
# │    kde, openssh, gaming, docker, mariadb, system-packages                              │
# │    copilot-cli (guarded internally)                                                    │
# │                                                                                        │
# │  Loaded when features.home-manager = true:                                             │
# │    <home-manager/nixos>  modules/hm-users.nix                                          │
# │    imports repo-managed Home Manager files under ./home/                               │
# │                                                                                        │
# │      # Loaded when features.hyprland = true:                                           │
# │             hyprland                — Hyprland runtime / systemd session integration   │
# │             window-managers         — Hyprland + bspwm/i3 configuration                │
# │             portals                 — XDG desktop portals                              │
# │             quickshell              — Wayland shell widgets                            │
# │             fonts                   — Desktop font collection                          │
# │             theme                   — GTK / cursor / dconf theme                       │
# │             overlays                — nixpkgs overlays                                 │
# │             nh                      — Nix helper utilities                             │
# │             hyprlock                — Screen lock + hypridle.conf generation           │
# │                            → features.hypr.lock / features.hypr.idle                   │
# │             wlogout                 — Logout menu theme deployment                     │
# │                            → features.hypr.logoutTheme                                 │
# │             vm-guest-services (*)   — Imported but inactive until enabled              │
# │             local-hardware-clock (*)— Imported but inactive until enabled              │
# │             packages/hyprland.nix   — Hyprland-specific applications                   │
# │                                                                                        │
# │       Loaded when features.uwu = true:                                                 │
# │           modules/uwu/nixowos.nix        — NixOwOS logo + OS identity                  │
# │           Loaded when features.uwu = false:                                            │
# │           modules/default-fastfetch.nix  — plain NixOS logo wrapper                    │
# │           Loaded when features.uwuPackages = true:                                     │
# │           packages/uwu.nix  → packages/catgirldownloader.nix (+ future pkgs)           │
# │                                                                                        │
# │  Loaded when features.virtualisation = true:                                           │
# │    modules/virtualbox.nix + modules/docker.nix                                         │
# └────────────────────────────────────────────────────────────────────────────────────────┘
#
# =============================================================================


# =============================================================================
#                           🚀 FEATURE TOGGLES
# =============================================================================
let
  # ===========================================================================
  # 🕸️  HIGH-LEVEL GRAPH LAYER
  # ===========================================================================
  # This is the simplified declaration surface for major capabilities.  The
  # canonical `features.*` attrset below is still what the module graph consumes,
  # but these graph nodes now seed the primary feature values so the public model
  # can stay simple while the architecture behind it remains explicit.
  #
  # Valid Nix shape:
  #   graph.services = { steam = true; flatpak = true; };
  #
  # Future services/packages that are not wired yet can live here too:
  #   # mongodb = true;
  #   # mariadb = false;
  graph = {
    services = {
      openssh        = true;
      autoupdate     = true;
      steam          = true;
      virtualisation = false;
      flatpak        = true;
      nixorcist      = true;
      homeManager    = true;
      copilot        = true;
      mariadb        = true;
      mongodb        = false;
      lamp           = true;
    };

    desktop = {
      hyprland    = true;
      kde         = true;
      uwu         = false;
      uwuPackages = false;
    };
  };

  features = rec {

    # =========================================================================
    # ╔═══════════════════════════════════════════════════════════════════════╗
    # ║  § 1 · SYSTEM FEATURES                                               ║
    # ╚═══════════════════════════════════════════════════════════════════════╝
    # =========================================================================

    # ── § 1.1 · System Definition ─────────────────────────────────────────────

    # =========================================================================
    # 🐾 UWU  (meme / aesthetic stack)
    # =========================================================================
    # NixOwOS branding: os-release + fastfetch ASCII logo overlay.
    # → modules/uwu/nixowos.nix
    #
    # Disabling this reverts all branding; no other modules are affected.
    #
    # All credits to yunfachi. Original dots: https://github.com/yunfachi/NixOwOS
    #
    uwu = graph.desktop.uwu; #<--- I know you want to enable it, you femboy.
    uwuPackages = graph.desktop.uwuPackages;
    #~~~~~~~~~~~~~~~~~~
    #                 |
    #                 ∨
    # Sub-toggle: UwU packages (requires uwu = true to be meaningful)
    # Installs catgirldownloader and other UwU-specific packages.
    # → packages/uwu.nix  (standalone derivations in packages/catgirldownloader.nix)
    #

    # ── § 1.2 · SSH & Auto-updates ────────────────────────────────────────────

    # =========================================================================
    # 🔐 OPENSSH
    # =========================================================================
    # Enables the SSH daemon for remote access.
    # → modules/openssh.nix
    #
    # ⚠️  Password authentication is ON. Switch to key-based auth for
    #     production or internet-exposed machines.
    #
    openssh = graph.services.openssh;

    # =========================================================================
    # 🔄 AUTO-UPDATE
    # =========================================================================
    # Automatic system upgrades via a systemd timer.
    # → modules/auto-upgrade.nix
    # → /var/log/takos-auto-upgrade.log   (runtime log)
    #
    # ┌─ HOW THE SCHEDULE WORKS ───────────────────────────────────────────┐
    # │  updates_per  — time unit the frequency is expressed in:           │
    # │                   "hour" | "day" | "week"                          │
    # │  update_times — how many times per that unit to run the upgrade.   │
    # │                   e.g. updates_per = "day"; update_times = 2       │
    # │                        → upgrade runs twice a day (00:00 + 12:00)  │
    # │                                                                    │
    # │  custom.every_hours — override both fields above and run on a      │
    # │                        fixed N-hour cycle instead.                 │
    # └────────────────────────────────────────────────────────────────────┘
    #
    # ┌─ EXTRA OPTIONS ─────────────────────────────────────────────────────┐
    # │  notify      — send a desktop notification on start / success /     │
    # │                failure (delivered to every active graphical         │
    # │                session via D-Bus)                                   │
    # │                                                                     │
    # │  randomDelay — add up to 20 min of random jitter before each run    │
    # │                so multiple machines don't hammer mirrors at once    │
    # │                                                                     │
    # │  allowReboot — reboot automatically (after 60 s) when nixos-rebuild │
    # │                detects the running kernel differs from the new one  │
    # └─────────────────────────────────────────────────────────────────────┘
    #
    # ⚠️  Upgrades pull from the configured nixos channel and may change
    #     the system on their own. Disable if you prefer manual rebuilds.
    #
    autoupdate = {
      enable = graph.services.autoupdate;
      # Master switch.
      # Enables the auto-upgrade systemd service and timers.
      # When disabled, no scheduled rebuilds will occur.

      notify = true;
      # Send notifications on:
      #   - start of upgrade
      #   - success
      #   - failure
      # NOTE: Requires implementation in the module (e.g. notify-send or logging hook).

      randomDelay = true;
      # Adds a random delay (e.g. up to ~20 minutes) before execution.
      # Helps avoid synchronized load on upstream servers.
      # Useful when many machines share the same update schedule.

      allowReboot = false;
      # Automatically reboot after a successful upgrade if required
      # (e.g. kernel or low-level system changes).
      # NOTE: Not recommended if bootloader is not fully managed by this system.
      # NOTE: Can interrupt active user sessions or running workloads.

      updates_per = "custom";
      # Base scheduling unit.
      # Supported values:
      #   "hour"   → base = 1 hour
      #   "day"    → base = 24 hours
      #   "week"   → base = 7 days
      #   "month"  → base ≈ 30 days (approximation)
      #   "custom" → use custom.every_hours
      #
      # Defines the total time window in which updates are distributed.

      update_times = 1;
      # Number of executions within the selected period.
      #
      # Example:
      #   updates_per = "day"
      #   update_times = 2
      #   → runs every 12 hours
      #
      #   updates_per = "week"
      #   update_times = 7
      #   → runs once per day
      #
      # Internally:
      #   interval = base_time / update_times
      #
      # NOTE: Must be greater than 0.

      custom = {
        every_hours = 4;
      };
      # Custom scheduling mode.
      # Only used when:
      #   updates_per = "custom"
      #
      # Example:
      #   updates_per = "custom"
      #   custom.every_hours = 6
      #   → runs every 6 hours
      #
      # NOTE: Ignored unless updates_per == "custom".
      # NOTE: Overrides update_times logic when active.
    };


    # =========================================================================
    # ╔═══════════════════════════════════════════════════════════════════════╗
    # ║  § 2 · DE / WM                                                       ║
    # ╚═══════════════════════════════════════════════════════════════════════╝
    # =========================================================================

    # =========================================================================
    # 🪟 HYPRLAND
    # =========================================================================
    # Wayland compositor (tiling window manager).
    #
    # Turning ON also loads:
    #   modules/window-managers.nix        — Hyprland + bspwm/i3 fallback, xkb
    #   modules/portals.nix                — XDG desktop portals (screen share, etc.)
    #   modules/quickshell.nix             — Wayland shell widget system
    #   modules/fonts.nix                  — Large font collection for bars/terminals
    #   modules/theme.nix                  — GTK/cursor/dconf dark theme
    #   modules/overlays.nix               — nixpkgs patches (waybar-weather, etc.)
    #   modules/nh.nix                     — Nix helper + nix-output-monitor
    #   modules/vm-guest-services.nix      — (inactive unless vm.guest-services.enable)
    #   modules/local-hardware-clock.nix   — (inactive unless local.hardware-clock.enable)
    #   packages/hyprland.nix              — Hyprland-specific user packages
    #
    hyprland = graph.desktop.hyprland;

    # ─── Hyprland sub-toggles ──────────────────────────────────────────────
    # All sub-toggles below require hyprland = true to have any effect.
    #
    hypr = {

      # 🔒 LOCK — GPU-accelerated screen locker (hyprlock)
      #   enable  — install hyprlock and configure it in the idle daemon
      #   timeout — seconds of idle before the screen locks
      #             (a warning notification fires 60 s before the lock)
      #
      lock = { enable = true; timeout = 6000; };

      # 💤 IDLE — idle daemon (hypridle) that fires the screen locker on inactivity
      #   Manages ~/.config/hypr/hypridle.conf for every user in home-manager-users.
      #   Set to false to skip the idle daemon entirely (and leave the conf untouched).
      #
      idle = true;

      bar      = true;   # 📊 Waybar  — Wayland status bar
      notif    = true;   # 🔔 SwayNC  — Notification centre
      logout   = true;   # 🚪 Wlogout — Logout / power-off menu
      launcher = true;   # 🔍 Rofi    — Application launcher (Wayland mode)

      # 🎨 LOGOUT THEME — Visual skin for the wlogout screen
      # Assets live in /etc/nixos/assets/wlogout/<profile>/
      #
      #   "default"     — Rounded icon buttons, wallust colour import (LinuxBeginnings)
      #   "catppuccin"  — Catppuccin Mocha / Mauve  (https://github.com/catppuccin/wlogout)
      #   "minimal"     — Minimal dark style, system wlogout icons
      #                   (https://github.com/shivalingeshwar6/wlogout-minimal)
      #   "end4"        — Material Symbols font icons, translucent dark
      #                   (https://github.com/end-4/dots-hyprland)
      #
      logoutTheme = "catppuccin";

      # 🎨 PER-USER LOGOUT THEME OVERRIDES
      # Map username → theme to give a specific user a different wlogout skin.
      # Users not listed here inherit logoutTheme above.
      #
      # Example:
      #   userLogoutThemes = { tak_2 = "minimal"; };
      #
      userLogoutThemes = {};
    };

    # =========================================================================
    # 🎨 KDE RUNTIME
    # =========================================================================
    # KDE libraries and Qt integration for apps — does NOT install Plasma.
    # → modules/kde.nix
    # → packages/kde.nix  (user packages)
    #
    # ⚠️  polkit-kde-agent is hardwired to hyprland-session.target.
    #     If hyprland = false, polkit popups will not auto-start.
    #
    # __TAKOS_FEATURE_KDE_START__
    kde = graph.desktop.kde;
    # __TAKOS_FEATURE_KDE_END__


    # =========================================================================
    # 🎮 STEAM / GAMING
    # =========================================================================
    # Steam with Gamescope + GameMode performance governor.
    # → modules/gaming.nix
    # → packages/games.nix  (user packages: Lutris, Heroic, MangoHud, etc.)
    #
    # __TAKOS_FEATURE_STEAM_START__
    steam = graph.services.steam;
    # __TAKOS_FEATURE_STEAM_END__


    # =========================================================================
    # ╔═══════════════════════════════════════════════════════════════════════╗
    # ║  § 4 · ADDITIONAL PACKAGES                                           ║
    # ╚═══════════════════════════════════════════════════════════════════════╝
    # =========================================================================

    # =========================================================================
    # 📦 VIRTUALISATION
    # =========================================================================
    # Enables Docker + VirtualBox host.
    # → modules/virtualbox.nix
    #
    # Disabled by default: it is heavy, slow to build, and should be opted into
    # explicitly instead of enabled by default during installation.
    #
    # __TAKOS_FEATURE_VIRTUALISATION_START__
    virtualisation = graph.services.virtualisation;
    # __TAKOS_FEATURE_VIRTUALISATION_END__

    # 📦 FLATPAK
    # =========================================================================
    # Optional Flatpak runtime support.
    # → modules/flatpak.nix
    #
    # __TAKOS_FEATURE_FLATPAK_START__
    flatpak = graph.services.flatpak;
    # __TAKOS_FEATURE_FLATPAK_END__

    # =========================================================================
    # 🤖 NIXORCIST   (Work in progress)
    # =========================================================================
    # Custom package automation system (see /etc/nixos/nixorcist/).
    # Exposes the `nixorcist` CLI only.
    # Pure generated package imports are intentionally disabled.
    # → modules/system-packages.nix  (nixorcist CLI wrapper)
    #
    # __TAKOS_FEATURE_NIXORCIST_START__
    nixorcist = graph.services.nixorcist;
    # __TAKOS_FEATURE_NIXORCIST_END__

    # =========================================================================
    # 🏠 HOME-MANAGER
    # =========================================================================
    # Declarative management of /home/ (dotfiles, user packages, services).
    # → modules/hm-users.nix  — profile declarations per user
    # → home/common.nix       — shared Home Manager baseline
    # → home/<user>.nix       — repo-managed per-user Home Manager config
    #
    # A machine that has the same users-declared/ and home/ files can restore
    # the same Home Manager state on rebuild with no host-local bootstrap step.
    #
    home-manager = graph.services.homeManager;
    home-manager-users =
      # Derived from ./users-declared/user-list.nix.
      import ./users-declared/user-list.nix;
    # =========================================================================
    # =========================================================================
    # 👤 SYSTEM USERS
    # =========================================================================
    # Per-user NixOS modules live in ./users-declared/<name>.nix.
    # They are generated from discovered existing users by the Tak_OS installer.
    # modules/users.nix links the generated users hub automatically.
    #

    # 🤖 GITHUB COPILOT CLI
    # =========================================================================
    # Installs the GitHub Copilot CLI from nixpkgs.
    # → modules/copilot-cli.nix
    #
    # __TAKOS_FEATURE_COPILOT_START__
    copilot = graph.services.copilot;
    # __TAKOS_FEATURE_COPILOT_END__

  };


  # ===========================================================================
  # 🚫 DISABLED PACKAGES
  # ===========================================================================
  # Managed by the CLI tools — prefer those over manual edits here.
  #
  # Edit this declarative list directly to disable a package globally.
  #
  # Source of truth:  packages/disabled/disabled-packages.nix
  # Format:           [ "steam" "discord" "telegram-desktop" ]
  #
  # You can also add quick one-off names directly to `extraDisabled` below
  # without touching the managed file — they are merged at evaluation time.
  #
  extraDisabled    = [];
  disabledPackages = import ./packages/disabled/disabled-packages.nix;
  isEnabled        = pkg: !(builtins.elem (lib.getName pkg) disabledPackages);
  filterPkgs       = list: builtins.filter isEnabled list;


in
{

  # ===========================================================================
  # 📦 IMPORTS
  # ===========================================================================
  # configuration.nix remains the public manifest, but the implementation graph
  # now flows through a few architectural hubs instead of one growing import
  # list.  This keeps the feature-flag UX intact while making ownership explicit.
  #
  #   hardware-configuration.nix  → machine-specific hardware only
  #   manifest-wiring.nix         → public interface wiring (_module.args, sddm)
  #   foundation.nix              → always-loaded system baseline
  #   feature-layer.nix           → always-imported modules with internal guards
  #   home-manager-layer.nix      → conditional Home Manager stack
  #   hyprland-layer.nix          → conditional Hyprland desktop stack
  #   branding-layer.nix          → mutually exclusive branding path
  #
  imports = [
    ./hardware-configuration.nix
    (import ./modules/manifest-wiring.nix { inherit features filterPkgs; })
    ./modules/foundation.nix
    ./modules/feature-layer.nix
    (import ./modules/home-manager-layer.nix { inherit lib features; })
    (import ./modules/hyprland-layer.nix { inherit lib features; })
    (import ./modules/branding-layer.nix { inherit lib features; })
  ];

  # ===========================================================================
  # 📶 HTTPD  (Apache + PHP)
  # ===========================================================================
  # LAMP stack web server. Toggle with graph.services.lamp.
  #
  takos.httpd = {
    enable = graph.services.lamp;
    adminAddr = "post@mysite.com";
    serverName = "localhost";
    documentRoot = "/var/www/html";
    enablePHP = true;
  };

  # ===========================================================================
  # 🗄️ MARIADB
  # ===========================================================================
  # Declarative MariaDB service, databases, ensured users, and data path.
  # Toggle the whole block with graph.services.mariadb above.
  #
  takos.mariadb = {
    enable = graph.services.mariadb;
    package = pkgs.mariadb;
    dataDir = "/var/lib/mysql";
    user = "mysql";
    group = "mysql";

    ensureDatabases = [ ];
    ensureUsers = [ ];
    initialDatabases = [ ];
    initialScript = null;

    settings = {
      mysqld = {
        "bind-address" = "127.0.0.1";
        port = 330066;
      };
    };

    tools = {
      enable = true;
      workbench = false;
    };
  };

  # ===========================================================================
  # 📶 WIFI-EDU
  # ===========================================================================
  # Declarative NetworkManager profile backed by an environment file containing
  # WIFI_EDU_USERNAME and WIFI_EDU_PASSWORD.
  #
  takos.networking.wifiEdu = {
    enable = false;
    environmentFile = null;
  };


  # ===========================================================================
  # 📦 SYSTEM PACKAGES
  # ===========================================================================
  # Add packages here directly — the same as on a stock NixOS system.
  # These are merged with the module-assembled list in modules/system-packages.nix.
  # For larger curated groups, use the files under packages/ instead.
  #
  # Example:
  #   environment.systemPackages = with pkgs; [
  #     vim
  #     wget
  #     git
  #   ];
  #
  environment.systemPackages = with pkgs; [
    # NixPak is consumed as a regular local package, not as a flake app.
    #(pkgs.callPackage ./nixpak/package.nix {})

    (pkgs.callPackage ./packages/update-git-repos { })

    # IQOS App 6.0.18.0 — native Linux repack (WiX Burn → Java 21, no Wine)
    # Built from /home/tak_1/Downloads/IQOS App Install.STORE.6.0.18.0.exe via /home/tak_1/iqos-linux-installer/default.nix
    (pkgs.callPackage /home/tak_1/iqos-linux-installer/default.nix {})
    firefox
    python3
  ];


  # ===========================================================================
  # 🚫 EXTRA DISABLED PACKAGES
  # ===========================================================================
  # Add package names here to block them from being installed anywhere.
  # These are merged with packages/disabled/disabled-packages.nix at eval time.
  # Keep permanent package exclusions in packages/disabled/disabled-packages.nix.
  #
  kool.disabledPackages = [
    # "discord"
    # "telegram-desktop"
  ];


  # ===========================================================================
  # 🧾 STATE VERSION
  # ===========================================================================
  # DO NOT CHANGE unless you are doing a NixOS release upgrade and know
  # exactly what stateful things will be migrated.
  #
  system.stateVersion = "26.05";

}
