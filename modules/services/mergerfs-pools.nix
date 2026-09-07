# mergerfs-pools — assemble homelab.pools into mounted MergerFS pools.
#
# MergerFS presents a set of independent disks as one tree without striping,
# so a lost disk costs that disk's contents rather than the array — the right
# trade for bulk media, usually paired with SnapRAID parity. The pool
# DEFINITIONS (which branches, which policy) are values and live in the
# consumer's flake via homelab.pools; the member-drive mounts themselves are
# hardware and belong next to your hardware-configuration.
#
# The option set below is the part that took real debugging:
#
#   cache.files=off      page-cache coherence across branches
#   moveonenospc=true    a write that fills a branch moves the file and retries
#   dropcacheonclose=true
#   func.getattr=newest  when a path exists on several branches, stat() returns
#                        the NEWEST branch's metadata — without it two writers
#                        that both hold a copy can see stale sizes/mtimes
#   category.create      see the option description in options.nix: `epmfs` is
#                        REQUIRED on any pool that receives hardlinks from
#                        rsync --link-dest, or mergerfs returns EXDEV and rsync
#                        silently COPIES instead of linking (an incremental
#                        backup quietly becomes a full one and fills the pool)
#   minfreespace         headroom so a create never hits ENOSPC mid-write
{ config, lib, ... }:

{
  imports = [ ../options.nix ];

  config = {
    fileSystems = lib.mapAttrs' (name: pool:
      lib.nameValuePair pool.mountpoint {
        device = pool.branches;
        fsType = "fuse.mergerfs";
        options = [
          "defaults"
          "allow_other"
          "use_ino"
          "cache.files=off"
          "moveonenospc=true"
          "dropcacheonclose=true"
          "category.create=${pool.createPolicy}"
        ]
        ++ lib.optional (pool.minFreeSpace != null) "minfreespace=${pool.minFreeSpace}"
        ++ [ "func.getattr=newest" ]
        ++ lib.optional (pool.fsname != null) "fsname=${pool.fsname}";
      }) config.homelab.pools;

    # Needed for MergerFS (allow_other).
    programs.fuse.userAllowOther = lib.mkIf (config.homelab.pools != { }) true;
  };
}
